import asyncio
import hashlib
import hmac
import json
import ssl
import time
import urllib.parse
from decimal import ROUND_DOWN, ROUND_UP, Decimal, getcontext

import aiohttp
import asyncpg
import pandas as pd
from loguru import logger
from pybit.unified_trading import HTTP

from config import CONFIG
from telegram_bot import notify_user
from trading.risk_manager import RiskManager
from trading.strategies import get_strategy_for_channel
from trading.take_profit import TakeProfit
from utils.logger import limit_order_placed
from utils.settings_manager import (
    get_bybit_base_url,
    get_settings,
    is_blacklisted,
    is_bot_active,
    is_trading_paused,
    SETTING_COLUMNS,
    update_setting,
)

DB_HOST = CONFIG["DB_HOST"]
DB_PORT = CONFIG["DB_PORT"]
DB_NAME = CONFIG["DB_NAME"]
DB_USER = CONFIG["DB_USER"]
DB_PASS = CONFIG["DB_PASS"]
DB_SSL = CONFIG.get("DB_SSL", False)

SESSION_REUSE_TIMEOUT = 600
MAX_SL_ATTEMPTS = 5


class SignalHandler:
    _STOP_CREATE_TYPES = {"CreateByStopLoss", "CreateByStopOrder", "CreateByTrailingStop", "CreateByStopLossSwitchMode"}
    _TAKE_PROFIT_CREATE_TYPES = {"CreateByTakeProfit", "CreateByPartialTakeProfit"}
    _USER_CLOSE_CREATE_TYPES = {"CreateByUser"}

    def __init__(self):
        logger.info("[INIT] SignalHandler.__init__ START")
        self.api_key = CONFIG["BYBIT_API_KEY"]
        self.api_secret = CONFIG["BYBIT_API_SECRET"]
        logger.info("[INIT] Отримую base_url...")
        self.base_url = get_bybit_base_url()
        logger.info(f"[INIT] base_url = {self.base_url}")
        self.recv_window = "20000"
        self._time_offset = 0
        self.position_size_percent = CONFIG.get("POSITION_SIZE_PERCENT", 2)
        self.safety_factor = 0.8
        self._symbol_specs = {}
        self.pinbar_detected = False
        self.pinbar_type = None
        self.pinbar_candle = None
        self._session = None
        self._session_timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self._sem = asyncio.Semaphore(5)
        self._db_pool = None
        self._db_lock = asyncio.Lock()
        getcontext().prec = 18
        self._closed = False
        self._active_monitors = 0
        logger.info("[INIT] Створюю TakeProfit...")
        self._take_profit_helper = TakeProfit()
        logger.info("[INIT] TakeProfit створено")
        logger.info("[INIT] Створюю RiskManager...")
        self._risk_manager = RiskManager()
        logger.info("[INIT] RiskManager створено")
        self._position_meta = {}
        self._pair_to_order = {}
        self._bot_owned_positions = set()
        self._bot_owned_position_ids = set()
        self._bot_owned_order_ids = set()
        self._manually_closed_positions = set()
        self._external_trade_monitor_running = False
        self._managed_external_positions = set()
        self._last_balance_log_time = 0.0
        self._balance_log_interval = float(CONFIG.get("BALANCE_LOG_INTERVAL", 300))
        self._pybit_session = None
        self._current_strategy = None
        self._processed_external_positions = set()
        logger.info("[INIT] SignalHandler.__init__ DONE")

    async def _init_db(self):
        if self._db_pool:
            return
        async with self._db_lock:
            if self._db_pool:
                return
            try:
                ssl_opt = "require" if DB_SSL else None
                self._db_pool = await asyncpg.create_pool(
                    host=DB_HOST,
                    port=DB_PORT,
                    database=DB_NAME,
                    user=DB_USER,
                    password=DB_PASS,
                    ssl=ssl_opt,
                    min_size=1,
                    max_size=3,
                    max_queries=50,
                    max_inactive_connection_lifetime=300.0,
                    timeout=30.0,
                    command_timeout=60.0,
                )
                
                # Автоматична ініціалізація таблиці, щоб "кабіна була цілою"
                async with self._db_pool.acquire() as conn:
                    await conn.execute("""
                        CREATE TABLE IF NOT EXISTS settings (
                            id SERIAL PRIMARY KEY,
                            rsi_low FLOAT DEFAULT 30,
                            rsi_high FLOAT DEFAULT 70,
                            rsi_period INT DEFAULT 14,
                            rsi_interval TEXT DEFAULT '1',
                            max_retries INT DEFAULT 3,
                            pinbar_timeout INT DEFAULT 100,
                            trigger_timeout INT DEFAULT 100
                        );
                    """)
                    
                    # Get existing columns first
                    existing_columns_records = await conn.fetch(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = 'settings'"
                    )
                    existing_columns = {r['column_name'] for r in existing_columns_records}

                    # Додаємо всі відсутні колонки з SETTING_COLUMNS
                    for col in SETTING_COLUMNS:
                        if col == "id":
                            continue
                        if col not in existing_columns:
                            try:
                                logger.info(f"Adding missing column (async): {col}")
                                await conn.execute(f"ALTER TABLE settings ADD COLUMN IF NOT EXISTS {col} TEXT")
                            except Exception as e:
                                logger.warning(f"Error adding column {col} in async init: {e}")

                    # Перевіряємо чи є хоча б один запис
                    count = await conn.fetchval("SELECT count(*) FROM settings")
                    if count == 0:
                        await conn.execute("""
                            INSERT INTO settings (rsi_low, rsi_high, rsi_period, rsi_interval, pinbar_tail_percent, pinbar_body_percent, pinbar_opposite_percent)
                            VALUES (30, 70, 14, '1', 66.0, 20.0, 15.0);
                        """)
                        logger.info("Створено початковий запис налаштувань у БД")

                logger.info("Подключение к базе данных (Aiven Cloud/SSL) установлено")
            except Exception as e:
                logger.error(f"Не удалось подключиться к базе данных: {e}")
                self._db_pool = None

    async def _get_max_retries(self):
        await self._init_db()
        if not self._db_pool:
            logger.error("База данных не подключена, используется значение max_retries=1")
            return 1
        
        for attempt in range(3):
            try:
                async with self._db_pool.acquire() as conn:
                    result = await conn.fetchval("SELECT max_retries FROM settings LIMIT 1")
                    if result == "None":
                        return 999
                    return int(result) if result is not None else 1
            except Exception as e:
                logger.warning(f"Спроба {attempt+1}: Не вдалося отримати max_retries: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    logger.error(f"Всі спроби вичерпано. Використовується default=1")
                    return 1

    async def _get_rsi_settings_db(self):
        await self._init_db()
        defaults = {"rsi_low": 30, "rsi_high": 70, "rsi_period": 14, "rsi_interval": "1"}
        if not self._db_pool:
            logger.warning("БД недоступна, використовуються RSI настройки по умолчанию")
            return defaults
        
        for attempt in range(3):
            try:
                async with self._db_pool.acquire() as conn:
                    logger.info("Читаю RSI настройки з БД...")
                    row = await conn.fetchrow(
                        "SELECT rsi_low, rsi_high, rsi_period, rsi_interval FROM settings ORDER BY id DESC LIMIT 1"
                    )
                    if not row:
                        logger.warning("Немає запису в БД, використовую defaults")
                        return defaults

                    def parse_val(val, default, cast_func):
                        if val is None or val == "None":
                            return None
                        try:
                            return cast_func(val)
                        except:
                            return default

                    rsi_low = parse_val(row[0], defaults["rsi_low"], float)
                    rsi_high = parse_val(row[1], defaults["rsi_high"], float)
                    rsi_period = parse_val(row[2], defaults["rsi_period"], int)
                    rsi_interval = parse_val(row[3], defaults["rsi_interval"], str)

                    result = {
                        "rsi_low": rsi_low if rsi_low is not None else defaults["rsi_low"],
                        "rsi_high": rsi_high if rsi_high is not None else defaults["rsi_high"],
                        "rsi_period": rsi_period if rsi_period is not None else defaults["rsi_period"],
                        "rsi_interval": rsi_interval if rsi_interval is not None else defaults["rsi_interval"],
                    }

                    logger.info(f"RSI настройки з БД отримано: {result}")
                    return result
            except Exception as e:
                logger.warning(f"Спроба {attempt+1}: Помилка БД при отриманні RSI: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    logger.error(f"Всі спроби отримати RSI вичерпано. Використовується defaults.")
                    return defaults

    async def get_server_time_offset(self):
        url = f"{self.base_url}/v5/market/time"
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        max_retries = 3
        retry_delay = 1
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            text = await response.text()
                            if text and len(text) > 0:
                                data = json.loads(text)
                                if data.get("retCode") == 0:
                                    server_time = int(data["result"]["timeNano"]) // 1000000
                                    local_time = int(time.time() * 1000)
                                    offset = server_time - local_time
                                    await asyncio.to_thread(update_setting, "timestamp_offset", offset)
                                    return offset
            except Exception:
                pass
            await asyncio.sleep(retry_delay * (2**attempt))
        logger.warning("Не удалось получить время сервера, используется локальное время")
        return 0

    async def _get_session(self):
        """Get or create session with proper closed state handling"""
        if self._closed:

            self._closed = False
            self._session = None

        if self._session is None or self._session.closed:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(
                ssl=ssl_context, limit=50, enable_cleanup_closed=True, force_close=False, ttl_dns_cache=300
            )
            default_headers = {
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "bybit-strategy/1.0",
            }
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=self._session_timeout, headers=default_headers
            )
            self._closed = False
            logger.debug("SignalHandler session created")
        return self._session

    async def close(self):
        """Properly close all resources"""
        if self._closed:
            return

        if getattr(self, "_external_trade_monitor_running", False):
            try:
                await self.stop_external_trade_monitor()
            except Exception as e:
                logger.warning(f"Error stopping external trade monitor: {e}")

        try:
            tp = getattr(self, "_take_profit_helper", None)
            if tp:
                try:
                    await tp.close()
                    logger.debug("TakeProfit helper closed")
                except Exception as e:
                    logger.warning(f"Error closing TakeProfit helper: {e}")
        except Exception:
            pass

        await self._close_session()

        if self._db_pool:
            try:
                await self._db_pool.close()
                # logger.info("Database connection closed")
            except Exception as e:
                logger.warning(f"Error closing DB pool: {e}")
            finally:
                self._db_pool = None

    async def _close_session(self):
        """Close session with proper cleanup"""
        if self._session and not self._session.closed:
            try:
                await self._session.close()

                await asyncio.sleep(0.25)
            except Exception as e:
                logger.warning(f"Error closing session: {e}")
        self._session = None
        self._closed = True

    async def _on_monitor_complete(self):
        if self._active_monitors > 0:
            self._active_monitors -= 1
        if self._active_monitors == 0:
            await self._close_session()

    async def _monitor_limit_order_lifetime(self, pair, order_id, check_interval=10):
        """
        Моніторинг часу життя лімітного ордера.
        Якщо ордер не виконався протягом вказаного часу, він скасовується.
        """
        try:
            settings = get_settings()
            lifetime_val = settings.get("limit_order_lifetime")
            if lifetime_val is None:
                logger.info(f"Моніторинг часу життя лімітки вимкнено для {pair} (налаштування вимкнено)")
                return

            lifetime_minutes = int(lifetime_val)

            if lifetime_minutes <= 0:
                logger.info(f"Моніторинг часу життя лімітки вимкнено для {pair}")
                return

            lifetime_seconds = lifetime_minutes * 60
            meta = self._position_meta.get(order_id)

            if not meta:
                logger.warning(f"Немає метаданих для ордера {order_id}, пропускаємо моніторинг часу життя")
                return

            created_at = meta.get("created_at", time.time())
            logger.info(
                f"Запущено моніторинг часу життя лімітки для {pair}: {lifetime_minutes} хв (order_id: {order_id})"
            )

            while True:
                await asyncio.sleep(check_interval)

                current_meta = self._position_meta.get(order_id)
                if not current_meta:
                    logger.info(f"Ордер {order_id} для {pair} більше не існує в метаданих")
                    return

                try:
                    position = await self._take_profit_helper.get_position(pair)
                    if position and float(position.get("size", 0) or 0) > 0:
                        logger.info(f"Лімітний ордер {order_id} для {pair} виконався - позиція відкрита")
                        return
                except Exception as e:
                    logger.error(f"Помилка перевірки позиції для {pair}: {e}")

                elapsed = time.time() - created_at
                if elapsed >= lifetime_seconds:
                    logger.warning(
                        f"ТАЙМАУТ! Лімітний ордер {order_id} для {pair} не виконався за {lifetime_minutes} хв. Скасовуємо..."
                    )

                    try:
                        cancel_result = await self._cancel_order(pair, order_id)
                        if cancel_result:
                            logger.info(f"Лімітний ордер {order_id} для {pair} успішно скасовано")
                            try:
                                await notify_user(
                                    f"Лімітка скасована по таймауту\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"Монета: {pair}\n"
                                    f"Напрямок: {current_meta.get('direction', 'N/A').upper()}\n"
                                    f"Ціна входу: {current_meta.get('entry_price', 0):.8f}\n"
                                    f"Час життя: {lifetime_minutes} хв\n"
                                    f"Причина: не виконалась протягом {lifetime_minutes} хвилин"
                                )
                            except Exception:
                                pass
                        else:
                            logger.error(f"Не вдалося скасувати лімітний ордер {order_id} для {pair}")
                    except Exception as e:
                        logger.error(f"Помилка скасування ордера {order_id} для {pair}: {e}")

                    if order_id in self._position_meta:
                        del self._position_meta[order_id]
                    if pair in self._pair_to_order:
                        del self._pair_to_order[pair]

                    return

        except asyncio.CancelledError:
            logger.info(f"Моніторинг часу життя лімітки для {pair} скасовано")
            return
        except Exception as e:
            logger.error(f"Помилка моніторингу часу життя лімітки для {pair}: {e}")
            return

    async def _cancel_order(self, pair, order_id):
        """
        Скасування ордера через Bybit API
        """
        try:
            params = {"category": "linear", "symbol": pair, "orderId": order_id}

            result = await self._signed_request("POST", "/v5/order/cancel", params)

            if result and result.get("retCode") == 0:
                logger.info(f"Ордер {order_id} для {pair} успішно скасовано")
                return True
            else:
                ret_code = result.get("retCode") if result else "N/A"
                ret_msg = result.get("retMsg", "Невідома помилка") if result else "Немає відповіді"
                logger.error(f"Не вдалося скасувати ордер {order_id} для {pair}: {ret_msg} (код {ret_code})")
                return False

        except Exception as e:
            logger.error(f"Помилка скасування ордера {order_id} для {pair}: {e}")
            return False

    async def _watch_position_exit(self, pair, poll_interval=3):
        order_id = self._pair_to_order.get(pair)
        if not order_id:
            logger.warning(f"Нет order_id для пары {pair}. Попытка получить позицию напрямую.")
            position = await self._take_profit_helper.get_position(pair)
            if position and float(position.get("size", 0) or 0) > 0:
                synthetic_id = f"synthetic_{pair}_{time.time()}"
                logger.info(f"Создан синтетический order_id {synthetic_id} для мониторинга позиции {pair}")
                self._pair_to_order[pair] = synthetic_id
                self._position_meta[synthetic_id] = {
                    "pair": pair,
                    "direction": "long" if position.get("side") == "Buy" else "short",
                    "entry_price": float(position.get("entryPrice", 0) or 0),
                    "created_at": time.time(),
                    "retry_count": 0,
                }
                order_id = synthetic_id
            else:
                logger.warning(f"Не найдена позиция для {pair}, прекращение мониторинга")
                return

        sl_attempted = False

        while True:
            meta = self._position_meta.get(order_id)
            if not meta:
                logger.error(f"Нет метаданных для order_id {order_id}, пара {pair}. Попытка восстановить...")
                if pair in self._pair_to_order:
                    current_order_id = self._pair_to_order[pair]
                    if current_order_id != order_id and current_order_id in self._position_meta:
                        logger.warning(f"Найдено расхождение order_id для {pair}. Используем актуальные метаданные.")
                        order_id = current_order_id
                        meta = self._position_meta.get(order_id)
                if not meta:
                    logger.error(f"Невозможно восстановить метаданные для {pair}, прекращение мониторинга")
                    return
            if meta.get("finalized") or meta.get("exit_reason"):
                return
            try:
                position = await self._take_profit_helper.get_position(pair)
            except Exception as exc:
                logger.error(f"Не удалось получить данные позиции для {pair}: {exc}")
                await asyncio.sleep(poll_interval)
                continue
            size = 0.0
            if position:
                try:
                    size = float(position.get("size", 0) or 0)
                except (TypeError, ValueError):
                    size = 0
            try:
                if size > 0:
                    meta.setdefault("rsi_tracker_started", False)
                    meta.setdefault("open_notified", False)
                    meta.setdefault("sl_set", False)

                    if not meta["sl_set"] and not sl_attempted and meta.get("stop_loss"):
                        sl_attempted = True
                        try:
                            stop_loss_price = meta.get("stop_loss")
                            direction = meta.get("direction")
                            entry_price = float(position.get("avgPrice", 0) or 0)

                            logger.info(f"НЕГАЙНЕ встановлення SL для {pair}...")
                            logger.info(f"   Позиція виявлена: entry={entry_price:.8f}")
                            logger.info(f"   Встановлюємо SL: {stop_loss_price:.8f}")

                            sl_success = False

                            for sl_attempt in range(MAX_SL_ATTEMPTS):
                                if sl_attempt > 0:
                                    logger.warning(
                                        f"Повторна спроба встановлення SL ({sl_attempt + 1}/{MAX_SL_ATTEMPTS})"
                                    )
                                    await asyncio.sleep(1)

                                success = await self.set_stop_loss(pair, stop_loss_price, direction)

                                if success:
                                    logger.info(f"SL встановлено ОДРАЗУ: {stop_loss_price:.8f}")
                                    meta["sl_set"] = True
                                    sl_success = True
                                    break
                                else:
                                    if sl_attempt < MAX_SL_ATTEMPTS - 1:
                                        logger.warning(
                                            f"Не вдалося встановити SL, спроба {sl_attempt + 1}/{MAX_SL_ATTEMPTS}"
                                        )

                            if not sl_success:
                                logger.error(f"Не вдалося встановити SL після {MAX_SL_ATTEMPTS} спроб")

                        except Exception as e:
                            logger.error(f"Помилка встановлення SL: {e}")

                    if not meta["open_notified"]:
                        try:

                            entry_price = meta.get("entry_price", 0)
                            stop_loss = meta.get("stop_loss", 0)

                            if entry_price == 0 or stop_loss == 0:
                                position = await self._take_profit_helper.get_position(pair)
                                if position:
                                    entry_price = float(position.get("avgPrice", 0) or 0)

                                    sl_from_pos = position.get("stopLoss", "")
                                    if sl_from_pos and sl_from_pos != "0" and sl_from_pos != "":
                                        stop_loss = float(sl_from_pos)

                            direction_emoji = "🟢" if meta.get("direction") == "long" else "🔴"
                            direction_text = "LONG" if meta.get("direction") == "long" else "SHORT"

                            msg = f"{direction_emoji} {pair}\n"
                            msg += f"Відкрито {direction_text}\n"
                            if entry_price and entry_price > 0:
                                msg += f"Вхід: {entry_price:.8f}\n"
                            if stop_loss and stop_loss > 0:
                                msg += f"Стоп: {stop_loss:.8f}"

                            await notify_user(msg)
                        except Exception as e:
                            logger.error(f"Помилка відправки повідомлення про відкриття: {e}")
                        meta["open_notified"] = True

                    if not meta["rsi_tracker_started"]:
                        rsi_cfg = await self._get_rsi_settings_db()

                        rsi_low_val = rsi_cfg.get("rsi_low")
                        rsi_high_val = rsi_cfg.get("rsi_high")

                        rsi_low = float(rsi_low_val) if rsi_low_val is not None else None
                        rsi_high = float(rsi_high_val) if rsi_high_val is not None else None

                        try:

                            rsi_period_val = rsi_cfg.get("rsi_period")
                            if rsi_period_val is None:
                                rsi_period = 14
                            else:
                                rsi_period = int(rsi_period_val)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid RSI period in DB: {rsi_cfg.get('rsi_period')}, using default 14")
                            rsi_period = 14
                        rsi_interval = "1"
                        direction = meta.get("direction", "long")

                        should_start_tracker = False
                        if direction == "long" and rsi_high is not None:
                            should_start_tracker = True
                        elif direction == "short" and rsi_low is not None:
                            should_start_tracker = True

                        if should_start_tracker:

                            async def _on_rsi_exit(sym, dirn, reason, context):
                                logger.info(
                                    f"RSI TP выход обратного вызова для {sym}: причина={reason}, инфо={context}"
                                )

                            logger.info(
                                f"Запуск RSI TP трекера для {pair}: dir={direction}, high={rsi_high}, low={rsi_low}, period={rsi_period}, interval={rsi_interval}"
                            )
                            asyncio.create_task(
                                self._take_profit_helper.track_position_rsi(
                                    pair,
                                    direction,
                                    on_exit=_on_rsi_exit,
                                    rsi_high_override=rsi_high,
                                    rsi_low_override=rsi_low,
                                    rsi_period_override=rsi_period,
                                    rsi_interval_override=rsi_interval,
                                    poll_seconds=max(15, int(self._interval_to_seconds(rsi_interval) // 2)),
                                )
                            )
                            meta["rsi_tracker_started"] = True
                        else:
                            logger.info(f"RSI TP трекер НЕ запущено для {pair} (налаштування вимкнено для {direction})")
                            meta["rsi_tracker_started"] = True
            except Exception as exc:
                logger.error(f"Не удалось запустить RSI трекер для {pair}: {exc}")
            if size <= 0:
                try:
                    direction = meta.get("direction")
                    meta.get("stop_loss")
                    closing_price = await self.get_real_time_price(pair)
                    meta["close_price"] = closing_price
                    meta["close_order"] = await self._get_latest_close_order(pair)
                    reason = await self._determine_exit_reason(pair, meta, closing_price=closing_price)
                    meta["exit_reason"] = reason
                    logger.info(f"Причина закриття {pair}: {reason}")
                except Exception as e:
                    logger.error(f"Не удалось класифіковане закриття для {pair}: {e}")
                    meta["exit_reason"] = meta.get("exit_reason", "position_closed")
                try:

                    direction_emoji = "🟢" if meta.get("direction") == "long" else "🔴"
                    reason_text = {
                        "stop_loss": "🛑 Стоп-лосс",
                        "take_profit": "✅ Тейк-профіт",
                        "manual_close": "👤 Закрито вручну",
                        "position_closed": "⚪ Закрито",
                    }.get(meta.get("exit_reason"), meta.get("exit_reason", "Закрито"))

                    msg = f"{direction_emoji} {pair}\n{reason_text}"
                    if meta.get("close_price"):
                        msg += f"\nЦіна: {meta.get('close_price'):.8f}"

                    await notify_user(msg)
                except Exception:
                    pass
                await self._handle_position_exit(pair, meta["exit_reason"], meta)
                return
            await asyncio.sleep(poll_interval)

    async def _get_latest_close_order(self, pair):
        endpoints = ("/v5/order/history", "/v5/order/list")
        params = {"category": "linear", "symbol": pair, "limit": "10"}
        for endpoint in endpoints:
            result = await self._signed_request("GET", endpoint, params)
            if not result or result.get("retCode") != 0:
                continue
            orders = result.get("result", {}).get("list", []) or []
            if not orders:
                continue
            orders.sort(key=lambda o: int(o.get("updatedTime") or o.get("createdTime") or 0), reverse=True)
            for order in orders:
                if order.get("orderStatus") in {"Filled", "Cancelled"}:
                    return order
        logger.debug(f"Не знайдено інформацію про закритий ордер для {pair}")
        return None

    async def _handle_position_exit(self, pair, reason, meta, context=None):
        if not meta:
            logger.warning(f"Нет метаданных для {pair}")
            return
        if meta.get("finalized"):
            logger.info(f"Позиция для {pair} уже завершена")
            return

        position_key = meta.get("position_key")
        if position_key and position_key in self._managed_external_positions:
            self._managed_external_positions.discard(position_key)
            logger.info(f"Удалено {position_key} из управляемых внешних позиций")

        direction = meta.get("direction")
        entry_price = meta.get("entry_price")
        stop_loss = meta.get("stop_loss")
        take_profit = meta.get("take_profit")
        source = meta.get("source")
        retry_count = meta.get("retry_count", 0)
        closing_price = meta.get("close_price")
        max_retries = await self._get_max_retries()

        logger.info(f"Обработка закрытия {pair}:")
        logger.info(f"   - Причина: {reason}")
        logger.info(f"   - Направление: {direction}")
        logger.info(f"   - Источник: {source}")
        logger.info(f"   - Цена входа: {entry_price:.6f}" if entry_price else "   - Цена входа: N/A")
        logger.info(f"   - Цена закрытия: {closing_price:.6f}" if closing_price else "   - Цена закрытия: N/A")
        logger.info(f"   - Стоп-лосс: {stop_loss:.6f}" if stop_loss else "   - Стоп-лосс: N/A")
        logger.info(f"   - Тейк-профит: {take_profit:.6f}" if take_profit else "   - Тейк-профіт: N/A")

        if reason == "take_profit" and context:
            rsi_value = context.get("rsi")
            rsi_threshold = context.get("rsi_threshold")
            if rsi_value is not None and rsi_threshold is not None:
                logger.info(f"   - RSI при закритті: {rsi_value:.2f} (поріг: {rsi_threshold:.2f})")

                try:
                    pnl_percent = 0
                    if entry_price and closing_price:
                        if direction == "long":
                            pnl_percent = (closing_price - entry_price) / entry_price * 100
                        else:
                            pnl_percent = (entry_price - closing_price) / entry_price * 100

                    msg = (
                        f"TAKE PROFIT: {pair}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Напрямок: {direction.upper()}\n"
                        f"Вхід: {entry_price:.8f}\n"
                        f"Вихід: {closing_price:.8f}\n"
                        f"PnL: {pnl_percent:+.2f}%\n"
                        f"RSI: {rsi_value:.2f} (поріг: {rsi_threshold:.2f})\n"
                        f"Причина: {reason}"
                    )
                    await notify_user(msg)
                except Exception as e:
                    logger.error(f"Помилка відправки TG повідомлення: {e}")

        elif reason in {"stop_loss", "manual_close"}:
            try:
                pnl_percent = 0
                if entry_price and closing_price:
                    if direction == "long":
                        pnl_percent = (closing_price - entry_price) / entry_price * 100
                    else:
                        pnl_percent = (entry_price - closing_price) / entry_price * 100

                reason_emoji = "СТОП" if reason == "stop_loss" else "ВРУЧНУ"
                reason_text = "СТОП-ЛОСС" if reason == "stop_loss" else "ЗАКРИТО ВРУЧНУ"

                msg = (
                    f"{reason_emoji} {reason_text}: {pair}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Напрямок: {direction.upper()}\n"
                    f"Вхід: {entry_price:.8f}\n"
                    if entry_price
                    else (
                        "" f"Вихід: {closing_price:.8f}\n"
                        if closing_price
                        else "" f"PnL: {pnl_percent:+.2f}%\n" f"Причина: {reason}"
                    )
                )
                await notify_user(msg)
            except Exception as e:
                logger.error(f"Помилка відправки TG повідомлення про закриття: {e}")

        logger.info(f"   - Попытки: {retry_count}/{max_retries}")

        if source and source.startswith("external"):
            logger.info(f"Позиция {pair} была открыта ВРУЧНУЮ (source={source})")
            logger.info(f"   - НЕ определяем причину закрытия")
            logger.info(f"   - НЕ делаем повторный вход")
            if pair in self._bot_owned_positions:
                self._bot_owned_positions.discard(pair)
            order_id = self._pair_to_order.get(pair)
            if order_id and order_id in self._bot_owned_order_ids:
                self._bot_owned_order_ids.discard(order_id)
            meta["finalized"] = True
            order_id = self._pair_to_order.pop(pair, None)
            if order_id:
                self._position_meta.pop(order_id, None)
            return

        if reason in {"position_closed", "tracking_stopped"}:
            try:
                old_reason = reason
                reason = await self._determine_exit_reason(pair, meta, closing_price=meta.get("close_price"))
                meta["exit_reason"] = reason
                logger.info(f"Переклассифицирована причина для {pair}: {old_reason} → {reason}")
            except Exception as exc:
                logger.error(f"Не удалось переклассифицировать причину закрытия для {pair}: {exc}")
                reason = "manual_close"

        if reason == "take_profit":
            logger.info(f"Позиция по {pair} ({direction}) закрыта по тейк-профиту")
            from utils.settings_manager import reset_losses_in_row
            reset_losses_in_row()
            
            if closing_price and take_profit:
                distance = abs(closing_price - take_profit)
                logger.info(
                    f"   Цена закрытия {closing_price:.6f} близка к TP {take_profit:.6f} (расстояние: {distance:.6f})"
                )
        elif reason == "stop_loss":
            from utils.settings_manager import increment_losses_in_row, get_max_losses_in_row, set_trading_paused, get_pause_after_losses
            
            # Інкрементуємо лічильник збитків
            losses = increment_losses_in_row()
            max_losses = get_max_losses_in_row()
            
            logger.warning(f"Позиція по {pair} ({direction}) закрита по стоп-лоссу. Збитків підряд: {losses}/{max_losses}")
            
            if losses >= max_losses:
                pause_minutes = get_pause_after_losses()
                logger.warning(f"⛔ Досягнуто ліміт збитків підряд ({losses}). Пауза торгівлі на {pause_minutes} хв.")
                set_trading_paused(pause_minutes)
                
                try:
                    await notify_user(
                        f"⛔ <b>Торгівлю призупинено!</b>\n"
                        f"Досягнуто ліміт {losses} збиткових угод підряд.\n"
                        f"Пауза: {pause_minutes} хв."
                    )
                except:
                    pass
            
            if closing_price and stop_loss:
                distance = abs(closing_price - stop_loss)
                from utils.logger import position_closed_by_stop

                position_closed_by_stop(pair, direction, closing_price, stop_loss, distance)
            else:
                logger.warning(f"Позиція по {pair} ({direction}) закрита по стоп-лоссу")
        elif reason == "manual_close":
            if closing_price and stop_loss:
                distance = abs(closing_price - stop_loss)
                from utils.logger import position_closed_manually

                position_closed_manually(pair, direction, closing_price, stop_loss, distance)
            else:
                logger.info(f"Позиція по {pair} ({direction}) була закрита вручну")
        else:
            logger.info(f"Позиція по {pair} закрита з невідомою причиною: {reason}")

        if reason == "stop_loss" and retry_count < max_retries:
            from utils.logger import reentry_allowed

            reentry_allowed(pair, f"Сработал стоп, попытка {retry_count + 1}/{max_retries}")

            signal_level = meta.get("signal_level") or entry_price
            if signal_level:
                try:
                    asyncio.create_task(self._start_monitor_pinbar_task(pair, signal_level, direction, retry_count=retry_count + 1))
                except Exception as exc:
                    logger.error(f"[{pair}] Помилка перезапуску: {exc}")
        else:
            from utils.logger import reentry_blocked

            if reason == "stop_loss":
                reentry_blocked(pair, f"Достигнут максимум спроб ({retry_count}/{max_retries})")
            else:
                reentry_blocked(pair, f"Не стоп-лосс (причина: {reason})")

        if pair in self._bot_owned_positions:
            self._bot_owned_positions.discard(pair)
        order_id = self._pair_to_order.get(pair)
        if order_id and order_id in self._bot_owned_order_ids:
            self._bot_owned_order_ids.discard(order_id)
            logger.debug(f"Удален orderId {order_id} из bot_owned")
        if reason == "manual_close":
            self._manually_closed_positions.add(pair)
            logger.info(f"👤 Вручну закрита позиція {pair} відслідковується для запобігання переоткриття")
        meta["finalized"] = True
        order_id = self._pair_to_order.pop(pair, None)
        if order_id:
            self._position_meta.pop(order_id, None)

    async def aclose(self):
        await self.close()

    async def __aenter__(self):
        await self._init_db()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    def __del__(self):
        if getattr(self, "_session", None) and not self._session.closed:
            logger.warning(
                "SignalHandler собран сборщиком мусора с открытой сессией aiohttp; вызовите await close() явно."
            )

    async def _signed_request(self, method, endpoint, params=None):
        logger.debug(f"[API] _signed_request: {method} {endpoint}")
        if self._time_offset == 0:
            settings = get_settings()
            stored_offset = settings.get("timestamp_offset", 0)
            if stored_offset:
                try:
                    self._time_offset = int(stored_offset)
                except (ValueError, TypeError):
                    self._time_offset = 0
            if self._time_offset == 0:
                logger.info(f"[API] Отримую server time offset...")
                self._time_offset = await self.get_server_time_offset()
                logger.info(f"[API] Server time offset: {self._time_offset}")
        url = f"{self.base_url}{endpoint}"
        params = params or {}
        sorted_params = {k: params[k] for k in sorted(params)}
        timestamp = str(int(time.time() * 1000) + self._time_offset)
        if method.upper() == "GET":
            payload = urllib.parse.urlencode(sorted_params)
        else:
            payload = json.dumps(sorted_params, separators=(",", ":"))
        pre_sign = f"{timestamp}{self.api_key}{self.recv_window}{payload}"
        signature = hmac.new(self.api_secret.encode("utf-8"), pre_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": self.recv_window,
        }
        if method.upper() == "POST":
            headers["Content-Type"] = "application/json"
        max_retries = 5
        base_delay = 0.6
        for attempt in range(1, max_retries + 1):
            try:
                async with self._sem:
                    session = await self._get_session()

                    if session.closed:
                        logger.warning(f"Сесія закрита, створюємо нову...")
                        self._session = None
                        session = await self._get_session()

                    if method.upper() == "GET":
                        resp = await session.get(url, params=sorted_params, headers=headers)
                    else:
                        resp = await session.post(url, data=payload, headers=headers)
                async with resp:
                    status = resp.status
                    text = await resp.text()
                if status == 404:
                    return {"retCode": 404, "retMsg": "HTTP 404 Not Found"}
                if not text.strip():
                    logger.warning(
                        f"Порожня відповідь від {method} {endpoint} (статус {status}) спроба {attempt}/{max_retries}"
                    )
                    if attempt == max_retries:
                        logger.error(f"Порожня відповідь {method} {endpoint} після {attempt} спроб")
                        return None

                    await asyncio.sleep(base_delay * attempt)
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    if attempt == max_retries:
                        logger.error(f"Невалідний JSON {method} {endpoint}: {text[:120]}")
                        return None
                    await asyncio.sleep(base_delay * attempt)
                    continue
                if data.get("retCode") == 10002:
                    self._time_offset = await self.get_server_time_offset()
                    if attempt < max_retries:
                        await asyncio.sleep(base_delay * attempt)
                        continue
                if resp.status != 200 and attempt == max_retries:
                    logger.error(f"HTTP {resp.status} {method} {endpoint}: {data}")
                return data
            except aiohttp.ClientConnectionError as e:
                logger.warning(f"Connection error {method} {endpoint} (attempt {attempt}/{max_retries}): {e}")

                self._session = None
                if attempt == max_retries:
                    logger.error(f"Помилка з'єднання {method} {endpoint}: {e}")
                    return None
                await asyncio.sleep(base_delay * attempt)
            except aiohttp.ClientError as e:
                logger.warning(f"Client error {method} {endpoint} (attempt {attempt}/{max_retries}): {e}")
                if attempt == max_retries:
                    logger.error(f"Помилка клієнта {method} {endpoint}: {e}")
                    return None
                await asyncio.sleep(base_delay * attempt)
            except Exception as e:
                logger.warning(f"Unexpected error {method} {endpoint} (attempt {attempt}/{max_retries}): {e}")
                if attempt == max_retries:
                    logger.error(f"Непередбачена помилка {method} {endpoint}: {e}")
                    return None
                await asyncio.sleep(base_delay * attempt)
        return None

    async def get_usdt_balance(self):
        result = await self._signed_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if result and result.get("retCode") == 0:
            coins = result.get("result", {}).get("list", [{}])[0].get("coin", [])
            for coin in coins:
                if coin.get("coin") == "USDT":
                    usdt_info = coin
                    equity = float(usdt_info.get("equity", 0) or 0)
                    wallet_balance = float(usdt_info.get("walletBalance", 0) or 0)
                    available = wallet_balance - float(usdt_info.get("totalOrderIM", 0) or 0)
                    now = time.time()
                    if now - self._last_balance_log_time >= self._balance_log_interval:
                        # logger.info(f"Баланс USDT:")
                        # logger.info(f"   - Кошелек: {wallet_balance:.2f} USDT")
                        # logger.info(f"   - В ордерах: {float(usdt_info.get('totalOrderIM', 0)):.2f} USDT")
                        # logger.info(f"   - Доступно: {available:.2f} USDT")
                        self._last_balance_log_time = now
                    else:
                        logger.debug(
                            f"USDT balance (suppressed INFO): wallet={wallet_balance:.2f} available={available:.2f} equity={equity:.2f}"
                        )
                    return {"equity": equity, "available": available, "wallet": wallet_balance}
        logger.debug(f"Не вдалося отримати баланс USDT: {result}")
        return {"equity": 0, "available": 0, "wallet": 0}

    async def display_usdt_balance(self):
        balance = await self.get_usdt_balance()
        equity = balance["equity"]
        available = balance["available"]
        # logger.info(f"Баланс USDT:")
        # logger.info(f"   - Полный equity: {equity:.2f} USDT")
        # logger.info(f"   - Доступний для виведення: {available:.2f} USDT")
        # if equity > 0:
        #     logger.info(f"   - Використано: {(equity - available):.2f} USDT")

    async def _get_symbol_specs(self, symbol):
        if symbol in self._symbol_specs:
            return self._symbol_specs[symbol]
        url = f"{self.base_url}/v5/market/instruments-info?category=linear&symbol={symbol}"
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url) as response:
                text = await response.text()
                if not text:
                    raise ValueError(f"Пустая інформація об інструмент для {symbol}")
                data = json.loads(text)
                if data.get("retCode") != 0:
                    raise ValueError(f"Ошибка інформації об інструмент для {symbol}: {data}")
                items = data.get("result", {}).get("list", [])
                if not items:
                    raise ValueError(f"Нет інформації об інструмент для {symbol}")
                lot = items[0].get("lotSizeFilter", {})
                price_filter = items[0].get("priceFilter", {})
                leverage = items[0].get("leverageFilter", {})
                specs = {
                    "qty_step": float(lot.get("qtyStep", 0.0001) or 0.0001),
                    "min_qty": float(lot.get("minOrderQty", lot.get("qtyStep", 0.0001)) or 0.0001),
                    "price_step": float(price_filter.get("tickSize", 0.5) or 0.5),
                    "min_leverage": float(leverage.get("minLeverage", 1) or 1),
                    "max_leverage": 100.0,
                }
                self._symbol_specs[symbol] = specs
                return specs

    async def _set_leverage(self, symbol, leverage):
        leverage = max(1.0, leverage)
        specs = await self._get_symbol_specs(symbol)
        leverage = min(max(leverage, specs["min_leverage"]), specs["max_leverage"])
        params = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": f"{leverage:.2f}",
            "sellLeverage": f"{leverage:.2f}",
        }
        result = await self._signed_request("POST", "/v5/position/set-leverage", params)
        if not result or result.get("retCode") != 0:
            logger.warning(f"Не вдалося встановити кредитне плече {leverage:.2f} для {symbol}: {result}")

    @staticmethod
    def _quantize(value, step):
        if step <= 0:
            return value
        step_dec = Decimal(str(step))
        value_dec = Decimal(str(value))
        steps = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
        return float(steps * step_dec)

    @staticmethod
    def _quantize_price(value, step, rounding=ROUND_DOWN):
        if step <= 0:
            return value
        step_dec = Decimal(str(step))
        value_dec = Decimal(str(value))
        steps = (value_dec / step_dec).to_integral_value(rounding=rounding)
        return float(steps * step_dec)

    @staticmethod
    def _interval_to_seconds(interval):
        """Convert interval to seconds - accepts both display and internal formats"""
        if not interval:
            interval = "1m"

        interval = str(interval).strip().lower()

        display_to_internal = {
            "1m": "1",
            "3m": "3",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "2h": "120",
            "4h": "240",
            "6h": "360",
            "12h": "720",
            "1d": "D",
            "1w": "W",
            "1mon": "M",
        }

        internal_value = display_to_internal.get(interval, interval)

        mapping = {
            "1": 60,
            "3": 180,
            "5": 300,
            "15": 900,
            "30": 1800,
            "60": 3600,
            "120": 7200,
            "240": 14400,
            "360": 21600,
            "720": 43200,
            "d": 86400,
            "w": 604800,
            "m": 2592000,
        }
        return mapping.get(internal_value.lower(), 60)

    def _parse_timeframe_for_api(self, timeframe):
        """Parse timeframe from display format (1m, 5m, 1h) to API format (1, 5, 60)"""
        if not timeframe:
            return "1"

        timeframe = str(timeframe).strip().lower()

        display_to_api = {
            "1m": "1",
            "3m": "3",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "2h": "120",
            "4h": "240",
            "6h": "360",
            "12h": "720",
            "1d": "D",
            "1w": "W",
            "1mon": "M",
        }

        result = display_to_api.get(timeframe)
        if result:
            return result

        valid_api = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "d", "w", "m"}
        if timeframe in valid_api:
            return timeframe

        return "1"

    async def _calculate_extremum_stop_loss(self, symbol, direction):
        try:

            try:
                stop_behind_offset = CONFIG.get("STOP_BEHIND_EXTREMUM_OFFSET", 0.002)
            except:
                stop_behind_offset = 0.002

            klines = await self.get_klines(symbol, "15", limit=20)
            if not klines:
                return None
            df = pd.DataFrame(klines)
            df.columns = ["timestamp", "open", "high", "low", "close", "volume", "turnover"]
            for col in ["high", "low", "close", "open"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            if direction == "long":
                recent_low = df["low"].min()

                stop_price = recent_low * (1 - stop_behind_offset)
                logger.info(f"📍 Розрахунок SL для LONG:")
                logger.info(f"   - Екстремум (LOW): {recent_low:.8f}")
                logger.info(f"   - Відступ: {stop_behind_offset * 100:.2f}%")
                logger.info(f"   - Стоп ЗА екстремумом: {stop_price:.8f}")
                return float(stop_price)
            else:
                recent_high = df["high"].max()

                stop_price = recent_high * (1 + stop_behind_offset)
                logger.info(f"📍 Розрахунок SL для SHORT:")
                logger.info(f"   - Екстремум (HIGH): {recent_high:.8f}")
                logger.info(f"   - Відступ: {stop_behind_offset * 100:.2f}%")
                logger.info(f"   - Стоп ЗА екстремумом: {stop_price:.8f}")
                return float(stop_price)
        except Exception as e:
            logger.error(f"_calculate_extremum_stop_loss error for {symbol}: {e}")
            return None

    async def _is_drawdown_blocked(self):
        from utils.settings_manager import (
            check_and_update_drawdown,
            is_drawdown_protection_active,
        )

        if is_drawdown_protection_active():
            logger.warning("Drawdown protection active: new orders disabled. Use START to re-enable.")
            return True
        try:
            bal = await self.get_usdt_balance()
            equity = float(bal.get("equity", 0) or 0)
            if equity > 0:
                if check_and_update_drawdown(equity):
                    return True
        except Exception as e:
            logger.error(f"Error checking drawdown: {e}")
            return False
        if is_trading_paused():
            logger.warning("Trading paused due to consecutive losses.")
            return True
        return False

    async def place_limit_order(
        self, pair, direction, price, quantity_usdt, stop_loss=None, take_profit=None, reduce_only=False, metadata=None
    ):
        if await self._is_drawdown_blocked():
            logger.warning(f"Пропускаем размещення лимитного ордера для {pair}: защита по просадке")
            return None

        settings = get_settings()
        max_trades = settings.get("max_open_trades")
        if max_trades is not None:
            max_trades = int(max_trades)
            active_count = await self.get_active_positions_count()

            try:
                result = await self._signed_request("GET", "/v5/order/realtime", {"category": "linear"})
                if result and result.get("retCode") == 0:
                    active_orders = result.get("result", {}).get("list", [])

                    conditional_count = len(
                        [o for o in active_orders if o.get("orderStatus") in ["New", "PartiallyFilled", "Untriggered"]]
                    )
                else:
                    conditional_count = 0
            except Exception:
                conditional_count = 0

            total_active = active_count + conditional_count

            logger.info(f"📊 Перевірка ліміту угод для {pair}:")
            logger.info(f"   - Активні позиції: {active_count}")
            logger.info(f"   - Умовні ордери: {conditional_count}")
            logger.info(f"   - Всього відкрито: {total_active}")
            logger.info(f"   - Максимум дозволено: {max_trades}")

            if total_active >= max_trades:
                logger.warning(f"⛔ ПЕРЕВИЩЕНО ЛІМІТ УГОД!")
                logger.warning(f"   Відкрито: {total_active} (позицій: {active_count} + ордерів: {conditional_count})")
                logger.warning(f"   Максимум: {max_trades}")
                logger.warning(f"   Пропускаємо відкриття {pair}")

                try:
                    await notify_user(
                        f"⛔ ЛІМІТ УГОД ПЕРЕВИЩЕНО!\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Спроба відкриття: {pair}\n"
                        f"Позицій: {active_count}\n"
                        f"Умовних ордерів: {conditional_count}\n"
                        f"Всього: {total_active}\n"
                        f"Максимум: {max_trades}\n"
                        f"\n❌ Угода НЕ відкрита"
                    )
                except Exception:
                    pass

                return None

        position = await self._take_profit_helper.get_position(pair)
        size = 0.0
        if position:
            size = float(position.get("size", 0) or 0)
        if size > 0:
            logger.info(f"Позиція по {pair} вже відкрита (розмір {size}), пропускаємо відкриття нової.")
            return None
        specs = await self._get_symbol_specs(pair)
        price_step = specs.get("price_step", 0.0)
        stop_loss_float = float(stop_loss) if stop_loss is not None else None
        take_profit_float = float(take_profit) if take_profit is not None else None
        if stop_loss_float is not None:
            if direction == "long":
                if stop_loss_float >= price:
                    logger.warning(
                        f"ОШИБКА: Стоп-лосс {stop_loss_float} >= ціни входу {price} для LONG. Корректируем..."
                    )
                    stop_loss_float = price * 0.993
            else:
                if stop_loss_float <= price:
                    logger.warning(
                        f"ОШИБКА: Стоп-лосс {stop_loss_float} <= ціни входу {price} для SHORT. Корректируем..."
                    )
                    stop_loss_float = price * 1.007
        price_rounding = ROUND_DOWN if direction == "long" else ROUND_UP
        price = self._quantize_price(price, price_step, price_rounding)
        if stop_loss_float is not None:
            sl_rounding = ROUND_DOWN if direction == "long" else ROUND_UP
            stop_loss_float = self._quantize_price(stop_loss_float, price_step, sl_rounding)
            if direction == "long" and stop_loss_float >= price:
                stop_loss_float = self._quantize_price(price * 0.993, price_step, ROUND_DOWN)
                logger.warning(f"Скорректирован SL после квантования: {stop_loss_float}")
            elif direction == "short" and stop_loss_float <= price:
                stop_loss_float = self._quantize_price(price * 1.007, price_step, ROUND_UP)
                logger.warning(f"Скорректирован SL после квантования: {stop_loss_float}")
        if take_profit_float is not None:
            tp_rounding = ROUND_UP if direction == "long" else ROUND_DOWN
            take_profit_float = self._quantize_price(take_profit_float, price_step, tp_rounding)
        sl_percent = None
        tp_percent = None
        if stop_loss_float and price:
            if direction == "long":
                sl_percent = (price - stop_loss_float) / price * 100
            else:
                sl_percent = (stop_loss_float - price) / price * 100
        if take_profit_float and price:
            tp_percent = (
                (take_profit_float - price) / price * 100
                if direction == "long"
                else (price - take_profit_float) / price * 100
            )
        quantity_usdt_value = float(quantity_usdt)
        margin_usdt = quantity_usdt_value
        leverage = 10
        notional_usdt = margin_usdt * leverage
        quantity_asset = self._quantize(notional_usdt / price, specs["qty_step"])
        if quantity_asset < specs["min_qty"]:
            quantity_asset = specs["min_qty"]
        order_value = quantity_asset * price
        price_str = format(price, "f")
        params = {
            "category": "linear",
            "symbol": pair,
            "side": "Buy" if direction == "long" else "Sell",
            "orderType": "Limit",
            "price": price_str,
            "qty": format(quantity_asset, "f"),
            "timeInForce": "GTC",
            "reduceOnly": reduce_only,
            "positionIdx": 0,
        }

        result = await self._signed_request("POST", "/v5/order/create", params)
        if result and result.get("retCode") == 0:
            limit_order_placed(
                direction, pair, price_str, f"{order_value:.2f} USDT @ {leverage}x (маржа {margin_usdt:.2f})"
            )

            if stop_loss_float is not None:
                logger.info(f"SL буде встановлений після відкриття: {stop_loss_float:.8f} ({sl_percent:.2f}%)")
            if take_profit_float is not None:
                logger.info(f"TP буде встановлений після відкриття: {take_profit_float:.8f} ({tp_percent:.2f}%)")

            order_id = result["result"].get("orderId")
            if order_id:
                meta = dict(metadata or {})
                meta.update(
                    {
                        "pair": pair,
                        "direction": direction,
                        "entry_price": price,
                        "stop_loss": stop_loss_float,
                        "take_profit": take_profit_float,
                        "order_value": order_value,
                        "quantity_asset": quantity_asset,
                        "created_at": time.time(),
                        "retry_count": meta.get("retry_count", 0),
                        "limit_order_id": order_id,
                    }
                )
                self._position_meta[order_id] = meta
                self._pair_to_order[pair] = order_id
                self._active_monitors += 1
                asyncio.create_task(self._watch_position_exit(pair))
                asyncio.create_task(self._monitor_limit_order_lifetime(pair, order_id))
            return result["result"]
        else:
            if not result:
                logger.error(f"Не удалось разместить лимитний ордер для {pair}: Нет відповіді від API")
            elif result.get("retCode") != 0:
                ret_code = result.get("retCode")
                ret_msg = result.get("retMsg", "Неизвестная ошибка")
                logger.error(f"Не вдалося розмістити лімітний ордер для {pair}: {ret_msg} (код {ret_code})")
            else:
                logger.error(f"Не вдалося розмістити лімітний ордер для {pair}: Невідомий формат відповіді - {result}")
        return None
    async def cancel_order(self, pair, order_id):
        params = {"category": "linear", "symbol": pair, "orderId": order_id}
        result = await self._signed_request("POST", "/v5/order/cancel", params)
        if result and result.get("retCode") == 0:
            logger.info(f"Order {order_id} cancelled successfully")
            return True
        logger.error(f"Failed to cancel order {order_id}: {result}")
        return False


    async def cancel_all_orders(self, pair, reason=None):
        params = {"category": "linear", "symbol": pair, "settleCoin": "USDT"}
        result = await self._signed_request("POST", "/v5/order/cancel-all", params)
        if result and result.get("retCode") == 0:
            reason_msg = f" (Причина: {reason})" if reason else ""
            logger.info(f"All orders cancelled for {pair}{reason_msg}")
            return True
        logger.warning(f"Failed to cancel all orders for {pair}: {result}")
        return False


    async def place_trigger_order(
        self, pair, direction, trigger_price, quantity_usdt, stop_loss=None, take_profit=None, metadata=None
    ):
        """
        ✅ Розміщує TRIGGER ORDER (Conditional Order) на Bybit
        Ордер спрацює коли ціна досягне trigger_price

        Параметри:
        - triggerPrice: ціна активації ордеру
        - triggerDirection: 1 (при зростанні) або 2 (при падінні)
        - orderFilter: "StopOrder" для умовних ордерів
        - orderType: "Market" - після активації виконується як ринковий
        """
        if await self._is_drawdown_blocked():
            logger.warning(f"Пропускаємо trigger order для {pair}: захист по просадці")
            return None

        settings = get_settings()
        max_trades = settings.get("max_open_trades")
        if max_trades is not None:
            max_trades = int(max_trades)
            active_count = await self.get_active_positions_count()

            try:
                result = await self._signed_request("GET", "/v5/order/realtime", {"category": "linear"})
                if result and result.get("retCode") == 0:
                    active_orders = result.get("result", {}).get("list", [])

                    conditional_count = len(
                        [o for o in active_orders if o.get("orderStatus") in ["New", "PartiallyFilled", "Untriggered"]]
                    )
                else:
                    conditional_count = 0
            except Exception:
                conditional_count = 0

            total_active = active_count + conditional_count

            logger.info(f"📊 Перевірка ліміту угод для {pair} (TRIGGER):")
            logger.info(f"   - Активні позиції: {active_count}")
            logger.info(f"   - Умовні ордери: {conditional_count}")
            logger.info(f"   - Всього відкрито: {total_active}")
            logger.info(f"   - Максимум дозволено: {max_trades}")

            if total_active >= max_trades:
                logger.warning(f"⛔ ПЕРЕВИЩЕНО ЛІМІТ УГОД (TRIGGER)!")
                logger.warning(f"   Відкрито: {total_active} (позицій: {active_count} + ордерів: {conditional_count})")
                logger.warning(f"   Максимум: {max_trades}")
                logger.warning(f"   Пропускаємо відкриття TRIGGER для {pair}")

                try:
                    await notify_user(
                        f"⛔ ЛІМІТ УГОД ПЕРЕВИЩЕНО (TRIGGER)!\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Спроба відкриття: {pair}\n"
                        f"Тип: TRIGGER ORDER\n"
                        f"Позицій: {active_count}\n"
                        f"Умовних ордерів: {conditional_count}\n"
                        f"Всього: {total_active}\n"
                        f"Максимум: {max_trades}\n"
                        f"\n❌ Trigger НЕ розміщено"
                    )
                except Exception:
                    pass

                return None

        position = await self._take_profit_helper.get_position(pair)
        size = 0.0
        if position:
            size = float(position.get("size", 0) or 0)
        if size > 0:
            logger.info(f"Позиція по {pair} вже відкрита (розмір {size}), пропускаємо відкриття нової.")
            return None

        specs = await self._get_symbol_specs(pair)
        price_step = specs.get("price_step", 0.0)

        current_price = await self.get_real_time_price(pair)
        if not current_price:
            logger.error(f"Не вдалося отримати поточну ціну для {pair}")
            return None

        trigger_price_float = float(trigger_price)
        trigger_price_quantized = self._quantize_price(trigger_price_float, price_step, ROUND_DOWN)

        # Визначаємо trigger_direction на основі порівняння trigger ціни з поточною
        # 1 = Rising (спрацює коли ціна ЗРОСТЕ до trigger)
        # 2 = Falling (спрацює коли ціна ВПАДЕ до trigger)
        if trigger_price_quantized < current_price:
            trigger_direction = 2  # Falling - ціна має впасти до trigger
            logger.info(f"📉 Trigger НИЖЧЕ поточної: спрацює при ПАДІННІ до {trigger_price_quantized:.8f}")
        else:
            trigger_direction = 1  # Rising - ціна має зрости до trigger  
            logger.info(f"📈 Trigger ВИЩЕ поточної: спрацює при ЗРОСТАННІ до {trigger_price_quantized:.8f}")

        logger.info(f"   🎯 {direction.upper()}: поточна {current_price:.8f} → trigger {trigger_price_quantized:.8f}")

        stop_loss_float = float(stop_loss) if stop_loss is not None else None
        take_profit_float = float(take_profit) if take_profit is not None else None

        if stop_loss_float is not None:
            if direction == "long":
                if stop_loss_float >= trigger_price_quantized:
                    logger.warning(
                        f"⚠ Стоп-лосс {stop_loss_float} >= trigger ціни {trigger_price_quantized} для LONG. Коригуємо..."
                    )
                    stop_loss_float = trigger_price_quantized * 0.993
            else:
                if stop_loss_float <= trigger_price_quantized:
                    logger.warning(
                        f"⚠ Стоп-лосс {stop_loss_float} <= trigger ціни {trigger_price_quantized} для SHORT. Коригуємо..."
                    )
                    stop_loss_float = trigger_price_quantized * 1.007

        if stop_loss_float is not None:
            sl_rounding = ROUND_DOWN if direction == "long" else ROUND_UP
            stop_loss_float = self._quantize_price(stop_loss_float, price_step, sl_rounding)

        if take_profit_float is not None:
            tp_rounding = ROUND_UP if direction == "long" else ROUND_DOWN
            take_profit_float = self._quantize_price(take_profit_float, price_step, tp_rounding)

        quantity_usdt_value = float(quantity_usdt)
        margin_usdt = quantity_usdt_value
        leverage = 10
        notional_usdt = margin_usdt * leverage
        quantity_asset = self._quantize(notional_usdt / trigger_price_quantized, specs["qty_step"])
        if quantity_asset < specs["min_qty"]:
            quantity_asset = specs["min_qty"]

        order_value = quantity_asset * trigger_price_quantized

        # Перевіряємо поточну ціну ще раз перед відправкою ордера
        # щоб уникнути помилки 110093 коли ціна змінилась
        fresh_price = await self.get_real_time_price(pair)
        if fresh_price:
            # Перевіряємо чи trigger_direction все ще валідний
            if trigger_direction == 2:  # Falling
                if trigger_price_quantized >= fresh_price:
                    logger.warning(
                        f"⚠ [{pair}] Ціна змінилась! Trigger={trigger_price_quantized:.8f} >= Поточна={fresh_price:.8f}. "
                        f"Для Falling trigger має бути < current. Пропускаємо."
                    )
                    return None
            else:  # Rising (trigger_direction == 1)
                if trigger_price_quantized <= fresh_price:
                    logger.warning(
                        f"⚠ [{pair}] Ціна змінилась! Trigger={trigger_price_quantized:.8f} <= Поточна={fresh_price:.8f}. "
                        f"Для Rising trigger має бути > current. Пропускаємо."
                    )
                    return None

        params = {
            "category": "linear",
            "symbol": pair,
            "side": "Buy" if direction == "long" else "Sell",
            "orderType": "Market",
            "qty": format(quantity_asset, "f"),
            "triggerPrice": format(trigger_price_quantized, "f"),
            "triggerDirection": trigger_direction,
            "triggerBy": "LastPrice",
            "orderFilter": "StopOrder",
            "reduceOnly": False,
            "positionIdx": 0,
        }

        logger.info(f"🎯 TRIGGER ORDER: {pair} {direction.upper()}")
        logger.info(f"   Поточна ціна: {fresh_price or current_price:.8f}")
        logger.info(f"   Trigger ціна: {trigger_price_quantized:.8f}")
        logger.info(
            f"   Trigger direction: {trigger_direction} ({'зростання' if trigger_direction == 1 else 'падіння'})"
        )
        logger.info(f"   Розмір: {quantity_asset} ({order_value:.2f} USDT @ {leverage}x)")
        if stop_loss_float:
            logger.info(f"   Stop Loss: {stop_loss_float:.8f}")
        if take_profit_float:
            logger.info(f"   Take Profit: {take_profit_float:.8f}")

        result = await self._signed_request("POST", "/v5/order/create", params)

        if result and result.get("retCode") == 0:
            logger.info(f"✅ TRIGGER ORDER розміщено: {pair} {direction.upper()}")

            order_id = result["result"].get("orderId")
            if order_id:
                meta = dict(metadata or {})
                meta.update(
                    {
                        "pair": pair,
                        "direction": direction,
                        "trigger_price": trigger_price_quantized,
                        "entry_price": trigger_price_quantized,
                        "stop_loss": stop_loss_float,
                        "take_profit": take_profit_float,
                        "order_value": order_value,
                        "quantity_asset": quantity_asset,
                        "created_at": time.time(),
                        "retry_count": meta.get("retry_count", 0),
                        "trigger_order_id": order_id,
                        "order_type": "trigger",
                        "trigger_direction": trigger_direction,
                    }
                )

                self._position_meta[order_id] = meta
                self._pair_to_order[pair] = order_id
                self._bot_owned_positions.add(pair)
                self._bot_owned_order_ids.add(order_id)

                self._active_monitors += 1
                asyncio.create_task(self._watch_trigger_order(pair, order_id))

                try:
                    msg = (
                        f"🎯 TRIGGER ORDER РОЗМІЩЕНО\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Монета: {pair}\n"
                        f"Напрямок: {direction.upper()}\n"
                        f"Поточна ціна: {current_price:.8f}\n"
                        f"Trigger: {trigger_price_quantized:.8f}\n"
                    )
                    if stop_loss_float:
                        msg += f"Stop Loss: {stop_loss_float:.8f}\n"
                    if take_profit_float:
                        msg += f"Take Profit: {take_profit_float:.8f}\n"
                    strategy_name = meta.get("strategy", "Conditional")
                    msg += f"Тип: Conditional ({strategy_name})"

                    await notify_user(msg)
                except Exception as e:
                    logger.error(f"Помилка відправки повідомлення: {e}")

            return result["result"]
        else:
            if not result:
                logger.error(f"❌ Не вдалося розмістити trigger order для {pair}: Немає відповіді від API")
            elif result.get("retCode") != 0:
                ret_code = result.get("retCode")
                ret_msg = result.get("retMsg", "Невідома помилка")
                logger.error(f"❌ Не вдалося розмістити trigger order для {pair}: {ret_msg} (код {ret_code})")
            else:
                logger.error(f"❌ Не вдалося розмістити trigger order для {pair}: Невідомий формат відповіді")

        return None

    async def _watch_trigger_order(self, pair, order_id, poll_interval=5):
        """
        Моніторинг trigger order до його активації та подальше відстеження позиції
        """
        try:
            meta = self._position_meta.get(order_id)
            if not meta:
                logger.error(f"Немає метаданих для trigger order {order_id}")
                return

            trigger_price = meta.get("trigger_price")
            direction = meta.get("direction")

            logger.info(f"📡 Моніторинг trigger order для {pair}")
            logger.info(f"   Order ID: {order_id}")
            logger.info(f"   Trigger: {trigger_price:.8f}")
            logger.info(f"   Direction: {direction.upper()}")

            while True:
                await asyncio.sleep(poll_interval)

                if not meta or meta.get("finalized"):
                    logger.info(f"Моніторинг trigger order {pair} завершено")
                    return

                try:

                    params = {"category": "linear", "symbol": pair, "orderId": order_id}

                    result = await self._signed_request("GET", "/v5/order/realtime", params)

                    if not result or result.get("retCode") != 0:
                        logger.warning(f"Не вдалося отримати статус trigger order {order_id}")
                        continue

                    orders = result.get("result", {}).get("list", [])
                    if not orders:

                        logger.info(f"🔍 Trigger order {order_id} не знайдено в активних - перевіряємо позицію...")

                        position = await self._take_profit_helper.get_position(pair)
                        if position and float(position.get("size", 0) or 0) > 0:
                            logger.info(f"✅ Trigger order {pair} АКТИВОВАНО - позиція відкрита!")

                            actual_entry = float(position.get("avgPrice", 0) or 0)
                            meta["entry_price"] = actual_entry
                            meta["trigger_activated"] = True
                            meta["activated_at"] = time.time()

                            logger.info(f"   Фактична ціна входу: {actual_entry:.8f}")

                            stop_loss = meta.get("stop_loss")
                            if stop_loss:
                                logger.info(f"🛡 Встановлення SL: {stop_loss:.8f}")
                                for attempt in range(MAX_SL_ATTEMPTS):
                                    if attempt > 0:
                                        await asyncio.sleep(1)
                                    success = await self.set_stop_loss(pair, stop_loss, direction)
                                    if success:
                                        meta["sl_set"] = True
                                        logger.info(f"✅ SL встановлено після активації trigger")
                                        break

                            try:
                                msg = (
                                    f"✅ TRIGGER АКТИВОВАНО\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"Монета: {pair}\n"
                                    f"Напрямок: {direction.upper()}\n"
                                    f"Trigger: {trigger_price:.8f}\n"
                                    f"Вхід: {actual_entry:.8f}\n"
                                )
                                if stop_loss:
                                    msg += f"Stop Loss: {stop_loss:.8f}\n"
                                msg += f"Статус: Позиція відкрита"

                                await notify_user(msg)
                            except Exception as e:
                                logger.error(f"Помилка відправки повідомлення: {e}")

                            try:
                                rsi_cfg = await self._get_rsi_settings_db()
                                rsi_low = float(rsi_cfg.get("rsi_low", 30))
                                rsi_high = float(rsi_cfg.get("rsi_high", 70))
                                rsi_period = int(rsi_cfg.get("rsi_period", 14))
                                rsi_interval = "1"

                                async def _on_rsi_exit(sym, dirn, reason, context):
                                    logger.info(f"RSI TP для trigger order {sym}: {reason}")

                                logger.info(f"🎯 Запуск RSI трекера для {pair}")
                                asyncio.create_task(
                                    self._take_profit_helper.track_position_rsi(
                                        pair,
                                        direction,
                                        on_exit=_on_rsi_exit,
                                        rsi_high_override=rsi_high,
                                        rsi_low_override=rsi_low,
                                        rsi_period_override=rsi_period,
                                        rsi_interval_override=rsi_interval,
                                        poll_seconds=max(15, int(self._interval_to_seconds(rsi_interval) // 2)),
                                    )
                                )
                                meta["rsi_tracker_started"] = True
                            except Exception as e:
                                logger.error(f"Помилка запуску RSI трекера: {e}")

                            asyncio.create_task(self._watch_position_exit(pair))
                            return
                        else:

                            logger.warning(f"⚠ Trigger order {order_id} для {pair} не активовано - статус невідомий")
                            meta["finalized"] = True
                            return
                    else:
                        order = orders[0]
                        order_status = order.get("orderStatus")

                        logger.debug(f"Trigger order {pair} статус: {order_status}")

                        if order_status in ["New", "Untriggered"]:

                            current_price = await self.get_real_time_price(pair)
                            if current_price:
                                if direction == "long":
                                    distance = current_price - trigger_price
                                    logger.debug(
                                        f"LONG trigger: поточна {current_price:.8f}, trigger {trigger_price:.8f}, відстань: {distance:.8f}"
                                    )
                                else:
                                    distance = trigger_price - current_price
                                    logger.debug(
                                        f"SHORT trigger: поточна {current_price:.8f}, trigger {trigger_price:.8f}, відстань: {distance:.8f}"
                                    )
                            continue
                        elif order_status in ["Filled", "PartiallyFilled"]:

                            logger.info(f"✅ Trigger order {pair} виконано зі статусом {order_status}")

                            position = await self._take_profit_helper.get_position(pair)
                            if position and float(position.get("size", 0) or 0) > 0:
                                logger.info(f"✅ Позиція по {pair} підтверджена!")

                                actual_entry = float(position.get("avgPrice", 0) or 0)
                                meta["entry_price"] = actual_entry
                                meta["trigger_activated"] = True
                                meta["activated_at"] = time.time()

                                logger.info(f"   Фактична ціна входу: {actual_entry:.8f}")

                                stop_loss = meta.get("stop_loss")
                                if stop_loss:
                                    logger.info(f"🛡 Встановлення SL: {stop_loss:.8f}")
                                    for attempt in range(MAX_SL_ATTEMPTS):
                                        if attempt > 0:
                                            await asyncio.sleep(1)
                                        success = await self.set_stop_loss(pair, stop_loss, direction)
                                        if success:
                                            meta["sl_set"] = True
                                            logger.info(f"✅ SL встановлено після активації trigger")
                                            break

                                try:
                                    msg = (
                                        f"✅ TRIGGER АКТИВОВАНО\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"Монета: {pair}\n"
                                        f"Напрямок: {direction.upper()}\n"
                                        f"Trigger: {trigger_price:.8f}\n"
                                        f"Вхід: {actual_entry:.8f}\n"
                                    )
                                    if stop_loss:
                                        msg += f"Stop Loss: {stop_loss:.8f}\n"
                                    msg += f"Статус: Позиція відкрита"

                                    await notify_user(msg)
                                except Exception as e:
                                    logger.error(f"Помилка відправки повідомлення: {e}")

                                try:
                                    rsi_cfg = await self._get_rsi_settings_db()
                                    rsi_low = float(rsi_cfg.get("rsi_low", 30))
                                    rsi_high = float(rsi_cfg.get("rsi_high", 70))
                                    rsi_period = int(rsi_cfg.get("rsi_period", 14))
                                    rsi_interval = "1"

                                    async def _on_rsi_exit(sym, dirn, reason, context):
                                        logger.info(f"RSI TP для trigger order {sym}: {reason}")

                                    logger.info(f"🎯 Запуск RSI трекера для {pair}")
                                    asyncio.create_task(
                                        self._take_profit_helper.track_position_rsi(
                                            pair,
                                            direction,
                                            on_exit=_on_rsi_exit,
                                            rsi_high_override=rsi_high,
                                            rsi_low_override=rsi_low,
                                            rsi_period_override=rsi_period,
                                            rsi_interval_override=rsi_interval,
                                            poll_seconds=max(15, int(self._interval_to_seconds(rsi_interval) // 2)),
                                        )
                                    )
                                    meta["rsi_tracker_started"] = True
                                except Exception as e:
                                    logger.error(f"Помилка запуску RSI трекера: {e}")

                                asyncio.create_task(self._watch_position_exit(pair))
                                return
                            else:
                                logger.warning(f"⚠ Trigger order {pair} Filled, але позиція не знайдена. Чекаємо...")
                                await asyncio.sleep(1)
                                continue
                        elif order_status in ["Cancelled", "Rejected"]:
                            logger.warning(f"⚠ Trigger order {pair} скасовано зі статусом {order_status}")
                            meta["finalized"] = True
                            return

                except Exception as e:
                    logger.error(f"Помилка моніторингу trigger order {pair}: {e}")
                    await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.info(f"Моніторинг trigger order {pair} скасовано")
        except Exception as e:
            logger.error(f"Критична помилка моніторингу trigger order {pair}: {e}")
            import traceback

            logger.error(traceback.format_exc())
        finally:
            self._active_monitors -= 1

    async def _wait_and_place_limit(
        self,
        pair,
        direction,
        target_price,
        quantity_usdt,
        stop_loss_price,
        take_profit_price,
        metadata,
        timeout_minutes=60,
        poll_interval=1.0,
    ):
        try:
            deadline = time.time() + max(60, int(timeout_minutes) * 60)
            logger.info(
                f"Waiting for {pair} to reach {target_price} before placing limit ({direction}), timeout {timeout_minutes}m"
            )
            while time.time() <= deadline:
                cur = await self.get_real_time_price(pair)
                if cur is None:
                    await asyncio.sleep(poll_interval)
                    continue
                if (direction == "long" and cur <= float(target_price)) or (
                    direction == "short" and cur >= float(target_price)
                ):
                    try:
                        specs = await self._get_symbol_specs(pair)
                        price_step = specs.get("price_step", 0.0)
                        rounding = ROUND_DOWN if direction == "long" else ROUND_UP
                        entry_price = self._quantize_price(float(target_price), price_step, rounding)
                        logger.info(f"Target reached for {pair} (cur={cur:.8f}) — placing limit @ {entry_price}")
                        result = await self.place_limit_order(
                            pair=pair,
                            direction=direction,
                            price=entry_price,
                            quantity_usdt=quantity_usdt,
                            stop_loss=stop_loss_price,
                            take_profit=take_profit_price,
                            metadata=metadata,
                        )
                        if result:
                            logger.info(f"Placed delayed limit for {pair} @ {entry_price}")
                        else:
                            logger.error(f"Failed to place delayed limit for {pair} @ {entry_price}")
                        return result
                    except Exception as e:
                        logger.error(f"Error placing delayed limit for {pair}: {e}")
                        return None
                await asyncio.sleep(poll_interval)
            logger.warning(
                f"Timeout waiting for {pair} to reach {target_price} (gave up after {timeout_minutes} minutes)"
            )
            return None
        except asyncio.CancelledError:
            logger.info(f"Waiting task for {pair} cancelled")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in _wait_and_place_limit for {pair}: {e}")
            return None

    async def place_signal_limit_order(self, pair, direction, entry_price, channel_id=None):
        if await self._is_drawdown_blocked():
            logger.warning(f"Пропускаем сигнальний лимит для {pair}: защита по просадке")
            return None
        direction = (direction or "").lower()
        if direction not in ("long", "short"):
            logger.error(f"Неподдерживаемая сторона '{direction}' для сигнала на {pair}")
            return None

        if not is_bot_active():
            logger.warning(f"Бот остановлен. Игнорируем сигнал для {pair}")
            return None

        if is_blacklisted(pair):
            logger.warning(f"Символ {pair} в черном списке. Igнорируем сигнал")
            return None

        if pair in self._manually_closed_positions:
            self._manually_closed_positions.remove(pair)
            logger.info(f"Позиція {pair} була закрита вручну, але буде знову відкрита на основі нового сигналу")

        try:
            entry_price = float(entry_price)
        except (TypeError, ValueError):
            logger.error(f"Невалидна цена входа '{entry_price}' для {pair}")
            return None

        if channel_id:
            strategy = get_strategy_for_channel(channel_id)
        else:
            from trading.strategies import QuantumPremium2Strategy

            strategy = QuantumPremium2Strategy(-1002956255805)

        logger.info(f"Стратегия для {pair}: {strategy.name}")
        logger.info(f"   - Канал: {channel_id}")
        logger.info(f"   - Точна цена з каналу: {entry_price}")
        logger.info(f"   - Направлення: {direction.upper()}")

        try:
            specs = await self._get_symbol_specs(pair)
        except (ValueError, Exception) as e:
            error_msg = str(e)
            if "symbol invalid" in error_msg.lower() or "retCode': 10001" in error_msg:
                logger.error(f"❌ Символ {pair} НЕ ІСНУЄ на Bybit! Пропускаю сигнал.")
                logger.error(f"   Деталі помилки: {error_msg}")
                try:
                    await notify_user(
                        f"❌ НЕВАЛІДНИЙ СИМВОЛ\\n"
                        f"━━━━━━━━━━━━━━━━━━\\n"
                        f"🪙 {pair}\\n"
                        f"📍 Напрямок: {direction.upper()}\\n"
                        f"💰 Ціна: {entry_price:.8f}\\n"
                        f"\\n⚠️ Цей символ не існує на Bybit!\\n"
                        f"Перевірте назву монети в каналі."
                    )
                except:
                    pass
            else:
                logger.error(f"❌ Помилка отримання специфікацій для {pair}: {e}")
            return None

        price_step = specs.get("price_step", 0.0)
        original_level = entry_price

        if entry_price <= 0:
            logger.error(f"[{pair}] Невалідна ціна входу: {entry_price}")
            return None

        logger.info(f"[{pair}] Запуск моніторингу ({strategy.name})")

        try:
            asyncio.create_task(self._start_monitor_pinbar_task(pair, original_level, direction, strategy=strategy))

            try:
                trade_direction = strategy.get_entry_direction(direction)
                signal_type = "Підтримка" if direction == "long" else "Опір"
                msg = f"🔍 {pair} @ {original_level}\n📊 {strategy.name}\n📍 {signal_type} → {trade_direction.upper()}"
                await notify_user(msg)
            except Exception:
                pass

            logger.info(f"✅ Завдання моніторингу запущено з {strategy.name}")
            return {
                "status": "monitor_started",
                "pair": pair,
                "direction": direction,
                "target_price": entry_price,
            }
        except Exception as e:
            logger.error(f"Не вдалося запустити задачу monitor_pinbar для {pair}: {e}")
            return None

    async def verify_limit_order_capability(self, symbol: str) -> bool:
        try:
            logger.info(f"[STARTUP] verify_limit_order_capability: початок для {symbol}")
            await self._init_db()
            logger.info(f"[STARTUP] verify_limit_order_capability: _init_db завершено")
            logger.info(f"[STARTUP] verify_limit_order_capability: отримую specs...")
            specs = await self._get_symbol_specs(symbol)
            logger.info(f"[STARTUP] verify_limit_order_capability: specs={specs is not None}")
            if not specs:
                return False
            params = {"category": "linear", "symbol": symbol}
            logger.info(f"[STARTUP] verify_limit_order_capability: запит до tickers API...")
            res = await self._signed_request("GET", "/v5/market/tickers", params)
            logger.info(f"[STARTUP] verify_limit_order_capability: відповідь отримано")
            if not res or res.get("retCode") != 0:
                return False
            listing = res.get("result", {}).get("list", [])
            if not listing:
                return False
            try:
                last = float(listing[0].get("lastPrice", 0) or 0)
            except Exception:
                last = 0.0
            if last <= 0:
                return False
            return True
        except Exception as exc:
            logger.error(f"verify_limit_order_capability error for {symbol}: {exc}")
            return False

    async def start_external_trade_monitor(self):
        if getattr(self, "_external_trade_monitor_running", False):
            logger.debug("External trade monitor already running")
            return
        self._external_trade_monitor_running = True
        self._external_monitor_task = asyncio.create_task(self._external_trade_monitor_loop())
        logger.info("External trade monitor started")

    async def stop_external_trade_monitor(self):
        if not getattr(self, "_external_trade_monitor_running", False):
            return
        self._external_trade_monitor_running = False
        task = getattr(self, "_external_monitor_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Error awaiting external monitor task cancellation: {e}")
        logger.info("External trade monitor stopped")

    def _get_pybit_session(self):
        if self._pybit_session is None:
            use_testnet = CONFIG.get("USE_TESTNET", False)
            recv_window = int(CONFIG.get("RECV_WINDOW", "20000"))

            try:
                if use_testnet:
                    self._pybit_session = HTTP(
                        testnet=False,
                        demo=True,
                        api_key=self.api_key,
                        api_secret=self.api_secret,
                        recv_window=recv_window,
                    )
                else:
                    self._pybit_session = HTTP(
                        testnet=False,
                        demo=False,
                        api_key=self.api_key,
                        api_secret=self.api_secret,
                        recv_window=recv_window,
                    )
                logger.debug(f"PyBit session created successfully (recv_window={recv_window})")
            except Exception as e:
                logger.error(f"Failed to create PyBit session: {e}")
                self._pybit_session = None

        return self._pybit_session

    async def _check_positions_via_pybit(self):
        try:
            session = self._get_pybit_session()

            if not session:
                logger.warning("PyBit session not available, falling back to API")
                return []

            try:
                result = await asyncio.to_thread(session.get_positions, category="linear", settleCoin="USDT")
            except Exception as e:

                error_msg = str(e)
                if "Retryable error" in error_msg or "network" in error_msg.lower():
                    logger.warning(f"PyBit network error, recreating session: {e}")
                    self._pybit_session = None
                    return []
                else:
                    logger.error(f"PyBit error: {e}")
                    return []

            if result.get("retCode") != 0:
                logger.error(f"pybit error getting positions: {result.get('retMsg')}")
                return []

            positions = result.get("result", {}).get("list", [])
            active_positions = [p for p in positions if float(p.get("size", 0)) > 0]

            return active_positions

        except Exception as e:
            logger.error(f" Exception in _check_positions_via_pybit: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return []

    async def _external_trade_monitor_loop(self):
        try:
            scan_count = 0
            while getattr(self, "_external_trade_monitor_running", False):
                try:
                    scan_count += 1

                    pybit_positions = await self._check_positions_via_pybit()

                    result = await self._signed_request("GET", "/v5/position/list", {"category": "linear"})
                    api_positions = []
                    if result and result.get("retCode") == 0:
                        api_positions = result.get("result", {}).get("list", [])

                    positions_to_process = pybit_positions if pybit_positions else api_positions
                    active_positions = [p for p in positions_to_process if float(p.get("size", 0)) > 0]

                    current_active_keys = set()
                    for pos in active_positions:
                        pair = pos.get("symbol")
                        position_id = pos.get("positionIdx", "0")
                        pos_key = f"{pair}_{position_id}"
                        current_active_keys.add(pos_key)

                    closed_keys = self._managed_external_positions - current_active_keys
                    for closed_key in closed_keys:
                        logger.info(f"Внешняя позиция {closed_key} закрита - удаляем из управляемих")
                        self._managed_external_positions.discard(closed_key)
                        self._processed_external_positions.discard(closed_key)

                    for pos in active_positions:
                        pair = pos.get("symbol")
                        size = float(pos.get("size", 0) or 0)

                        if size <= 0:
                            continue

                        if pair in self._bot_owned_positions:
                            continue

                        position_id = pos.get("positionIdx", "0")
                        pos_key = f"{pair}_{position_id}"

                        if pos_key in self._processed_external_positions:
                            continue

                        if pos_key in self._managed_external_positions:
                            continue

                        if self._take_profit_helper.is_tracking(pair):
                            self._processed_external_positions.add(pos_key)
                            continue

                        direction = "long" if pos.get("side") == "Buy" else "short"
                        entry_price = float(pos.get("avgPrice", 0) or 0)

                        logger.info(f"===== ОБНАРУЖЕНА НОВАЯ РУЧНАЯ ПОЗИЦИЯ =====")
                        logger.info(f"Монета: {pair}")
                        logger.info(f"Направление: {direction.upper()}")
                        logger.info(f"Размер: {size}")
                        logger.info(f"Цена входа: {entry_price:.6f}")
                        logger.info(f"Плечо: {pos.get('leverage', 'N/A')}x")
                        logger.info(f"Position Key: {pos_key}")
                        logger.info(f"=====")

                        try:
                            from utils.settings_manager import (
                                get_settings as get_settings_fn,
                            )

                            settings = get_settings_fn()
                            tf = settings.get("timeframe", "1m")
                            tf_display = tf

                            tf_api = self._parse_timeframe_for_api(tf)

                            logger.info(f"Получаю свечу для {pair} на TF {tf_display} (API: {tf_api})")

                            candles = await self.get_klines(pair, tf_api, limit=2)

                            if candles and len(candles) >= 2:
                                last_closed = candles[-2]
                                c_high = float(last_closed[2])
                                c_low = float(last_closed[3])

                                logger.info(f"Получена свеча для {pair}")
                                logger.info(f"   TF: {tf_display}")
                                logger.info(f"   High: {c_high:.8f}")
                                logger.info(f"   Low: {c_low:.8f}")

                                from utils.logger import candle_high_low_info

                                candle_high_low_info(pair, tf_display, c_high, c_low)
                            else:
                                logger.warning(f"Нет данных свечей для {pair} на TF {tf_display}")
                        except Exception as e:
                            logger.error(f"Ошибка получения High/Low свечи для {pair}: {e}")
                            import traceback

                            logger.error(traceback.format_exc())

                        from utils.logger import external_position_detect

                        external_position_detect(pair, direction, entry_price, pos.get("leverage", "N/A"))

                        has_sl = False
                        try:
                            stop_loss_str = pos.get("stopLoss")
                            if stop_loss_str and stop_loss_str != "" and stop_loss_str != "0":
                                has_sl = True
                                logger.info(f"Позиція вже має SL: {stop_loss_str}")
                        except Exception:
                            has_sl = False

                        self._processed_external_positions.add(pos_key)

                        if not has_sl and entry_price > 0:
                            try:

                                extremum_data = await self.get_candle_extremum_from_db_timeframe(
                                    pair, direction, pinbar_candle_index=None
                                )

                                if extremum_data:
                                    stop_price = extremum_data["stop_price"]
                                    candle_high = extremum_data["candle_high"]
                                    candle_low = extremum_data["candle_low"]
                                    tf_display = extremum_data["timeframe"]

                                    from utils.logger import candle_high_low_info

                                    candle_high_low_info(pair, tf_display, candle_high, candle_low)

                                    if direction == "long" and stop_price >= entry_price:
                                        logger.error(
                                            f"ПОМИЛКА: SL для LONG ({stop_price:.8f}) >= ціни входу ({entry_price:.8f})"
                                        )
                                        specs = await self._get_symbol_specs(pair)
                                        price_step = specs.get("price_step", 0.00001)
                                        stop_price = self._quantize_price(entry_price * 0.995, price_step, ROUND_DOWN)
                                        logger.warning(f"Використовую резервний SL: {stop_price:.8f}")
                                    elif direction == "short" and stop_price <= entry_price:
                                        logger.error(
                                            f"ПОМИЛКА: SL для SHORT ({stop_price:.8f}) <= ціни входу ({entry_price:.8f})"
                                        )
                                        specs = await self._get_symbol_specs(pair)
                                        price_step = specs.get("price_step", 0.00001)
                                        stop_price = self._quantize_price(entry_price * 1.005, price_step, ROUND_UP)
                                        logger.warning(f"Використовую резервний SL: {stop_price:.8f}")

                                    logger.info(f"Встановлюю SL для {pair} на екстремумі свічки...")
                                    success = await self.set_stop_loss(pair, stop_price, direction)

                                    if success:
                                        extremum_value = candle_low if direction == "long" else candle_high

                                        logger.info(f"✅ SL ВСТАНОВЛЕНО НА ЕКСТРЕМУМІ СВІЧКИ:")
                                        logger.info(
                                            f"   Свічка (TF {tf_display}): H={candle_high:.8f} L={candle_low:.8f}"
                                        )
                                        logger.info(f"   Екстремум ({direction.upper()}): {extremum_value:.8f}")
                                        logger.info(f"   Стоп: {stop_price:.8f}")

                                        self._managed_external_positions.add(pos_key)

                                        try:
                                            extremum_value = candle_low if direction == "long" else candle_high
                                            direction_emoji = "🟢" if direction == "long" else "🔴"

                                            await notify_user(
                                                f"{direction_emoji} СТОП-ЛОСС ВСТАНОВЛЕНО\n"
                                                f"━━━━━━━━━━━━━━━━━━\n"
                                                f"Монета: {pair}\n"
                                                f"Напрямок: {direction.upper()}\n"
                                                f"Вхід: {entry_price:.8f}\n"
                                                f"Екстремум: {extremum_value:.8f}\n"
                                                f"Стоп: {stop_price:.8f}\n"
                                                f"━━━━━━━━━━━━━━━━━━\n"
                                                f"TF: {tf_display}\n"
                                                f"HIGH: {candle_high:.8f}\n"
                                                f"LOW: {candle_low:.8f}"
                                            )
                                        except Exception as e:
                                            logger.error(f"Помилка відправки Telegram повідомлення про SL: {e}")
                                    else:
                                        logger.error(f"Не вдалося встановити SL для {pair}")
                            except Exception as e:
                                logger.error(f"Не удалось встановити SL для {pair}: {e}")
                                import traceback

                                logger.error(traceback.format_exc())
                        elif has_sl:

                            self._managed_external_positions.add(pos_key)

                        try:
                            from utils.settings_manager import (
                                get_settings as get_settings_fn,
                            )

                            settings = get_settings_fn()

                            rsi_cfg = await self._get_rsi_settings_db()
                            rsi_exit_short = rsi_cfg.get("rsi_low", 30)
                            rsi_exit_long = rsi_cfg.get("rsi_high", 70)
                            rsi_length = rsi_cfg.get("rsi_period", 14)
                            rsi_interval = "1"

                            from utils.logger import rsi_settings_from_db

                            rsi_settings_from_db(pair, rsi_exit_short, rsi_exit_long, rsi_length, rsi_interval)

                            logger.info(f"RSI настройки з БД для {pair}:")
                            logger.info(f"   - RSI Low (SHORT exit): {rsi_exit_short}")
                            logger.info(f"   - RSI High (LONG exit): {rsi_exit_long}")
                            logger.info(f"   - RSI Length: {rsi_length}")
                            logger.info(f"   - RSI Interval: {rsi_interval}")

                            if rsi_length is None:
                                rsi_length = 14
                                logger.warning(f"RSI Length was None, using default: {rsi_length}")

                            from analysis.signals import get_rsi

                            if rsi_length is None:
                                rsi_length = 14

                            try:
                                current_rsi, _ = await get_rsi(pair, interval=rsi_interval, period=rsi_length)
                            except Exception as e:
                                logger.error(f"Не удалось запустить RSI трекер для {pair}: {e}")
                                return
                            logger.info(f"Запуск RSI Take-Profit для {pair}:")
                            logger.info(f"   - Напрямок: {direction.upper()}")
                            logger.info(f"   - Поточний RSI: {current_rsi:.2f}")
                            if direction == "long":
                                logger.info(f"   - Поріг закриття (RSI High): {rsi_exit_long}")
                                logger.info(f"   - Закриється при RSI >= {rsi_exit_long}")
                                threshold_text = f"RSI >= {rsi_exit_long}"
                                distance = rsi_exit_long - current_rsi
                            else:
                                logger.info(f"   - Поріг закриття (RSI Low): {rsi_exit_short}")
                                logger.info(f"   - Закриється при RSI <= {rsi_exit_short}")
                                threshold_text = f"RSI <= {rsi_exit_short}"
                                distance = current_rsi - rsi_exit_short

                            logger.info(f"ЗАПУСКАЮ RSI трекер для {pair}...")

                            async def _on_rsi_exit_external(sym, dirn, reason, context):
                                logger.info(
                                    f"RSI TP для ЗОВНІШНЬОЇ позиції {sym}: причина={reason}, контекст={context}"
                                )

                            rsi_interval_seconds = self._interval_to_seconds(rsi_interval)
                            poll_seconds = max(15, int(rsi_interval_seconds // 2))

                            logger.info(f"   - RSI інтервал: {rsi_interval} ({rsi_interval_seconds}s)")
                            logger.info(f"   - Опитування кожні: {poll_seconds}s)")

                            asyncio.create_task(
                                self._take_profit_helper.track_position_rsi(
                                    pair,
                                    direction,
                                    on_exit=_on_rsi_exit_external,
                                    rsi_high_override=rsi_exit_long,
                                    rsi_low_override=rsi_exit_short,
                                    rsi_period_override=rsi_length,
                                    rsi_interval_override=rsi_interval,
                                    poll_seconds=poll_seconds,
                                )
                            )

                            logger.info(f"RSI трекер ЗАПУЩЕНО для {pair}")

                            stop_price_for_msg = None
                            if not has_sl:
                                try:

                                    extremum_data = await self.get_candle_extremum_from_db_timeframe(
                                        pair, direction, pinbar_candle_index=None
                                    )
                                    if extremum_data:
                                        stop_price_for_msg = extremum_data["stop_price"]
                                except Exception:
                                    pass
                            else:

                                try:
                                    stop_price_for_msg = float(pos.get("stopLoss", 0))
                                except Exception:
                                    pass

                            if stop_price_for_msg and stop_price_for_msg > 0:
                                direction_emoji = "🟢" if direction == "long" else "🔴"
                                await notify_user(
                                    f"{direction_emoji} {pair}\n"
                                    f"Відстежуємо RSI\n"
                                    f"Вхід: {entry_price:.6f}\n"
                                    f"Стоп: {stop_price_for_msg:.8f}\n"
                                    f"RSI: {current_rsi:.1f} → {threshold_text}"
                                )
                            else:
                                direction_emoji = "🟢" if direction == "long" else "🔴"
                                await notify_user(
                                    f"{direction_emoji} {pair}\n"
                                    f"Відстежуємо RSI\n"
                                    f"Вхід: {entry_price:.6f}\n"
                                    f"RSI: {current_rsi:.1f} → {threshold_text}"
                                )
                        except Exception as e:
                            logger.error(f"Помилка запуску RSI трекера для {pair}: {e}")
                            import traceback

                            logger.error(traceback.format_exc())

                except Exception as e:
                    logger.error(f" Ошибка при сканировании позиций: {e}")

                await asyncio.sleep(10)

        except asyncio.CancelledError:
            logger.debug("External trade monitor loop отменен")
        finally:
            self._external_trade_monitor_running = False
            logger.debug("External trade monitor loop завершен")

    async def _start_monitor_pinbar_task(self, pair, level, direction, strategy=None, retry_count=0):
        try:
            from analysis.trigger_strategy import monitor_and_trade
            from utils.settings_manager import get_settings

            if not strategy:
                from trading.strategies import QuantumPremium2Strategy

                strategy = QuantumPremium2Strategy(-1)

            settings = get_settings() or {}
            settings["strategy"] = strategy
            settings["channel_id"] = strategy.channel_id
            settings["retry_count"] = retry_count

            await monitor_and_trade(pair, level, direction, settings)
        except Exception as e:
            logger.error(f"[{pair}] Помилка запуску монітора: {e}")
            import traceback

            logger.error(traceback.format_exc())

    async def get_real_time_price(self, pair):
        """Get current price for a symbol"""
        try:
            params = {"category": "linear", "symbol": pair}
            result = await self._signed_request("GET", "/v5/market/tickers", params)
            if result and result.get("retCode") == 0:
                tickers = result.get("result", {}).get("list", [])
                if tickers:
                    return float(tickers[0].get("lastPrice", 0))
            return None
        except Exception as e:
            logger.error(f"Ошибка получения цены в реальном времени для {pair}: {e}")
            return None

    async def get_klines(self, pair, interval, limit=200):
        """Get klines/candles for a symbol"""
        try:
            params = {"category": "linear", "symbol": pair, "interval": interval, "limit": str(limit)}
            result = await self._signed_request("GET", "/v5/market/kline", params)
            if result and result.get("retCode") == 0:

                candles = result["result"]["list"]
                return list(reversed(candles))
            return None
        except Exception as e:
            logger.error(f"Ошибка получения klines для {pair}: {e}")
            return None

    async def set_stop_loss(self, pair, price, direction):
        """Set stop loss for a position"""
        try:
            params = {"category": "linear", "symbol": pair, "stopLoss": str(price), "slTriggerBy": "LastPrice"}
            result = await self._signed_request("POST", "/v5/position/trading-stop", params)
            if result and result.get("retCode") == 0:
                logger.info(f" Стоп-лосс встановлено для {pair}: {price:.8f}")
                return True
            # retCode 34040 = "not modified" - SL вже на цьому рівні, це не помилка
            if result and result.get("retCode") == 34040:
                logger.info(f" Стоп-лосс для {pair} вже встановлено на {price:.8f} (не змінено)")
                return True
            logger.error(f" Не вдалося встановити стоп-лосс для {pair}: {result}")
            return False
        except Exception as e:
            logger.error(f" Помилка встановлення стоп-лосса для {pair}: {e}")
            return False

    async def set_take_profit(self, pair, price, direction):
        """Set take profit for a position"""
        try:
            params = {"category": "linear", "symbol": pair, "takeProfit": str(price), "tpTriggerBy": "LastPrice"}
            result = await self._signed_request("POST", "/v5/position/trading-stop", params)
            if result and result.get("retCode") == 0:
                logger.info(f"Тейк-профіт установлен для {pair} на {price}")
                return True
            logger.error(f"Не удалось установить тейк-профіт для {pair}: {result}")
            return False
        except Exception as e:
            logger.error(f"Ошибка установки тейк-профита для {pair}: {e}")
            return False

    async def get_active_positions_count(self):
        """Get count of active positions"""
        try:
            result = await self._signed_request("GET", "/v5/position/list", {"category": "linear"})
            if result and result.get("retCode") == 0:
                positions = result.get("result", {}).get("list", [])
                return len([p for p in positions if float(p.get("size", 0)) > 0])
            return 0
        except Exception as e:
            logger.error(f"Ошибка получения количества активных позиций: {e}")
            return 0

    async def _determine_exit_reason(self, pair, meta, closing_price=None):
        """Determine why a position was closed"""
        try:
            source = meta.get("source", "")
            if source and source.startswith("external"):
                if meta.get("rsi_tracker_started"):
                    return "take_profit"

            close_order = meta.get("close_order")
            if not close_order:
                close_order = await self._get_latest_close_order(pair)

            if not close_order:
                if meta.get("rsi_tracker_started"):
                    logger.info(f" Нет close_order для {pair}, но RSI трекер был активен - это RSI take-profit")
                    return "take_profit"
                return "position_closed"

            if closing_price:
                stop_loss = meta.get("stop_loss")
                take_profit = meta.get("take_profit")

                if stop_loss:
                    sl_distance = abs(closing_price - stop_loss)

                    if sl_distance < stop_loss * 0.001:
                        logger.info(
                            f"✅ Закриття по ЦІНІ СТОП-ЛОССА: закриття={closing_price:.8f}, SL={stop_loss:.8f}, відстань={sl_distance:.8f}"
                        )
                        return "stop_loss"

                if take_profit:
                    tp_distance = abs(closing_price - take_profit)
                    if tp_distance < take_profit * 0.001:
                        logger.info(
                            f"✅ Закриття по ЦІНІ ТЕЙК-ПРОФІТА: закриття={closing_price:.8f}, TP={take_profit:.8f}, відстань={tp_distance:.8f}"
                        )
                        return "take_profit"

            create_type = close_order.get("createType", "")
            stop_order_type = close_order.get("stopOrderType", "")
            order_type = close_order.get("orderType", "")

            if create_type in self._STOP_CREATE_TYPES or stop_order_type in ("StopLoss", "TrailingStop"):
                return "stop_loss"

            if create_type in self._TAKE_PROFIT_CREATE_TYPES or stop_order_type == "TakeProfit":
                return "take_profit"

            if order_type == "Market" and close_order.get("reduceOnly"):
                if meta.get("rsi_tracker_started"):
                    logger.info(f" Market reduceOnly ордер + активный RSI трекер для {pair} = RSI take-profit")
                    return "take_profit"

            if create_type in self._USER_CLOSE_CREATE_TYPES:
                logger.info(f"⚠️ Закриття НЕ по ціні стопа/ТП, визначено як MANUAL_CLOSE (createType={create_type})")
                return "manual_close"

            if meta.get("rsi_tracker_started"):
                logger.info(f" Fallback: RSI трекер був активний для {pair} - це take-profit")
                return "take_profit"

            return "position_closed"

        except Exception as e:
            logger.error(f"Ошибка определения причины выхода для {pair}: {e}")
            if meta.get("rsi_tracker_started"):
                return "take_profit"
            return "position_closed"

    async def _reenter_immediately(self, pair, direction, meta):
        """Immediately re-enter a position after stop loss"""
        try:
            logger.info(f" Attempting immediate re-entry for {pair} {direction}")

            current_price = await self.get_real_time_price(pair)
            if not current_price:
                logger.error(f"Cannot get current price for immediate re-entry on {pair}")
                return False

            specs = await self._get_symbol_specs(pair)
            price_step = specs.get("price_step", 0.0)

            settings = get_settings()
            stop_loss_offset = float(settings.get("stop_loss_offset", 0.007))

            extremum_data = await self.get_candle_extremum_from_db_timeframe(pair, direction, pinbar_candle_index=None)

            if extremum_data:
                stop_loss_price = extremum_data["stop_price"]
                logger.info(f" Re-entry SL calculated from extremum: {stop_loss_price:.8f}")
            else:

                logger.warning(f" Failed to get extremum for re-entry, using fallback offset")
                if direction == "long":
                    stop_loss_price = self._quantize_price(entry_price * (1 - stop_loss_offset), price_step, ROUND_DOWN)
                else:
                    stop_loss_price = self._quantize_price(entry_price * (1 + stop_loss_offset), price_step, ROUND_UP)

            if direction == "long":
                entry_price = self._quantize_price(current_price, price_step, ROUND_DOWN)
            else:
                entry_price = self._quantize_price(current_price, price_step, ROUND_UP)

            qty_usdt = await self._risk_manager.calculate_position_size(pair, entry_price)
            if not qty_usdt or qty_usdt < 10:
                qty_usdt = 10

            retry_metadata = {
                "source": "retry_immediate",
                "signal_level": meta.get("signal_level", entry_price),
                "direction": direction,
                "retry_count": meta.get("retry_count", 0),
            }

            result = await self.place_limit_order(
                pair=pair,
                direction=direction,
                price=entry_price,
                quantity_usdt=qty_usdt,
                stop_loss=stop_loss_price,
                take_profit=None,
                metadata=retry_metadata,
            )

            if result:
                logger.info(f" Immediate re-entry successful for {pair} @ {entry_price}")
                return True
            else:
                logger.error(f" Immediate re-entry failed for {pair}")
                return False

        except Exception as e:
            logger.error(f"Error in immediate re-entry for {pair}: {e}")
            return False

    async def start_rsi_tracker(self, pair, direction):
        """
        Manually start RSI tracker for a pair.
        Useful for strategies like Gravity2 that rely on RSI for Take Profit.
        """
        try:
            rsi_cfg = await self._get_rsi_settings_db()
            rsi_low = float(rsi_cfg.get("rsi_low", 30))
            rsi_high = float(rsi_cfg.get("rsi_high", 70))
            rsi_period = int(rsi_cfg.get("rsi_period", 14))
            rsi_interval = "1"

            async def _on_rsi_exit(sym, dirn, reason, context):
                logger.info(f"RSI TP triggered for {sym}: {reason}")

            logger.info(f"🎯 Manual Start: RSI Tracker for {pair} ({direction})")
            logger.info(f"   Settings: High={rsi_high}, Low={rsi_low}, Len={rsi_period}, TF={rsi_interval}")

            asyncio.create_task(
                self._take_profit_helper.track_position_rsi(
                    pair,
                    direction,
                    on_exit=_on_rsi_exit,
                    rsi_high_override=rsi_high,
                    rsi_low_override=rsi_low,
                    rsi_period_override=rsi_period,
                    rsi_interval_override=rsi_interval,
                    poll_seconds=max(15, int(self._interval_to_seconds(rsi_interval) // 2)),
                )
            )

            order_id = self._pair_to_order.get(pair)
            if order_id and order_id in self._position_meta:
                self._position_meta[order_id]["rsi_tracker_started"] = True

            return True
        except Exception as e:
            logger.error(f"Failed to start RSI tracker for {pair}: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return False

    async def get_candle_extremum_from_db_timeframe(self, pair, direction, pinbar_candle_index=None):
        """
        Get stop loss based on extremum of SIGNAL CANDLE (pinbar)

        Args:
            pair: Trading pair
            direction: 'long' or 'short'
            pinbar_candle_index: Index of pinbar candle in array (None = fallback to old logic)
        """
        try:
            settings = get_settings()
            timeframe_display = settings.get("timeframe", "1m")

            stop_buffer_type = settings.get("stop_buffer_type", None)
            stop_buffer_value = settings.get("stop_buffer_value", None)

            timeframe_api = self._parse_timeframe_for_api(timeframe_display)

            logger.info(f"Отримую екстремум для {pair}:")
            logger.info(f"   - TF: {timeframe_display}")
            logger.info(f"   - Напрямок: {direction.upper()}")
            if pinbar_candle_index is not None:
                logger.info(f"   - Індекс пін-бара: {pinbar_candle_index}")
            if stop_buffer_type and stop_buffer_value:
                logger.info(f"   - Буфер стопа: {stop_buffer_type} = {stop_buffer_value}")

            candles = await self.get_klines(pair, timeframe_api, 30)

            if not candles or len(candles) < 2:
                logger.error(f"Недостатньо свічок для {pair}")
                return None

            if pinbar_candle_index is not None:
                if pinbar_candle_index < 0 or pinbar_candle_index >= len(candles):
                    logger.error(f"Невалідний індекс пін-бара: {pinbar_candle_index}")
                    extremum_candle = candles[-2]
                    logger.warning(f"⚠️ Використовую FALLBACK (остання закрита свічка)")
                else:
                    extremum_candle = candles[pinbar_candle_index]
                    logger.info(f"✅ Використовую СИГНАЛЬНУ свічку (пін-бар) з індексом {pinbar_candle_index}")

                candle_high = float(extremum_candle[2])
                candle_low = float(extremum_candle[3])
                candle_timestamp = int(extremum_candle[0])
                signal_extremum = candle_low if direction.lower() == "long" else candle_high

            else:

                logger.info(f"🔍 Шукаю локальний екстремум за останні 20 свічок...")

                lookback_candles = candles[-21:] if len(candles) >= 21 else candles

                if direction.lower() == "long":

                    lows = [float(c[3]) for c in lookback_candles]
                    signal_extremum = min(lows)
                    extremum_candle_idx = lows.index(signal_extremum)
                    extremum_candle = lookback_candles[extremum_candle_idx]
                    logger.info(f"✅ Знайдено МІНІМАЛЬНИЙ LOW за {len(lookback_candles)} свічок: {signal_extremum:.8f}")
                else:

                    highs = [float(c[2]) for c in lookback_candles]
                    signal_extremum = max(highs)
                    extremum_candle_idx = highs.index(signal_extremum)
                    extremum_candle = lookback_candles[extremum_candle_idx]
                    logger.info(
                        f"✅ Знайдено МАКСИМАЛЬНИЙ HIGH за {len(lookback_candles)} свічок: {signal_extremum:.8f}"
                    )

                candle_high = float(extremum_candle[2])
                candle_low = float(extremum_candle[3])
                candle_timestamp = int(extremum_candle[0])

            import datetime

            candle_time = datetime.datetime.fromtimestamp(candle_timestamp / 1000)

            logger.info(f"📊 Екстремальна свічка (TF {timeframe_display}):")
            logger.info(f"   - ЧАС: {candle_time.strftime('%H:%M:%S %d.%m.%Y')}")
            logger.info(f"   - HIGH: {candle_high:.8f}")
            logger.info(f"   - LOW: {candle_low:.8f}")

            specs = await self._get_symbol_specs(pair)
            price_step = specs.get("price_step", 0.00001)

            # Спрощена логіка SL (як просив користувач):
            # SL = Extremum (точно на екстремумі, без відступів)
            
            stop_loss_offset_val = 0.0
            offset_amount = signal_extremum * stop_loss_offset_val

            if direction.lower() == "long":
                stop_price = self._quantize_price(signal_extremum - offset_amount, price_step, ROUND_DOWN)
                logger.info(f"✅ LONG: SL = LOW - {stop_loss_offset_val*100:.2f}%")
            else:
                stop_price = self._quantize_price(signal_extremum + offset_amount, price_step, ROUND_UP)
                logger.info(f"✅ SHORT: SL = HIGH + {stop_loss_offset_val*100:.2f}%")

            logger.info(f"   - Екстремум: {signal_extremum:.8f}")
            logger.info(f"   - Стоп: {stop_price:.8f}")

            return {
                "stop_price": stop_price,
                "candle_high": candle_high,
                "candle_low": candle_low,
                "signal_extremum": signal_extremum,
                "buffer_distance": 0.0,
                "final_offset": offset_amount,
                "min_offset_distance": 0.0,
                "stop_behind_offset_percent": stop_loss_offset_val * 100,
                "timeframe": timeframe_display,
                "candle_timestamp": candle_timestamp,
            }

        except Exception as e:
            logger.error(f" Помилка отримання екстремуму свічки для {pair}: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return None
