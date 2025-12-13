import asyncio
import time
from decimal import ROUND_DOWN, ROUND_UP
from enum import Enum

from loguru import logger

from analysis.candle_analysis import get_klines
from config import CONFIG


class BotState(Enum):
    WAIT_SIGNAL = "wait_signal"
    WAIT_LEVEL_TOUCH = "wait_level_touch"
    WAIT_PINBAR = "wait_pinbar"
    WAIT_RSI = "wait_rsi"
    SIGNAL_CONFIRMED = "signal_confirmed"
    TRIGGER_PLACED = "trigger_placed"
    TRIGGER_FILLED = "trigger_filled"
    TRIGGER_EXPIRED = "trigger_expired"


_active_monitors = {}
_monitor_states = {}


def stop_monitoring(pair):
    if pair in _active_monitors:
        _active_monitors[pair] = False
        if pair in _monitor_states:
            del _monitor_states[pair]
        return True
    return False


def stop_all_monitoring():
    stopped = []
    for pair in list(_active_monitors.keys()):
        if _active_monitors[pair]:
            _active_monitors[pair] = False
            stopped.append(pair)
    _monitor_states.clear()
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
    _monitor_states[pair] = BotState.WAIT_LEVEL_TOUCH

    timeframe_raw = str(settings.get("timeframe") or "1m")
    timeframe_api = _parse_timeframe_to_api(timeframe_raw)
    tf_secs = _interval_to_seconds(timeframe_raw)

    pinbar_timeout = int(settings.get("pinbar_timeout", 10))
    trigger_timeout_candles = int(settings.get("trigger_timeout", 5))

    tail_percent_min = float(settings.get("tail_percent_min", 0))
    body_percent_max = float(settings.get("body_percent_max", 100))
    opposite_percent_max = float(settings.get("opposite_percent_max", 100))

    signal_handler = SignalHandler()
    risk_manager = signal_handler._risk_manager

    rsi_settings = await signal_handler._get_rsi_settings_db()
    rsi_period = int(rsi_settings.get("rsi_period") or 14)
    rsi_high = float(rsi_settings.get("rsi_high") or 70)
    rsi_low = float(rsi_settings.get("rsi_low") or 30)

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
    trade_direction = strategy.get_entry_direction(direction)

    stop_offset = CONFIG.get("STOP_BEHIND_EXTREMUM_OFFSET", 0.001)

    logger.info(f"[{pair}] Старт моніторингу")
    logger.info(
        f"[{pair}] Стратегія: {strategy.name}, Рівень: {target}, Сигнал: {direction}, Торгівля: {trade_direction}"
    )

    level_touched = False
    touch_time = None
    touch_candle_index = None
    signal_candle = None
    trigger_order_id = None
    trigger_placed_at = None
    trigger_candle_count = 0

    prev_price = None
    candles_since_touch = []

    last_pinbar_log_time = 0

    try:
        while _active_monitors.get(pair, False):
            from utils.settings_manager import is_bot_active

            if not is_bot_active():
                logger.info(f"[{pair}] Бот зупинено")
                break

            current_price = await signal_handler.get_real_time_price(pair)
            if current_price is None:
                await asyncio.sleep(1)
                continue

            candles = await get_klines(pair, timeframe_api, 50)
            if not candles or len(candles) < 3:
                await asyncio.sleep(1)
                continue

            state = _monitor_states.get(pair, BotState.WAIT_LEVEL_TOUCH)

            if state == BotState.WAIT_LEVEL_TOUCH and not level_touched:
                t = float(target)
                ranges = [float(c[2]) - float(c[3]) for c in candles[-20:-1] if len(c) > 3]
                avg_range = sum(ranges) / len(ranges) if ranges else 0.001
                tol = min(max(avg_range * 0.1, t * 0.0005), t * 0.003)

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

                    logger.info(f"[{pair}] ✅ РІВЕНЬ ТОРКНУТО: {t:.8f}")

                    if is_gravity2:
                        _monitor_states[pair] = BotState.WAIT_RSI
                    else:
                        _monitor_states[pair] = BotState.WAIT_PINBAR

                prev_price = current_price
                await asyncio.sleep(1)
                continue

            if state == BotState.WAIT_PINBAR:
                candles_since_touch = candles[touch_candle_index:] if touch_candle_index else candles[-pinbar_timeout:]

                elapsed_candles = len(candles_since_touch)
                if elapsed_candles > pinbar_timeout:
                    logger.info(f"[{pair}] ⏱ Таймаут пошуку пінбара ({pinbar_timeout} свічок)")
                    break

                now = time.time()
                if now - last_pinbar_log_time > 300:
                    logger.info(f"[{pair}] ⏳ Ще шукаю пінбар...")
                    last_pinbar_log_time = now

                for i in range(len(candles_since_touch) - 1, 0, -1):
                    candle = candles_since_touch[i]

                    if not _is_pinbar(candle, direction, tail_percent_min, body_percent_max, opposite_percent_max):
                        continue

                    abs_index = (
                        touch_candle_index + i if touch_candle_index else len(candles) - len(candles_since_touch) + i
                    )
                    if not _is_new_extremum(candles, abs_index, direction):
                        continue

                    signal_candle = candle
                    logger.info(f"[{pair}] ✅ ПІНБАР ЗНАЙДЕНО: H={float(candle[2]):.8f} L={float(candle[3]):.8f}")
                    _monitor_states[pair] = BotState.SIGNAL_CONFIRMED
                    break

                if _monitor_states.get(pair) != BotState.SIGNAL_CONFIRMED:
                    await asyncio.sleep(tf_secs // 4)
                    continue

            if state == BotState.WAIT_RSI:
                from analysis.signals import get_rsi

                current_rsi, _ = await get_rsi(pair, interval="1", period=rsi_period)

                elapsed = (time.time() - touch_time) / 60 if touch_time else 0
                timeout_minutes = pinbar_timeout
                if elapsed > timeout_minutes:
                    logger.info(f"[{pair}] ⏱ Таймаут RSI ({timeout_minutes} хв)")
                    break

                rsi_ok = False
                if direction == "long" and current_rsi >= rsi_high:
                    rsi_ok = True
                    logger.info(f"[{pair}] ✅ RSI ПЕРЕКУПЛЕНІСТЬ: {current_rsi:.2f} >= {rsi_high}")
                elif direction == "short" and current_rsi <= rsi_low:
                    rsi_ok = True
                    logger.info(f"[{pair}] ✅ RSI ПЕРЕПРОДАНІСТЬ: {current_rsi:.2f} <= {rsi_low}")

                if rsi_ok:
                    for i in range(len(candles) - 2, max(0, len(candles) - 10), -1):
                        candle = candles[i]
                        if _is_new_extremum(candles, i, trade_direction):
                            signal_candle = candle
                            logger.info(
                                f"[{pair}] ✅ СИГНАЛЬНА СВІЧКА (екстремум): H={float(candle[2]):.8f} L={float(candle[3]):.8f}"
                            )
                            _monitor_states[pair] = BotState.SIGNAL_CONFIRMED
                            break

                    if not signal_candle:
                        signal_candle = candles[-2]
                        logger.info(f"[{pair}] Використовую останню закриту свічку")
                        _monitor_states[pair] = BotState.SIGNAL_CONFIRMED
                else:
                    await asyncio.sleep(5)
                    continue

            if state == BotState.SIGNAL_CONFIRMED and signal_candle:
                c_high = float(signal_candle[2])
                c_low = float(signal_candle[3])

                specs = await signal_handler._get_symbol_specs(pair)
                price_step = specs.get("price_step", 0.00001)

                if trade_direction == "short":
                    trigger_price = signal_handler._quantize_price(c_low, price_step, ROUND_DOWN)
                    raw_stop = c_high * (1 + stop_offset)
                    stop_loss = signal_handler._quantize_price(raw_stop, price_step, ROUND_UP)
                else:
                    trigger_price = signal_handler._quantize_price(c_high, price_step, ROUND_UP)
                    raw_stop = c_low * (1 - stop_offset)
                    stop_loss = signal_handler._quantize_price(raw_stop, price_step, ROUND_DOWN)

                if trade_direction == "long" and stop_loss >= trigger_price:
                    stop_loss = signal_handler._quantize_price(trigger_price * 0.995, price_step, ROUND_DOWN)
                if trade_direction == "short" and stop_loss <= trigger_price:
                    stop_loss = signal_handler._quantize_price(trigger_price * 1.005, price_step, ROUND_UP)

                qty_usdt = await risk_manager.calculate_position_size(pair, trigger_price)
                if not qty_usdt or qty_usdt < 10:
                    qty_usdt = 10

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
                trigger_candle_count += 1

                if trigger_candle_count >= trigger_timeout_candles:
                    logger.info(f"[{pair}] ⏱ Trigger не активувався за {trigger_timeout_candles} свічок → ВИДАЛЕННЯ")
                    if trigger_order_id:
                        await signal_handler.cancel_order(pair, trigger_order_id)
                    _monitor_states[pair] = BotState.TRIGGER_EXPIRED
                    break

                position = await signal_handler._take_profit_helper.get_position(pair)
                if position and float(position.get("size", 0) or 0) > 0:
                    logger.info(f"[{pair}] ✅ TRIGGER ВИКОНАНО → Позиція відкрита")
                    _monitor_states[pair] = BotState.TRIGGER_FILLED
                    break

                await asyncio.sleep(tf_secs // 2)
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
