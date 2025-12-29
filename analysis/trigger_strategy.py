import asyncio
import time
from decimal import ROUND_DOWN, ROUND_UP
from enum import Enum

from loguru import logger

from analysis.candle_analysis import get_klines
from config import CONFIG
from analysis.signals import get_rsi
from trading.risk_manager import RiskManager

class BotState(Enum):
    WAIT_SIGNAL = "wait_signal"
    WAIT_LEVEL_TOUCH = "wait_level_touch"
    WAIT_PINBAR = "wait_pinbar"
    WAIT_PINBAR_GRAVITY = "wait_pinbar_gravity"
    WAIT_RSI = "wait_rsi"
    SIGNAL_CONFIRMED = "signal_confirmed"
    TRIGGER_PLACED = "trigger_placed"
    TRIGGER_FILLED = "trigger_filled"
    TRIGGER_EXPIRED = "trigger_expired"


_active_monitors = {}
_monitor_states = {}
_monitor_data = {}  # Додаткові дані для статусу


def get_active_monitors_info():
    """Повертає інформацію про активні монітори для команди /status"""
    result = {}
    for pair, is_active in _active_monitors.items():
        if is_active:
            state = _monitor_states.get(pair, BotState.WAIT_SIGNAL)
            data = _monitor_data.get(pair, {})
            result[pair] = {
                "state": state.value if isinstance(state, BotState) else str(state),
                "target": data.get("target"),
                "direction": data.get("direction"),
                "trade_direction": data.get("trade_direction"),
                "strategy": data.get("strategy"),
                "level_touched": data.get("level_touched", False),
                "touch_time": data.get("touch_time"),
                "start_time": data.get("start_time"),
            }
    return result


def stop_monitoring(pair):
    if pair in _active_monitors:
        _active_monitors[pair] = False
        if pair in _monitor_states:
            del _monitor_states[pair]
        if pair in _monitor_data:
            del _monitor_data[pair]
        return True
    return False


def stop_all_monitoring():
    stopped = []
    for pair in list(_active_monitors.keys()):
        if _active_monitors[pair]:
            _active_monitors[pair] = False
            stopped.append(pair)
    _monitor_states.clear()
    _monitor_data.clear()
    return stopped


def _parse_timeframe_to_api(value):
    if not value:
        return "1"
    value = str(value).strip().upper()
    mapping = {
        "1M": "1",
        "3M": "3",
        "5M": "5",
        "15M": "15",
        "30M": "30",
        "1H": "60",
        "2H": "120",
        "4H": "240",
        "6H": "360",
        "12H": "720",
        "1D": "D",
        "1W": "W",
        "1MON": "M",
    }
    if value in mapping:
        return mapping[value]
    valid = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}
    if value in valid:
        return value
    return "1"


def _interval_to_seconds(interval):
    interval_api = _parse_timeframe_to_api(interval)
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
        "D": 86400,
        "W": 604800,
        "M": 2592000,
    }
    return mapping.get(interval_api, 60)


def _is_pinbar(candle, direction, tail_percent_min=0, body_percent_max=100, opposite_percent_max=100):
    try:
        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        c = float(candle[4])

        total_range = h - l
        if total_range == 0:
            return False

        body = abs(c - o)
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)

        body_percent = (body / total_range) * 100
        lower_wick_percent = (lower_wick / total_range) * 100
        upper_wick_percent = (upper_wick / total_range) * 100

        if direction == "long":
            main_tail_percent = lower_wick_percent
            opposite_tail_percent = upper_wick_percent
        else:
            main_tail_percent = upper_wick_percent
            opposite_tail_percent = lower_wick_percent

        if main_tail_percent < tail_percent_min:
            return False
        if body_percent > body_percent_max:
            return False
        if opposite_tail_percent > opposite_percent_max:
            return False

        return True
    except:
        return False


def _is_new_extremum(candles, index, direction):
    if index < 1 or index >= len(candles):
        return False

    candle = candles[index]
    prev_candles = candles[:index]

    if not prev_candles:
        return False

    if direction == "long":
        current_low = float(candle[3])
        prev_lows = [float(c[3]) for c in prev_candles[-5:]]
        return current_low <= min(prev_lows) if prev_lows else False
    else:
        current_high = float(candle[2])
        prev_highs = [float(c[2]) for c in prev_candles[-5:]]
        return current_high >= max(prev_highs) if prev_highs else False


async def monitor_and_trade(pair, target, direction, settings):
    from trading.signal_handler import SignalHandler

    _active_monitors[pair] = True

    timeframe_raw = str(settings.get("timeframe") or "1m")
    timeframe_api = _parse_timeframe_to_api(timeframe_raw)
    tf_secs = _interval_to_seconds(timeframe_raw)

    from utils.settings_manager import get_settings
    db_settings = get_settings()
    
    pinbar_timeout = int(db_settings.get("pinbar_timeout", 100))
    trigger_timeout_minutes = int(db_settings.get("trigger_timeout", 100))
    
    raw_tail = db_settings.get("pinbar_tail_percent")
    tail_percent_min = float(raw_tail) if raw_tail is not None else 66.0
    
    raw_body = db_settings.get("pinbar_body_percent")
    body_percent_max = float(raw_body) if raw_body is not None else 20.0
    
    raw_opp = db_settings.get("pinbar_opposite_percent")
    opposite_percent_max = float(raw_opp) if raw_opp is not None else 15.0
    
    raw_ps = db_settings.get("position_size_percent")
    pos_size = float(raw_ps) if raw_ps is not None else CONFIG.get("POSITION_SIZE_PERCENT", 1.5)
    
    raw_mt = db_settings.get("max_open_trades")
    max_trades = int(raw_mt) if raw_mt is not None else CONFIG.get("MAX_OPEN_TRADES", 3)
    
    raw_mr = db_settings.get("max_retries")
    max_retries = int(raw_mr) if raw_mr is not None else CONFIG.get("MAX_RETRIES", 1)
    
    raw_so = db_settings.get("stop_loss_offset")
    sl_offset = float(raw_so) if raw_so is not None else CONFIG.get("STOP_LOSS_OFFSET", 0)

    signal_handler = SignalHandler()
    risk_manager = RiskManager()
    rsi_settings = await signal_handler._get_rsi_settings_db()
    rsi_period = int(rsi_settings.get("rsi_period") or 14)
    rsi_high = float(rsi_settings.get("rsi_high") or 70)
    rsi_low = float(rsi_settings.get("rsi_low") or 30)
    rsi_interval = rsi_settings.get("rsi_interval") or timeframe_raw

    # Визначаємо стратегію ПЕРЕД використанням is_gravity2
    strategy = settings.get("strategy")
    if not strategy:
        from trading.strategies import get_strategy_for_channel

        channel_id = settings.get("channel_id")
        if channel_id:
            strategy = get_strategy_for_channel(channel_id)
        else:
            from trading.strategies import QuantumPremium2Strategy

            strategy = QuantumPremium2Strategy(-1)

    is_gravity2 = strategy.name == "Quantum Gravity2"
    is_premium2 = strategy.name == "Quantum Premium2"
    trade_direction = strategy.get_entry_direction(direction)

    # Тепер встановлюємо початковий стан на основі стратегії
    if is_gravity2:
        _monitor_states[pair] = BotState.WAIT_RSI
        logger.info(f"[{pair}] Gravity2: Старт з пошуку RSI (Wait RSI), контроль дотику рівня активний.")
    else:
        _monitor_states[pair] = BotState.WAIT_LEVEL_TOUCH

    # Зберігаємо дані для /status
    _monitor_data[pair] = {
        "target": float(target),
        "direction": direction,
        "trade_direction": trade_direction,
        "strategy": strategy.name,
        "level_touched": False,
        "touch_time": None,
        "start_time": time.time(),
    }

    logger.info(
        f"[{pair}] ⚙️ ПОВНИЙ КОНФІГ:\n"
        f"   🔹 РИЗИК: Поз={pos_size}%, МаксУгод={max_trades}, Спроб={max_retries}, СмещениеSL={sl_offset}\n"
        f"   🔹 ПІНБАР: {pinbar_timeout}м, Tail>={tail_percent_min}%, Body<={body_percent_max}%, Opp<={opposite_percent_max}%\n"
        f"   🔹 RSI: {rsi_low}-{rsi_high} (P={rsi_period}, TF={rsi_interval})\n"
        f"   🔹 ТАЙМАУТ ТРИГЕРА: {trigger_timeout_minutes}м"
    )

    stop_offset = CONFIG.get("STOP_BEHIND_EXTREMUM_OFFSET", 0.001)

    logger.info(f"[{pair}] Старт моніторингу")
    logger.info(
        f"[{pair}] Стратегія: {strategy.name}, Рівень: {target}, Сигнал: {direction}, Торгівля: {trade_direction}"
    )

    level_touched = False
    touch_time = time.time() if is_gravity2 else None
    rsi_trigger_candle_index = None
    touch_candle_index = None
    signal_candle = None
    trigger_order_id = None
    trigger_placed_at = 0
    trigger_candle_count = 0
    calculated_stop_loss = None  # Для моніторингу пробою СЛ при активному тригері
    last_processed_candle_ts = None  # Для відстеження вже оброблених свічок

    prev_price = None
    candles_since_touch = []

    last_pinbar_log_time = time.time()

    try:
        await signal_handler.cancel_all_orders(pair)
    except Exception:
        pass

    try:
        while _active_monitors.get(pair, False):
            from utils.settings_manager import get_settings, is_bot_active

            if not is_bot_active():
                logger.info(f"[{pair}] Бот зупинено")
                break
            
            current_settings = get_settings()
            pinbar_timeout = int(current_settings.get("pinbar_timeout", 100))
            trigger_timeout_minutes = int(current_settings.get("trigger_timeout", 5))
            tail_percent_min = float(current_settings.get("pinbar_tail_percent", 0))
            body_percent_max = float(current_settings.get("pinbar_body_percent", 100))
            opposite_percent_max = float(current_settings.get("pinbar_opposite_percent", 100))
            stop_offset = float(current_settings.get("stop_loss_offset") or 0.001)

            current_price = await signal_handler.get_real_time_price(pair)
            if current_price is None:
                await asyncio.sleep(1)
                continue

            candles = await get_klines(pair, timeframe_api, 50)
            if not candles or len(candles) < 3:
                await asyncio.sleep(1)
                continue

            state = _monitor_states.get(pair, BotState.WAIT_LEVEL_TOUCH if not is_gravity2 else BotState.WAIT_RSI)

            # Розрахунок параметрів рівня для перевірки торкання
            t = float(target)
            ranges = [float(c[2]) - float(c[3]) for c in candles[-20:-1] if len(c) > 3]
            avg_range = sum(ranges) / len(ranges) if ranges else 0.001
            tol = min(max(avg_range * 0.1, t * 0.0005), t * 0.003)

            # === GRAVITY2 SAFETY CHECK: CANCEL ON LEVEL TOUCH ===
            if is_gravity2 and state in [BotState.WAIT_RSI, BotState.WAIT_PINBAR_GRAVITY]:
                # Gravity2: Якщо ціна торкається рівня ДО входу -> СКАСУВАННЯ
                # direction="long" (Signal) -> Рівень Підтримки. Ціна має бути вище. Якщо впала до рівня -> Cancel.
                # direction="short" (Signal) -> Рівень Опору. Ціна має бути нижче. Якщо виросла до рівня -> Cancel.
                
                touch_detected = False
                if direction == "long" and current_price <= t + tol:
                    touch_detected = True
                elif direction == "short" and current_price >= t - tol:
                    touch_detected = True
                
                if touch_detected:
                    logger.info(f"[{pair}] ❌ GRAVITY2: Ціна торкнулася рівня {t} до входу → СКАСУВАННЯ")
                    break

            # === STATE: WAIT_LEVEL_TOUCH (Premium2) ===
            if state == BotState.WAIT_LEVEL_TOUCH and not level_touched:
                # Перевіряємо чи ціна перетнула рівень або біля рівня
                if direction == "long":
                    # LONG: рівень підтримки. Ціна має опуститись до рівня або нижче
                    crossed = prev_price is not None and prev_price > t and current_price <= t
                    already_below = current_price <= t  # Ціна вже на рівні або нижче
                else:
                    # SHORT: рівень опору. Ціна має піднятись до рівня або вище
                    crossed = prev_price is not None and prev_price < t and current_price >= t
                    already_below = current_price >= t  # Ціна вже на рівні або вище

                near = abs(current_price - t) <= tol

                # Логування для дебагу (рідко)
                if prev_price is None:
                    logger.info(f"[{pair}] Premium2: Початок моніторингу. Ціна: {current_price:.8f}, Рівень: {t:.8f}, Толеранс: {tol:.8f}")
                    logger.info(f"[{pair}] Premium2: already_at_level={already_below}, near={near}")

                # Торкання = перетнули АБО біля рівня АБО вже за рівнем
                if crossed or near or already_below:
                    level_touched = True
                    touch_time = time.time()
                    touch_candle_index = len(candles) - 1
                    candles_since_touch = []

                    # Оновлюємо дані для /status
                    if pair in _monitor_data:
                        _monitor_data[pair]["level_touched"] = True
                        _monitor_data[pair]["touch_time"] = touch_time

                    reason = "crossed" if crossed else ("near" if near else "already_at_level")
                    logger.info(f"[{pair}] ✅ РІВЕНЬ ТОРКНУТО ({reason}): рівень={t:.8f}, ціна={current_price:.8f}")

                    if is_gravity2:
                        _monitor_states[pair] = BotState.WAIT_RSI
                    else:
                        _monitor_states[pair] = BotState.WAIT_PINBAR
                        logger.info(f"[{pair}] Premium2: Перехід до WAIT_PINBAR. Шукаю сигнальні свічки...")

                prev_price = current_price
                await asyncio.sleep(1)
                continue

            # === STATE: WAIT_PINBAR (Premium2) ===
            if state == BotState.WAIT_PINBAR:
                # Перевірка таймауту за часом (хвилини)
                elapsed_minutes = (time.time() - touch_time) / 60 if touch_time else 0
                if elapsed_minutes > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Таймаут пошуку сигнальної свічки ({pinbar_timeout} хв)")
                    break

                now = time.time()
                if now - last_pinbar_log_time > 300:  # Раз на 5 хвилин
                    logger.info(f"[{pair}] ⏳ Premium2: шукаю сигнальну свічку... (elapsed: {elapsed_minutes:.1f} хв)")
                    last_pinbar_log_time = now

                # Отримуємо RSI для перевірки
                current_rsi_val = None
                if is_premium2:
                    current_rsi_val, _ = await get_rsi(pair, "1", rsi_period)

                # Premium2: Кожна свічка що оновила екстремум = сигнальна
                # Дивимось на останню ЗАКРИТУ свічку (candles[-2])
                if len(candles) >= 3:
                    last_closed_candle = candles[-2]  # Остання закрита свічка
                    candle_ts = int(last_closed_candle[0])  # Timestamp свічки
                    
                    # Перевіряємо чи ця свічка вже була оброблена
                    if last_processed_candle_ts and candle_ts <= last_processed_candle_ts:
                        # Ця свічка вже оброблена, чекаємо наступну
                        await asyncio.sleep(5)
                        continue
                    
                    c_high = float(last_closed_candle[2])
                    c_low = float(last_closed_candle[3])
                    
                    logger.debug(f"[{pair}] Premium2: Аналіз свічки H={c_high:.8f} L={c_low:.8f}")
                    
                    # Перевіряємо чи свічка оновила екстремум з моменту торкання рівня
                    is_new_ext = False
                    
                    # Беремо всі свічки від торкання до цієї (не включаючи цю)
                    start_idx = touch_candle_index if touch_candle_index else max(0, len(candles) - 50)
                    prev_candles = candles[start_idx:-2]  # Всі свічки до останньої закритої
                    
                    if direction == "long":
                        # Для LONG: свічка має оновити мінімум (new low)
                        if prev_candles:
                            prev_lows = [float(c[3]) for c in prev_candles]
                            min_prev_low = min(prev_lows)
                            if c_low < min_prev_low:  # Строго менше
                                is_new_ext = True
                                logger.debug(f"[{pair}] Premium2: NEW LOW! {c_low:.8f} < {min_prev_low:.8f}")
                            else:
                                logger.debug(f"[{pair}] Premium2: L={c_low:.8f} >= min={min_prev_low:.8f}, not new extremum")
                        else:
                            # Перша свічка після торкання - завжди новий екстремум
                            is_new_ext = True
                            logger.debug(f"[{pair}] Premium2: Перша свічка після торкання = new extremum")
                    else:
                        # Для SHORT: свічка має оновити максимум (new high)
                        if prev_candles:
                            prev_highs = [float(c[2]) for c in prev_candles]
                            max_prev_high = max(prev_highs)
                            if c_high > max_prev_high:  # Строго більше
                                is_new_ext = True
                                logger.debug(f"[{pair}] Premium2: NEW HIGH! {c_high:.8f} > {max_prev_high:.8f}")
                            else:
                                logger.debug(f"[{pair}] Premium2: H={c_high:.8f} <= max={max_prev_high:.8f}, not new extremum")
                        else:
                            # Перша свічка після торкання - завжди новий екстремум
                            is_new_ext = True
                            logger.debug(f"[{pair}] Premium2: Перша свічка після торкання = new extremum")
                    
                    # Запам'ятовуємо свічку як оброблену
                    last_processed_candle_ts = candle_ts
                    
                    if is_new_ext:
                        # RSI Check for Premium2 - ОПЦІОНАЛЬНА (тільки логування)
                        rsi_info = ""
                        if is_premium2 and current_rsi_val is not None:
                            rsi_info = f", RSI={current_rsi_val:.1f}"
                            if direction == "long" and current_rsi_val > rsi_low:
                                logger.info(f"[{pair}] Premium2: RSI {current_rsi_val:.1f} > {rsi_low} (не в зоні перепроданості)")
                            if direction == "short" and current_rsi_val < rsi_high:
                                logger.info(f"[{pair}] Premium2: RSI {current_rsi_val:.1f} < {rsi_high} (не в зоні перекупленості)")
                        
                        # Для Premium2 вхід базується ТІЛЬКИ на оновленні екстремуму
                        signal_candle = last_closed_candle
                        logger.info(f"[{pair}] ✅ PREMIUM2 СИГНАЛЬНА СВІЧКА: H={c_high:.8f} L={c_low:.8f}{rsi_info}")
                        _monitor_states[pair] = BotState.SIGNAL_CONFIRMED
                        # Не continue - переходимо до SIGNAL_CONFIRMED в цьому ж циклі

                if _monitor_states.get(pair) != BotState.SIGNAL_CONFIRMED:
                    await asyncio.sleep(5)  # Перевіряємо кожні 5 секунд
                    continue

            # === STATE: WAIT_RSI (Gravity2) ===
            if state == BotState.WAIT_RSI:
                current_rsi_val, _ = await get_rsi(pair, interval="1", period=rsi_period)

                elapsed = (time.time() - touch_time) / 60 if touch_time else 0
                if elapsed > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Таймаут RSI ({pinbar_timeout} хв)")
                    break

                rsi_ok = False
                # Gravity:
                # Long (Low Level): RSI <= Low
                # Short (High Level): RSI >= High
                if trade_direction == "long" and current_rsi_val <= rsi_low:
                    rsi_ok = True
                    logger.info(f"[{pair}] ✅ RSI ПЕРЕПРОДАНІСТЬ: {current_rsi_val:.2f} <= {rsi_low}")
                elif trade_direction == "short" and current_rsi_val >= rsi_high:
                    rsi_ok = True
                    logger.info(f"[{pair}] ✅ RSI ПЕРЕКУПЛЕНІСТЬ: {current_rsi_val:.2f} >= {rsi_high}")

                if rsi_ok:
                    _monitor_states[pair] = BotState.WAIT_PINBAR_GRAVITY
                    # Запам'ятовуємо індекс (поточний), з якого починаємо шукати пінбар та екстремум
                    # Оскільки ми щойно отримали RSI, шукаємо серед свічок, які будуть далі (або ця сама, якщо закриється пінбаром)
                    rsi_trigger_candle_index = len(candles) - 1
                    logger.info(f"[{pair}] ✅ RSI OK. Чекаємо пінбар GRAVITY...")
                else:
                    await asyncio.sleep(5)
                    continue

            # === STATE: WAIT_PINBAR_GRAVITY (Gravity2) ===
            if state == BotState.WAIT_PINBAR_GRAVITY:
                elapsed = (time.time() - touch_time) / 60 if touch_time else 0
                if elapsed > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Таймаут пошуку пінбара Gravity ({pinbar_timeout} хв)")
                    break

                # Дивимось на останню ЗАКРИТУ свічку
                if len(candles) < 3:
                    await asyncio.sleep(5)
                    continue
                    
                last_closed_candle = candles[-2]  # Остання закрита свічка
                candle_ts = int(last_closed_candle[0])
                
                # Перевіряємо чи ця свічка вже була оброблена
                if last_processed_candle_ts and candle_ts <= last_processed_candle_ts:
                    await asyncio.sleep(5)
                    continue
                
                c_high = float(last_closed_candle[2])
                c_low = float(last_closed_candle[3])
                
                # 1. Перевірка чи свічка є ПІНБАРОМ
                is_pinbar = _is_pinbar(last_closed_candle, trade_direction, tail_percent_min, body_percent_max, opposite_percent_max)
                
                if not is_pinbar:
                    # Не пінбар - запам'ятовуємо і чекаємо наступну
                    last_processed_candle_ts = candle_ts
                    await asyncio.sleep(5)
                    continue
                
                # 2. Перевірка чи свічка ОНОВИЛА ЕКСТРЕМУМ з моменту RSI trigger
                # Беремо всі свічки від моменту RSI до поточної (не включаючи поточну)
                search_start = rsi_trigger_candle_index if rsi_trigger_candle_index else 0
                prev_candles = candles[search_start:-2]  # Всі свічки до останньої закритої
                
                is_new_ext = True
                
                if trade_direction == "long":
                    # Для LONG: свічка має зробити новий LOW (нижче за всі попередні)
                    if prev_candles:
                        prev_lows = [float(pc[3]) for pc in prev_candles]
                        min_prev_low = min(prev_lows)
                        if c_low >= min_prev_low:  # Якщо low >= попереднього мінімуму - НЕ новий екстремум
                            is_new_ext = False
                            logger.debug(f"[{pair}] Свічка L={c_low:.8f} НЕ оновила мінімум {min_prev_low:.8f}")
                else:
                    # Для SHORT: свічка має зробити новий HIGH (вище за всі попередні)
                    if prev_candles:
                        prev_highs = [float(pc[2]) for pc in prev_candles]
                        max_prev_high = max(prev_highs)
                        if c_high <= max_prev_high:  # Якщо high <= попереднього максимуму - НЕ новий екстремум
                            is_new_ext = False
                            logger.debug(f"[{pair}] Свічка H={c_high:.8f} НЕ оновила максимум {max_prev_high:.8f}")
                
                if not is_new_ext:
                    # Свічка не оновила екстремум - запам'ятовуємо і чекаємо наступну
                    last_processed_candle_ts = candle_ts
                    await asyncio.sleep(5)
                    continue
                
                # Всі умови виконані - це сигнальна свічка!
                signal_candle = last_closed_candle
                last_processed_candle_ts = candle_ts
                logger.info(f"[{pair}] ✅ GRAVITY ПІНБАР (new extremum): H={c_high:.8f} L={c_low:.8f}")
                _monitor_states[pair] = BotState.SIGNAL_CONFIRMED
                # Не continue - переходимо до SIGNAL_CONFIRMED

            if state == BotState.WAIT_PINBAR_GRAVITY and _monitor_states.get(pair) != BotState.SIGNAL_CONFIRMED:
                await asyncio.sleep(5)
                continue

            if state == BotState.SIGNAL_CONFIRMED and signal_candle:
                c_high = float(signal_candle[2])
                c_low = float(signal_candle[3])

                specs = await signal_handler._get_symbol_specs(pair)
                price_step = specs.get("price_step", 0.00001)

                if trade_direction == "short":
                    trigger_price = signal_handler._quantize_price(c_low, price_step, ROUND_DOWN)
                    raw_stop = c_high # SL рівно на High
                    stop_loss = signal_handler._quantize_price(raw_stop, price_step, ROUND_UP)
                else:
                    trigger_price = signal_handler._quantize_price(c_high, price_step, ROUND_UP)
                    raw_stop = c_low # SL рівно на Low
                    stop_loss = signal_handler._quantize_price(raw_stop, price_step, ROUND_DOWN)

                if trade_direction == "long" and stop_loss >= trigger_price:
                    stop_loss = signal_handler._quantize_price(trigger_price * 0.995, price_step, ROUND_DOWN)
                if trade_direction == "short" and stop_loss <= trigger_price:
                    stop_loss = signal_handler._quantize_price(trigger_price * 1.005, price_step, ROUND_UP)

                qty_usdt = await risk_manager.calculate_position_size(pair, trigger_price)
                if not qty_usdt or qty_usdt < 10:
                    qty_usdt = 10
                
                calculated_stop_loss = stop_loss

                logger.info(f"[{pair}] 🎯 TRIGGER ORDER: {trade_direction.upper()}")
                logger.info(f"[{pair}]    Trigger: {trigger_price:.8f}")
                logger.info(f"[{pair}]    SL: {stop_loss:.8f}")

                metadata = {
                    "source": f"{strategy.name.lower().replace(' ', '_')}_trigger",
                    "signal_level": float(target),
                    "direction": trade_direction,
                    "strategy": strategy.name,
                    "signal_candle_high": c_high,
                    "signal_candle_low": c_low,
                }

                result = await signal_handler.place_trigger_order(
                    pair=pair,
                    direction=trade_direction,
                    trigger_price=trigger_price,
                    quantity_usdt=qty_usdt,
                    stop_loss=stop_loss,
                    take_profit=None,
                    metadata=metadata,
                )

                if result:
                    trigger_order_id = result.get("orderId") if isinstance(result, dict) else None
                    trigger_placed_at = time.time()
                    trigger_candle_count = 0
                    logger.info(f"[{pair}] ✅ TRIGGER РОЗМІЩЕНО")
                    _monitor_states[pair] = BotState.TRIGGER_PLACED
                    await signal_handler.start_external_trade_monitor()
                else:
                    logger.error(f"[{pair}] ❌ Не вдалося розмістити trigger")
                    break

            if state == BotState.TRIGGER_PLACED:
                # Перевірка загального таймауту сигналу
                total_elapsed = (time.time() - touch_time) / 60 if touch_time else 0
                if total_elapsed > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Загальний таймаут сигналу ({pinbar_timeout} хв) → ЗАВЕРШЕННЯ")
                    if trigger_order_id:
                        await signal_handler.cancel_order(pair, trigger_order_id)
                    break
                
                # Перевірка таймауту тригера
                elapsed_seconds = time.time() - trigger_placed_at
                timeout_seconds = trigger_timeout_minutes * 60
                
                if elapsed_seconds >= timeout_seconds:
                    logger.info(f"[{pair}] ⏱ Trigger не активувався за {trigger_timeout_minutes} хв → шукаємо наступну свічку")
                    if trigger_order_id:
                        await signal_handler.cancel_order(pair, trigger_order_id)
                    trigger_order_id = None
                    signal_candle = None
                    calculated_stop_loss = None
                    # Повертаємось до пошуку наступної сигнальної свічки
                    _monitor_states[pair] = BotState.WAIT_PINBAR
                    await asyncio.sleep(1)
                    continue

                # Перевірка на пробій майбутнього SL
                if calculated_stop_loss:
                    sl_hit = False
                    if trade_direction == "long" and current_price < calculated_stop_loss:
                        sl_hit = True
                    elif trade_direction == "short" and current_price > calculated_stop_loss:
                        sl_hit = True
                    
                    if sl_hit:
                        logger.info(f"[{pair}] ❌ Ціна ({current_price:.8f}) пробила SL ({calculated_stop_loss:.8f}) до активації → шукаємо наступну свічку")
                        if trigger_order_id:
                            await signal_handler.cancel_order(pair, trigger_order_id)
                        trigger_order_id = None
                        signal_candle = None
                        calculated_stop_loss = None
                        # Повертаємось до пошуку наступної сигнальної свічки
                        _monitor_states[pair] = BotState.WAIT_PINBAR
                        await asyncio.sleep(1)
                        continue

                position = await signal_handler._take_profit_helper.get_position(pair)
                if position and float(position.get("size", 0) or 0) > 0:
                    logger.info(f"[{pair}] ✅ TRIGGER ВИКОНАНО → Позиція відкрита")
                    _monitor_states[pair] = BotState.TRIGGER_FILLED
                    break

                await asyncio.sleep(5)
                continue

            prev_price = current_price
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"[{pair}] Помилка: {e}")
        import traceback

        logger.error(traceback.format_exc())
    finally:
        _active_monitors[pair] = False
        if pair in _monitor_states:
            del _monitor_states[pair]
        await signal_handler.close()
