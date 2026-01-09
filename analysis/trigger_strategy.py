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


def _is_pinbar(candle, direction, tail_percent_min=0, body_percent_max=100, opposite_percent_max=100, size_min=0, size_max=100, avg_size=None, min_avg_pct=0, max_avg_pct=1000):
    try:
        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        c = float(candle[4])

        total_range = h - l
        if total_range == 0:
            return False
            
        # Перевірка розміру (у % від ціни відкриття)
        size_percent = (total_range / o) * 100
        if size_percent < size_min:
            return False
        if size_percent > size_max:
            return False
            
        # Перевірка відносно середньої свічки
        if avg_size is not None and avg_size > 0:
            avg_ratio = (total_range / avg_size) * 100
            if avg_ratio < min_avg_pct:
                return False
            if avg_ratio > max_avg_pct:
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
    
    pinbar_min_size = float(db_settings.get("pinbar_min_size", 0.05))
    pinbar_max_size = float(db_settings.get("pinbar_max_size", 2.0))
    
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
    touch_candle_index = None
    signal_candle = None
    trigger_order_id = None
    trigger_placed_at = 0
    trigger_candle_count = 0
    calculated_stop_loss = None # Для моніторингу пробою СЛ при активному тригері

    prev_price = None
    candles_since_touch = []

    last_pinbar_log_time = time.time()
    last_touch_log_time = time.time()
    last_rsi_log_time = time.time()

    try:
        await signal_handler.cancel_all_orders(pair, reason="Ініціалізація нового моніторингу")
    except Exception:
        pass

    try:
        while _active_monitors.get(pair, False):
            from utils.settings_manager import get_settings, is_bot_active, is_trading_paused

            if is_trading_paused():
                logger.info(f"[{pair}] ⏸️ Торгівля на паузі (ліміт збитків). Чекаємо...")
                await asyncio.sleep(60)
                continue

            if not is_bot_active():
                logger.info(f"[{pair}] Бот зупинено")
                break
            
            current_settings = get_settings()
            pinbar_timeout = int(current_settings.get("pinbar_timeout", 100))
            trigger_timeout_minutes = int(current_settings.get("trigger_timeout", 5))
            
            tail_percent_min = float(current_settings.get("pinbar_tail_percent", 0))
            body_percent_max = float(current_settings.get("pinbar_body_percent", 100))
            opposite_percent_max = float(current_settings.get("pinbar_opposite_percent", 100))
            
            # Helper to safely get setting
            def get_setting(key, default, type_func=float):
                val = current_settings.get(key)
                if val is None: return default
                return type_func(val)

            # Нові налаштування розміру пінбару (за замовчуванням 0.05% - 2.0%)
            pinbar_min_size = get_setting("pinbar_min_size", 0.05)
            pinbar_max_size = get_setting("pinbar_max_size", 2.0)
            
            # Налаштування середньої свічки
            pinbar_avg_candles = get_setting("pinbar_avg_candles", 10, int)
            pinbar_min_avg_pct = get_setting("pinbar_min_avg_percent", 50)
            pinbar_max_avg_pct = get_setting("pinbar_max_avg_percent", 200)
            
            stop_offset = get_setting("stop_loss_offset", 0.001)

            current_price = await signal_handler.get_real_time_price(pair)
            if current_price is None:
                await asyncio.sleep(1)
                continue

            candles = await get_klines(pair, timeframe_api, 200)
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
                now = time.time()
                if now - last_touch_log_time > 300:  # Раз на 5 хвилин
                    logger.info(f"[{pair}] ⏳ Чекаю торкання рівня {t}... (Ціна: {current_price:.4f})")
                    last_touch_log_time = now

                if direction == "long":
                    crossed = prev_price is not None and prev_price > t and current_price <= t
                else:
                    crossed = prev_price is not None and prev_price < t and current_price >= t

                near = abs(current_price - t) <= tol

                if crossed or near:
                    level_touched = True
                    touch_time = time.time()
                    touch_candle_index = len(candles) - 1
                    candles_since_touch = []

                    # Оновлюємо дані для /status
                    if pair in _monitor_data:
                        _monitor_data[pair]["level_touched"] = True
                        _monitor_data[pair]["touch_time"] = touch_time

                    logger.info(f"[{pair}] ✅ РІВЕНЬ ТОРКНУТО: {t:.8f}")

                    if is_gravity2:
                        _monitor_states[pair] = BotState.WAIT_RSI
                    else:
                        _monitor_states[pair] = BotState.WAIT_PINBAR

                prev_price = current_price
                await asyncio.sleep(1)
                continue

            # === STATE: WAIT_PINBAR (Premium2) ===
            if state == BotState.WAIT_PINBAR:
                candles_since_touch = candles[touch_candle_index:] if touch_candle_index else candles[-pinbar_timeout:]

                # Перевірка таймауту за часом (хвилини), а не за кількістю свічок
                elapsed_minutes = (time.time() - touch_time) / 60 if touch_time else 0
                if elapsed_minutes > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Таймаут пошуку пінбара ({pinbar_timeout} хв)")
                    break

                now = time.time()
                if now - last_pinbar_log_time > 1200:  # Раз на 20 хвилин (1200 секунд)
                    logger.info(f"[{pair}] ⏳ Ще шукаю пінбар...")
                    last_pinbar_log_time = now

                # Отримуємо RSI для перевірки (для Premium2)
                current_rsi_val = None
                if is_premium2:
                    current_rsi_val, _ = await get_rsi(pair, "1", rsi_period)

                for i in range(len(candles_since_touch) - 1, 0, -1):
                    candle = candles_since_touch[i]

                    # Розрахунок середньої свічки для цьої точки
                    abs_index = (
                        touch_candle_index + i if touch_candle_index else len(candles) - len(candles_since_touch) + i
                    )
                    avg_size = 0.0
                    if abs_index > 0:
                        avg_start = max(0, abs_index - pinbar_avg_candles)
                        avg_chunk = candles[avg_start:abs_index]
                        if avg_chunk:
                            avg_size = sum(float(c[2]) - float(c[3]) for c in avg_chunk) / len(avg_chunk)

                    if not _is_pinbar(candle, direction, tail_percent_min, body_percent_max, opposite_percent_max, pinbar_min_size, pinbar_max_size, avg_size=avg_size, min_avg_pct=pinbar_min_avg_pct, max_avg_pct=pinbar_max_avg_pct):
                        continue

                    # RSI Check for Premium2:
                    if is_premium2 and current_rsi_val is not None:
                        if direction == "long" and current_rsi_val > rsi_low:
                            continue
                        if direction == "short" and current_rsi_val < rsi_high:
                            continue

                    if not _is_new_extremum(candles, abs_index, direction):
                        continue

                    signal_candle = candle
                    rsi_msg = f", RSI={current_rsi_val:.2f}" if current_rsi_val is not None else ""
                    logger.info(f"[{pair}] ✅ ПІНБАР ЗНАЙДЕНО: H={float(candle[2]):.8f} L={float(candle[3]):.8f}{rsi_msg}")
                    _monitor_states[pair] = BotState.SIGNAL_CONFIRMED
                    break

                if _monitor_states.get(pair) != BotState.SIGNAL_CONFIRMED:
                    await asyncio.sleep(tf_secs // 4)
                    continue

            # === STATE: WAIT_RSI (Gravity2) ===
            if state == BotState.WAIT_RSI:
                current_rsi_val, _ = await get_rsi(pair, interval="1", period=rsi_period)

                now = time.time()
                if now - last_rsi_log_time > 300:  # Раз на 5 хвилин
                    cur_rsi_str = f"{current_rsi_val:.2f}" if current_rsi_val is not None else "N/A"
                    logger.info(f"[{pair}] ⏳ Чекаю RSI... (Поточний: {cur_rsi_str})")
                    last_rsi_log_time = now

                elapsed = (time.time() - touch_time) / 60 if touch_time else 0
                if elapsed > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Таймаут RSI ({pinbar_timeout} хв)")
                    break

                rsi_ok = False
                # Gravity:
                if trade_direction == "long" and current_rsi_val <= rsi_low:
                    rsi_ok = True
                    logger.info(f"[{pair}] ✅ RSI ПЕРЕПРОДАНІСТЬ: {current_rsi_val:.2f} <= {rsi_low}")
                elif trade_direction == "short" and current_rsi_val >= rsi_high:
                    rsi_ok = True
                    logger.info(f"[{pair}] ✅ RSI ПЕРЕКУПЛЕНІСТЬ: {current_rsi_val:.2f} >= {rsi_high}")

                if rsi_ok:
                    _monitor_states[pair] = BotState.WAIT_PINBAR_GRAVITY
                    
                    # Зберігаємо ЧАС свічки, на якій спрацював RSI
                    rsi_trigger_candle_time = int(candles[-1][0])
                    # rsi_trigger_candle_index = len(candles) - 1 # БІЛЬШЕ НЕ ВИКОРИСТОВУЄМО ІНДЕКС
                    
                    logger.info(f"[{pair}] ✅ RSI OK (Time={rsi_trigger_candle_time}). Чекаємо пінбар GRAVITY...")
                else:
                    await asyncio.sleep(5)
                    continue

            # === STATE: WAIT_PINBAR_GRAVITY (Gravity2) ===
            if state == BotState.WAIT_PINBAR_GRAVITY:
                elapsed = (time.time() - touch_time) / 60 if touch_time else 0
                if elapsed > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Таймаут пошуку пінбара Gravity ({pinbar_timeout} хв)")
                    break

                # Знаходимо індекс свічки RSI start в поточному списку candles
                start_index = -1
                for idx, c in enumerate(candles):
                    if int(c[0]) == rsi_trigger_candle_time:
                        start_index = idx
                        break
                
                if start_index == -1:
                    # Якщо свічка випала за межі 200 свічок - беремо початок
                    start_index = 0
                
                # Шукаємо пінбар серед свічок, починаючи з моменту RSI
                search_candles = candles[start_index:]
                
                # Потрібно мінімум 1 свічка для аналізу. Якщо це та сама свічка, що й RSI, і вона закрита - ок.
                # Але candles[-1] зазвичай закрита (в get_klines ми беремо closed).
                
                found_pinbar = False
                for i in range(len(search_candles)):
                    c = search_candles[i]
                    
                    # Розрахунок середньої свічки для цьої точки
                    current_idx = start_index + i
                    avg_size = 0.0
                    if current_idx > 0:
                        avg_start = max(0, current_idx - pinbar_avg_candles)
                        avg_chunk = candles[avg_start:current_idx]
                        if avg_chunk:
                            avg_size = sum(float(ac[2]) - float(ac[3]) for ac in avg_chunk) / len(avg_chunk)
                    
                    # 1. Is Pinbar?
                    if not _is_pinbar(c, trade_direction, tail_percent_min, body_percent_max, opposite_percent_max, pinbar_min_size, pinbar_max_size, avg_size=avg_size, min_avg_pct=pinbar_min_avg_pct, max_avg_pct=pinbar_max_avg_pct):
                        continue

                    # 2. Is New Extremum (Relative to RSI trigger moment)?
                    current_val = float(c[3]) if trade_direction == "long" else float(c[2])
                    is_new_ext = True
                    
                    # Порівнюємо з попередніми свічками в цьому діапазоні (від RSI до поточної)
                    for past_c in search_candles[:i]:
                        past_val = float(past_c[3]) if trade_direction == "long" else float(past_c[2])
                        if trade_direction == "long":
                            if past_val <= current_val: # Low має бути нижчим за попередні
                                is_new_ext = False
                                break
                        else:
                            if past_val >= current_val: # High має бути вищим
                                is_new_ext = False
                                break
                    
                    if is_new_ext:
                        signal_candle = c
                        logger.info(f"[{pair}] ✅ GRAVITY ПІНБАР: H={float(c[2]):.8f} L={float(c[3]):.8f}")
                        _monitor_states[pair] = BotState.SIGNAL_CONFIRMED
                        found_pinbar = True
                        break

                if not found_pinbar:
                    await asyncio.sleep(tf_secs // 4)
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
                # Перевірка таймауту за часом (точніше ніж лічильник ітерацій)
                elapsed_seconds = time.time() - trigger_placed_at
                timeout_seconds = trigger_timeout_minutes * 60
                
                if elapsed_seconds >= timeout_seconds:
                    logger.info(f"[{pair}] ⏱ Trigger не активувався за {trigger_timeout_minutes} хв ({timeout_seconds}с) → ВИДАЛЕННЯ")
                    if trigger_order_id:
                        await signal_handler.cancel_order(pair, trigger_order_id)
                    _monitor_states[pair] = BotState.TRIGGER_EXPIRED
                    break

                # Перевірка на пробій майбутнього SL
                if calculated_stop_loss:
                    sl_hit = False
                    if trade_direction == "long" and current_price < calculated_stop_loss:
                        sl_hit = True
                    elif trade_direction == "short" and current_price > calculated_stop_loss:
                        sl_hit = True
                    
                    if sl_hit:
                        logger.info(f"[{pair}] ❌ Ціна ({current_price:.8f}) пробила SL ({calculated_stop_loss:.8f}) до активації → ВИДАЛЕННЯ")
                        if trigger_order_id:
                            await signal_handler.cancel_order(pair, trigger_order_id)
                        _monitor_states[pair] = BotState.TRIGGER_EXPIRED
                        break

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
