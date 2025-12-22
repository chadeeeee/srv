import asyncio
import re
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from decimal import ROUND_DOWN, ROUND_UP

from pybit.unified_trading import HTTP
from telethon import TelegramClient, events
from telethon.tl.types import Channel

from analysis.signals import stop_all_monitoring
from config import CONFIG
from telegram_bot import notify_user
from trading.signal_handler import SignalHandler
from trading.take_profit import TakeProfit
from utils.logger import logger, trade_signal
from utils.settings_manager import (
    add_to_blacklist,
    get_blacklist,
    get_settings,
    is_blacklisted,
    is_bot_active,
    remove_from_blacklist,
    reset_drawdown_protection,
    set_bot_active,
    update_setting,
)

api_id = CONFIG.get("TELEGRAM_API_ID")
api_hash = CONFIG.get("TELEGRAM_API_HASH")
channel_username = CONFIG.get("TELEGRAM_CHANNEL", "")
client = None
channel_source = CONFIG.get("TELEGRAM_CHANNEL", "")
CHANNEL_TARGET = None

signal_handler_instance: Optional[SignalHandler] = None


async def get_last_message_from_channel(client, channel_id_or_list):
    try:
        if not isinstance(channel_id_or_list, (list, tuple)):
            channel_id_or_list = [channel_id_or_list]

        for channel in channel_id_or_list:
            try:
                entity = await client.get_entity(channel)
                if isinstance(entity, Channel):
                    messages = await client.get_messages(entity, limit=1)
                    if messages:
                        last_message = messages[0].text

                        if not last_message:
                            logger.info(f"Последнее сообщение в {channel} не содержит текста.")
                            continue

                        coin_match = re.search(r"`([A-Z]+USDT)`", last_message)
                        coin = coin_match.group(1) if coin_match else "N/A"

                        if "Поддержка" in last_message:
                            direction = "LONG"
                        elif "Сопротивление" in last_message:
                            direction = "SHORT"
                        else:
                            direction = "N/A"

                        price_match = re.search(r"`([\d.,]+)`", last_message)
                        price = price_match.group(1) if price_match else "N/A"

                        # logger.info(
                        #     f"Последнее сообщение из {channel}: Монета: {coin}, Направление: {direction}, Цена: {price}"
                        # )
            except Exception as e:
                logger.error(f"Ошибка получения последнего сообщения из канала {channel}: {e}")
    except Exception as e:
        logger.error(f"Критическая ошибка в get_last_message_from_channel: {e}")


async def send_command_reply(event, message, *, parse_mode=None):
    try:
        await event.reply(message, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Не удалось отправить ответ на команду: {e}")


async def fetch_positions(handler):
    result = await handler._signed_request("GET", "/v5/position/list", {"category": "linear"})
    if result and result.get("retCode") == 0:
        return result["result"]["list"]
    return []


async def monitor_bybit_positions(handler, tp):
    known_position_pids = set()
    logger.info("Запуск мониторинга позиций Bybit для ручных сделок")

    scan_count = 0

    try:
        while True:
            try:
                scan_count += 1
                positions = await fetch_positions(handler)
                current_pids = set()
                normalized_positions = []

                for p in positions:
                    if float(p.get("size", 0)) > 0:
                        pid = p.get("positionIdx") or f"{p['symbol']}_{p['side']}"
                        current_pids.add(pid)
                        normalized_positions.append((pid, p))

                if scan_count % 6 == 0:
                    if normalized_positions:
                        for pid, pos in normalized_positions:
                            logger.info(f" Активна: {pos['symbol']} | {pos['side']} | Size: {pos['size']}")
                            logger.info(f"   - Entry: {pos.get('avgPrice', 'N/A')}")
                            logger.info(f"   - PnL: {float(pos.get('unrealisedPnl', 0)):.2f} USDT")

                new_pids = current_pids - known_position_pids
                for nid, pos in normalized_positions:
                    if nid in new_pids:
                        known_position_pids.add(nid)
                        try:
                            symbol = pos["symbol"]
                            side = pos["side"].lower()
                            direction = "long" if side == "buy" else "short"
                            entry = float(pos["avgPrice"])
                            size = float(pos["size"])

                            logger.warning("=" * 70)
                            logger.warning(f"🆕🆕🆕 ВИЯВЛЕНО НОВУ РУЧНУ ПОЗИЦІЮ! 🆕🆕🆕")
                            logger.warning("=" * 70)
                            logger.warning(f"📍 Символ: {symbol}")
                            logger.warning(f"📍 Напрямок: {direction.upper()}")
                            logger.warning(f"📍 Розмір: {size}")
                            logger.warning(f"📍 Ціна входу: {entry:.6f}")
                            logger.warning(f"📍 Плече: {pos.get('leverage', 'N/A')}x")
                            logger.warning(f"📍 Вартість позиції: {float(pos.get('positionValue', 0)):.2f} USDT")
                            logger.warning(f"📍 Unrealized PnL: {float(pos.get('unrealisedPnl', 0)):.2f} USDT")
                            logger.warning("=" * 70)

                            try:
                                ping_result = await handler._signed_request("GET", "/v5/market/time", {})
                                if ping_result and ping_result.get("retCode") == 0:
                                    server_time_ms = int(ping_result["result"]["timeNano"]) // 1000000
                                    local_time_ms = int(time.time() * 1000)
                                    ping_ms = abs(server_time_ms - local_time_ms)

                                    from utils.logger import bybit_ping_info

                                    bybit_ping_info(symbol, ping_ms, server_time_ms, local_time_ms)

                                    try:
                                        import datetime

                                        await notify_user(
                                            f"🏓 BYBIT PING (Ручна позиція)\n"
                                            f"━━━━━━━━━━━━━━━━━━\n"
                                            f" {symbol}\n"
                                            f" Пінг: {ping_ms}ms\n"
                                            f"🕐 Server: {datetime.datetime.fromtimestamp(server_time_ms/1000).strftime('%H:%M:%S.%f')[:-3]}\n"
                                            f"🕐 Local: {datetime.datetime.fromtimestamp(local_time_ms/1000).strftime('%H:%M:%S.%f')[:-3]}"
                                        )
                                    except Exception:
                                        pass
                            except Exception as e:
                                logger.warning(f" Не вдалося отримати пінг від Bybit: {e}")

                            try:
                                import datetime

                                existing_sl = pos.get("stopLoss")
                                if existing_sl and existing_sl != "" and existing_sl != "0":
                                    try:
                                        sl_value = float(existing_sl)
                                        if sl_value > 0:
                                            logger.info(
                                                f" Позиція {symbol} вже має SL: {sl_value:.8f}, пропускаємо встановлення"
                                            )

                                            asyncio.create_task(tp.track_position_rsi(symbol, direction))
                                            continue
                                    except (ValueError, TypeError):
                                        pass

                                extremum_data = await handler.get_candle_extremum_from_db_timeframe(symbol, direction)

                                if extremum_data:
                                    stop_price = extremum_data["stop_price"]
                                    candle_high = extremum_data["candle_high"]
                                    candle_low = extremum_data["candle_low"]
                                    signal_extremum = extremum_data["signal_extremum"]
                                    buffer_distance = extremum_data["buffer_distance"]
                                    tf_display = extremum_data["timeframe"]

                                    from utils.logger import (
                                        candle_high_low_info,
                                        stop_calculation_details,
                                    )

                                    candle_high_low_info(symbol, tf_display, candle_high, candle_low)

                                    settings = get_settings()
                                    buffer_type = settings.get("stop_buffer_type")
                                    buffer_value = settings.get("stop_buffer_value")

                                    stop_calculation_details(
                                        symbol,
                                        direction,
                                        signal_extremum,
                                        buffer_type,
                                        buffer_value,
                                        stop_price,
                                        signal_candle_time=extremum_data.get("candle_timestamp"),
                                    )
                                else:

                                    logger.warning(
                                        f" Немає даних сигнальної свічки для {symbol}, використовуємо поточні свічки"
                                    )

                                    settings = get_settings()
                                    tf_display = settings.get("timeframe", "1m")
                                    tf_api = handler._parse_timeframe_for_api(tf_display)

                                    candles = await handler.get_klines(symbol, tf_api, limit=3)

                                    if candles and len(candles) >= 2:

                                        last_closed = candles[-2]
                                        candle_high = float(last_closed[2])
                                        candle_low = float(last_closed[3])

                                        if direction == "long":
                                            signal_extremum = candle_low
                                        else:
                                            signal_extremum = candle_high

                                        buffer_type = settings.get("stop_buffer_type", "percentage")
                                        buffer_value = settings.get("stop_buffer_value", 0.1)

                                        if buffer_type == "percentage":
                                            if direction == "long":
                                                stop_price = signal_extremum * (1 - buffer_value / 100)
                                            else:
                                                stop_price = signal_extremum * (1 + buffer_value / 100)
                                        else:
                                            if direction == "long":
                                                stop_price = signal_extremum - buffer_value
                                            else:
                                                stop_price = signal_extremum + buffer_value

                                        specs = await handler._get_symbol_specs(symbol)
                                        price_step = specs.get("price_step", 0.00001)
                                        rounding_mode = ROUND_DOWN if direction == "long" else ROUND_UP
                                        stop_price = handler._quantize_price(stop_price, price_step, rounding_mode)

                                        from utils.logger import (
                                            candle_high_low_info,
                                            stop_calculation_details,
                                        )

                                        candle_high_low_info(symbol, tf_display, candle_high, candle_low)
                                        stop_calculation_details(
                                            symbol, direction, signal_extremum, buffer_type, buffer_value, stop_price
                                        )
                                    else:
                                        logger.error(f" Не вдалося отримати свічки для {symbol}")
                                        continue

                                if direction == "long" and stop_price >= entry:
                                    logger.error(
                                        f" КРИТИЧНА ПОМИЛКА: SL для LONG ({stop_price:.8f}) >= ціни входу ({entry:.8f})"
                                    )
                                    specs = await handler._get_symbol_specs(symbol)
                                    stop_price = handler._quantize_price(
                                        entry * 0.995, specs.get("price_step", 0.00001), ROUND_DOWN
                                    )
                                    logger.warning(f" FALLBACK SL: {stop_price:.8f}")
                                elif direction == "short" and stop_price <= entry:
                                    logger.error(
                                        f" КРИТИЧНА ПОМИЛКА: SL для SHORT ({stop_price:.8f}) <= ціни входу ({entry:.8f})"
                                    )
                                    specs = await handler._get_symbol_specs(symbol)
                                    from decimal import ROUND_UP

                                    stop_price = handler._quantize_price(
                                        entry * 1.005, specs.get("price_step", 0.00001), ROUND_UP
                                    )
                                    logger.warning(f" FALLBACK SL: {stop_price:.8f}")

                                logger.info(f" Встановлюю SL для {symbol} НЕГАЙНО після виявлення позиції...")

                                max_sl_attempts = 3
                                for attempt in range(max_sl_attempts):
                                    if attempt > 0:
                                        logger.warning(
                                            f" Повторна спроба встановлення SL ({attempt + 1}/{max_sl_attempts})"
                                        )
                                        await asyncio.sleep(1)

                                    success = await handler.set_stop_loss(symbol, stop_price, direction)

                                    if success:
                                        if extremum_data:
                                            logger.info(f" SL ВСТАНОВЛЕНО ЗА ЕКСТРЕМУМОМ СИГНАЛЬНОЇ СВІЧКИ:")
                                            logger.info(
                                                f"    Сигнальна свічка (TF {tf_display}): H={candle_high:.8f} L={candle_low:.8f}"
                                            )
                                            logger.info(f"    Екстремум: {signal_extremum:.8f}")
                                            logger.info(f"   📏 Буфер: {buffer_distance:.8f}")
                                            logger.info(f"    Фінальний стоп: {stop_price:.8f}")
                                        else:
                                            logger.info(f" SL ВСТАНОВЛЕНО ЗА ПОТОЧНОЮ СВІЧКОЮ:")
                                            logger.info(
                                                f"    Остання закрита свічка (TF {tf_display}): H={candle_high:.8f} L={candle_low:.8f}"
                                            )
                                            logger.info(f"    Екстремум: {signal_extremum:.8f}")
                                            logger.info(f"    Фінальний стоп: {stop_price:.8f}")
                                        break
                                    else:
                                        if attempt == max_sl_attempts - 1:
                                            logger.error(
                                                f" Не вдалося встановити SL для {symbol} після {max_sl_attempts} спроб"
                                            )

                            except Exception as e:
                                logger.error(f" EXCEPTION при встановленні SL для {symbol}: {e}")
                                import traceback

                                logger.error(traceback.format_exc())

                            asyncio.create_task(tp.track_position_rsi(symbol, direction))
                            logger.info(f" SL and RSI monitoring set: {symbol} {direction}")
                        except Exception as e:
                            logger.error(f" Error processing position {nid}: {e}")

                closed_pids = known_position_pids - current_pids
                for cpid in closed_pids:
                    logger.warning(f"🔒 Position closed: {cpid}")
                    known_position_pids.discard(cpid)

            except Exception as e:
                logger.error(f" Ошибка мониторинга позиций: {e}")

            await asyncio.sleep(10)
    except asyncio.CancelledError:
        logger.info("monitor_bybit_positions cancelled")

async def main():
    api_id = CONFIG.get("TELEGRAM_API_ID")
    api_hash = CONFIG.get("TELEGRAM_API_HASH")

    session_name = "bybit_strategy_session"

    # logger.info(f" Використовуємо сесію: {session_name}")

    if not api_id or not api_hash:
        logger.error("TELEGRAM_API_ID и TELEGRAM_API_HASH не настроены в config.py")
        return

    client = TelegramClient(session_name, api_id, api_hash)

    channel_targets = CONFIG.get("TELEGRAM_CHANNELS")
    if not channel_targets:
        logger.error(" TELEGRAM_CHANNELS не налаштовано в config.py!")
        return

    if not isinstance(channel_targets, list):
        channel_targets = [channel_targets]

    # logger.info(f" Слухаємо канали: {channel_targets}")

    handler = SignalHandler()
    tp = TakeProfit()

    startup_pair = "BTCUSDT"

    asyncio.create_task(monitor_bybit_positions(handler, tp))

    if not await handler.verify_limit_order_capability(startup_pair):
        logger.error("Проверка готовности на старте провалена. Reader остановлен.")
        await handler.close()
        return

    try:
        await handler._risk_manager.display_balance()
    except Exception as e:
        logger.error(f"Не удалось показать баланс: {e}")

    await client.start()

    await client.get_dialogs(limit=None)

    logger.info(f" Підключено до каналів: {channel_targets}")
    await get_last_message_from_channel(client, channel_targets)



    await handler.start_external_trade_monitor()

    @client.on(events.NewMessage(chats=channel_targets))
    async def handler_event(event):
        try:
            message_id = event.message.id
            message_text = event.message.text

            if not message_text:
                logger.debug("⏭ Пустое сообщение, пропуск")
                return

            logger.info(f"📥 Получено сообщение {message_id} из целевого канала")

            if message_text.strip().upper() == "START":
                if not is_bot_active():
                    set_bot_active(True)
                    update_setting("bot_was_active_before_panika", True)

                    reset_drawdown_protection()

                    try:
                        bal = await handler.get_usdt_balance()
                        if bal and bal.get("equity", 0) > 0:
                            current_equity = float(bal["equity"])
                            update_setting("equity_peak", current_equity)
                            logger.info(f" Пик эквити сброшен на текущий баланс: {current_equity:.2f} USDT")
                    except Exception as e:
                        logger.error(f"Не удалось обновить пик эквити: {e}")

                    try:
                        await event.reply(" Бот активирован!")
                    except Exception as e:
                        logger.error(f"Не удалось отправить ответ: {e}")

                    await handler.start_external_trade_monitor()
                    logger.info(" Бот активирован через команду START")
                else:
                    try:
                        reset_drawdown_protection()
                        try:
                            bal = await handler.get_usdt_balance()
                            if bal and bal.get("equity", 0) > 0:
                                current_equity = float(bal["equity"])
                                update_setting("equity_peak", current_equity)
                                logger.info(f" Пик эквити сброшен на текущий баланс: {current_equity:.2f} USDT")
                        except Exception as e:
                            logger.error(f"Не удалось обновить пик эквити для вже активного бота: {e}")

                        try:
                            await event.reply(" Бот вже активен — сброшена защита по просадке, нові ордера дозволені")
                        except Exception:
                            pass

                        await handler.start_external_trade_monitor()
                        logger.info(" Защита по просадке сброшена для вже активного бота і запущен external monitor")
                    except Exception as e:
                        logger.error(f"Не вдалося сбросити захист по просадці на START при вже активному боті: {e}")
                        try:
                            await event.reply(" Бот вже активен")
                        except Exception:
                            pass
                return

            if message_text.strip().upper() == "PANIKA":
                logger.warning(" Получена команда PANIKA - вход в режим только мониторинга")

                current_state = is_bot_active()
                update_setting("bot_was_active_before_panika", current_state)
                logger.info(f"Сохранено состояние бота перед PANIKA: {current_state}")

                set_bot_active(False)

                positions_count = await handler.get_active_positions_count()

                if positions_count > 0:
                    try:
                        await event.reply(f" Режим PANIKA активирован!")
                    except Exception as e:
                        logger.error(f"Не удалось отправить ответ: {e}")

                    logger.info(
                        f" Бот в режиме PANIKA: мониторинг {positions_count} позиций, новые сделки заблокированы"
                    )
                else:
                    logger.info(" Режим PANIKA активирован, открытых позиций нет")
                    try:
                        await event.reply(f" Режим PANIKA активирован!")
                    except Exception:
                        pass
                return

            if message_text.strip().upper() == "STOP":
                set_bot_active(False)
                logger.warning(" Получена команда STOP - бот теперь остановлен")

                positions_count = await handler.get_active_positions_count()

                try:
                    if positions_count > 0:
                        await event.reply(
                            f" Бот остановлен\n Продолжаем мониторинг {positions_count} существующих позиций\n🚫 Новые сделки НЕ открываются\n Для активации напишите START"
                        )
                    else:
                        await event.reply(" Бот остановлен. Нет активных позиций для мониторинга.")
                except Exception:
                    pass
                return

            if message_text.upper().endswith("STOP"):
                logger.info("Сигнал содержит 'STOP' в конце - игнорируем")

                stopped = stop_all_monitoring()

                try:
                    if stopped:
                        await event.reply(f" Сигнал игнорирован. Остановлен мониторинг для: {', '.join(stopped)}")
                    else:
                        await event.reply(" Сигнал игнорирован, потому что содержит 'STOP' в конце")
                except Exception:
                    pass
                return

            pair = None
            target_price = None
            direction = None

            lines = [line.strip() for line in (message_text or "").splitlines() if line.strip()]
            for line in lines:
                lower = line.lower()

                if line.startswith("фьючерс:") or "фьючерс" in lower:
                    candidate = line.split(":", 1)[-1].replace("`", "").replace(" ", "").upper()
                    if candidate:
                        pair = candidate

                if "цена:" in lower or "ціна:" in lower:
                    price_part = line.split(":", 1)[-1].replace("`", "").replace(" ", "").replace(",", ".")
                    try:
                        target_price = float(price_part)
                        logger.info(f" Ціна з каналу: {target_price}")
                    except ValueError:
                        logger.warning(f" Неверный фрагмент целевой ціни: {price_part}")

                if "сопротивление" in lower:
                    direction = "short"
                elif "поддержка" in lower:
                    direction = "long"
                elif "short" in lower:
                    direction = "short"
                elif "long" in lower:
                    direction = "long"

            if pair and target_price and direction:
                logger.info(f" Валидный сигнал: {pair} @ {target_price} ({direction})")
                logger.info(f"📍 Точная цена для ордера: {target_price}")
                logger.info(f" ID каналу: {event.chat_id}")

                from trading.strategies import get_strategy_for_channel

                channel_strategy = get_strategy_for_channel(event.chat_id)
                logger.info(f"📊 Стратегия для канала {event.chat_id}: {channel_strategy.name}")

                trade_signal(pair, target_price, direction)

                from utils.logger import signal_from_channel

                signal_from_channel(pair, target_price, direction, event.chat_id, channel_strategy)

                try:
                    settings = get_settings()
                    tf_display = settings.get("timeframe", "1m")
                    tf_api = handler._parse_timeframe_for_api(tf_display)

                    candles = await handler.get_klines(pair, tf_api, limit=2)

                    candle_info = ""
                    if candles and len(candles) >= 2:
                        last_closed = candles[-2]
                        c_high = float(last_closed[2])
                        c_low = float(last_closed[3])
                        candle_info = f"\n📊 Свічка: H={c_high:.8f} L={c_low:.8f}"

                    trade_direction = channel_strategy.get_entry_direction(direction)
                    signal_type = "Підтримка" if direction == "long" else "Опір"

                    msg = f"📥 {pair}\n📊 {channel_strategy.name}\n💰 {target_price:.8f}\n📍 {signal_type} → {trade_direction.upper()}{candle_info}"
                    await notify_user(msg)
                except Exception as e:
                    logger.error(f"Помилка відправки: {e}")

                if not is_bot_active():
                    return

                if is_blacklisted(pair):
                    return

                try:
                    result = await handler.place_signal_limit_order(
                        pair, direction, target_price, channel_id=event.chat_id
                    )
                    if isinstance(result, dict) and result.get("status") == "monitor_started":
                        logger.info(f"[{pair}] Моніторинг запущено")
                    await handler.start_external_trade_monitor()
                except Exception as exc:
                    logger.error(f"[{pair}] Помилка обробки: {exc}")
            else:
                logger.debug(f"⏭ Сообщение не соответствует формату торгового сигнала")

        except Exception as e:
            logger.exception(f" Ошибка обработки торгового сигнала: {e}")

    @client.on(events.NewMessage)
    async def universal_command_handler(event):
        text = (event.text or "").strip()
        if not text.startswith("/"):
            return

        targets_list = channel_targets if isinstance(channel_targets, list) else [channel_targets]
        if event.chat_id in targets_list:
            return

        logger.info(f"Получена команда из чата {event.chat_id}: {text}")

        tokens = text.replace("\n", " ").split()
        cmd_token = None
        cmd_index = -1
        for idx, token in enumerate(tokens):
            if token.startswith("/") and len(token) > 1 and token[1].isalpha():
                cmd_token = token
                cmd_index = idx
                break
        if not cmd_token:
            return

        cmd = cmd_token[1:].split("@", 1)[0].lower()
        args = tokens[cmd_index + 1 :]

        if cmd == "balance":
            try:
                await handler._risk_manager.display_balance()
                await send_command_reply(event, " Баланс показан в логах")
            except Exception as e:
                logger.error(f"Ошибка показа баланса: {e}")
                await send_command_reply(event, f" Ошибка получения баланса: {e}")
            return

        try:
            get_settings() or {}
        except Exception as exc:
            logger.error(f"Не удалось загрузить настройки: {exc}")
            await send_command_reply(event, "Не удалось получить настройки, попробуйте позже.")
            return

        if cmd == "blacklist":
            if not args:
                blacklist = get_blacklist()
                if blacklist:
                    msg = "<b>Черный список:</b>\n" + "\n".join(blacklist)
                else:
                    msg = "Черный список пуст"
                await send_command_reply(event, msg, parse_mode="html")
                return

            symbol = args[0].upper()
            try:
                if add_to_blacklist(symbol):
                    await send_command_reply(event, f" {symbol} добавлен в черный список")
                else:
                    await send_command_reply(event, f"{symbol} уже в черном списке")
            except Exception as e:
                logger.error(f"Ошибка добавления в черный список: {e}")
                await send_command_reply(event, f"Ошибка: не удалось добавить {symbol} в черный список")
            return

        if cmd == "whitelist":
            if not args:
                await send_command_reply(event, "Укажите символ для удаления из черного списка: /whitelist BTCUSDT")
                return

            symbol = args[0].upper()
            try:
                if remove_from_blacklist(symbol):
                    await send_command_reply(event, f" {symbol} удален из черного списка")
                else:
                    await send_command_reply(event, f"{symbol} не был в черном списке")
            except Exception as e:
                logger.error(f"Ошибка удаления из черного списка: {e}")
                await send_command_reply(event, f"Ошибка: не удалось удалить {symbol} из черного списка")
                return

        if cmd == "start":
            from utils.settings_manager import reset_drawdown_protection

            if not is_bot_active():
                set_bot_active(True)
                update_setting("bot_was_active_before_panika", True)
                reset_drawdown_protection()
                await send_command_reply(event, " Бот активирован!")
                try:
                    bal = await handler.get_usdt_balance()
                    if bal and bal.get("equity", 0) > 0:
                        update_setting("equity_peak", float(bal["equity"]))
                        logger.info(f" Пик эквити обновлен: {bal['equity']:.2f} USDT")
                except Exception as e:
                    logger.error(f"Не удалось обновить пик эквити: {e}")
                await handler.start_external_trade_monitor()
            else:
                await send_command_reply(event, " Бот уже активен")
            return

    from telegram_bot import init_bot, start_bot

    aiogram_bot, aiogram_dp = init_bot()

    if aiogram_bot and aiogram_dp:
        asyncio.create_task(start_bot())
        logger.info("Бот для настроек запущен")
    else:
        logger.warning(" Aiogram бот не запущен (проверьте TELEGRAM_BOT_TOKEN в config.py)")

    try:
        while True:
            try:
                logger.info("Запуск клієнта Telegram...")
                await client.run_until_disconnected()
            except asyncio.CancelledError:
                # logger.info("Клиент Telegram отменен, graceful shutdown...")
                break
            except Exception as e:
                logger.error(f"Критическая ошибка в main() (перезапуск через 5с): {e}")
                import traceback

                logger.error(traceback.format_exc())
                await asyncio.sleep(5)
                try:
                    if not client.is_connected():
                        await client.connect()
                except Exception as conn_err:
                    logger.error(f"Помилка перепідключення: {conn_err}")
    finally:
        logger.info("Начинается graceful shutdown...")

        shutdown_tasks = []

        try:
            if handler:
                # logger.info("Закрытие SignalHandler...")
                shutdown_tasks.append(handler.close())
        except Exception as e:
            logger.warning(f"Error creating handler close task: {e}")

        try:
            if tp:
                # logger.info("Закрытие TakeProfit...")
                shutdown_tasks.append(tp.close())
        except Exception as e:
            logger.warning(f"Error creating tp close task: {e}")

        if shutdown_tasks:
            try:
                await asyncio.gather(*shutdown_tasks, return_exceptions=True)
                # logger.info("Все async ресурсы закрыты")
            except Exception as e:
                logger.warning(f"Error during parallel shutdown: {e}")

        try:
            if client and client.is_connected():
                logger.info("Отключение Telegram client...")
                await client.disconnect()
                logger.info("Telegram client отключен")
        except Exception as e:
            logger.warning(f"Error disconnecting Telegram client: {e}")


class TelegramReader:
    def __init__(self):
        try:
            self._settings = get_settings() or {}
        except Exception:
            self._settings = {}

    def get_signal(self):
        pair = self._settings.get("default_pair", "BTCUSDT")
        return pair, None


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
            logger.debug(f" PyBit session created successfully (recv_window={recv_window})")
        except Exception as e:
            logger.error(f" Failed to create PyBit session: {e}")
            self._pybit_session = None

    return self._pybit_session


if __name__ == "__main__":
    import signal
    import time
    import traceback

    def signal_handler(sig, frame):
        logger.info(f"Зупинка...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот зупинено користувачем")
    except Exception as e:
        logger.error(f"Критична помилка: {e}")
        import traceback

        logger.error(traceback.format_exc())
