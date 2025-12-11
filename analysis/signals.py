import asyncio
import numpy as np
import talib
import time
import datetime
from decimal import ROUND_DOWN, ROUND_UP
from config import CONFIG
from trading.signal_handler import SignalHandler
from utils.logger import price_trend
from utils.settings_manager import is_bot_active
from loguru import logger
from analysis.candle_analysis import (
    is_pinbar,
    is_valid_pinbar,
    touched_level,
    check_patterns_with_divergence,
    get_klines,
    is_extremum_candle,
    detect_pinbar
)

DB_HOST = CONFIG['DB_HOST']
DB_PORT = CONFIG['DB_PORT']
DB_NAME = CONFIG['DB_NAME']
DB_USER = CONFIG['DB_USER']
DB_PASS = CONFIG['DB_PASS']

_active_monitors = {}


def stop_monitoring(pair):
    if pair in _active_monitors:
        _active_monitors[pair] = False
        logger.info(f" Остановка мониторинга для {pair}")
        return True
    return False


def stop_all_monitoring():
    stopped = []
    for pair in list(_active_monitors.keys()):
        if _active_monitors[pair]:
            _active_monitors[pair] = False
            stopped.append(pair)
    if stopped:
        logger.info(f" Остановлен мониторинг для: {', '.join(stopped)}")
    return stopped


def _slope_percent(values):
    x = np.arange(len(values))
    slope, _ = np.polyfit(x, values, 1)
    return (slope / values[-1]) * 100 if values[-1] != 0 else 0


def analyze_trend(candles):
    closes = np.array([float(c[4]) for c in candles], dtype=float)
    highs = np.array([float(c[2]) for c in candles], dtype=float)
    lows = np.array([float(c[3]) for c in candles], dtype=float)

    if len(closes) < 15:
        return {"trend": "sideways", "bounce": "up", "trend_direction": "sideways", "bounce_direction": "up", "strength": "weak"}

    short_len = min(len(closes), 20)
    mid_len = min(len(closes), 60)
    long_len = min(len(closes), 120)

    slope_short = _slope_percent(closes[-short_len:])
    slope_mid = _slope_percent(closes[-mid_len:])
    slope_long = _slope_percent(closes[-long_len:])

    hh = sum(highs[i] > highs[i - 1]
             for i in range(len(highs) - short_len, len(highs)))
    ll = sum(lows[i] < lows[i - 1]
             for i in range(len(lows) - short_len, len(lows)))
    volatility = np.std(closes[-mid_len:]) / \
        closes[-1] * 100 if mid_len > 1 else 0

    if slope_short > 0 and slope_mid > 0:
        trend = "up"
        bounce = "down"
        strength_score = sum([slope_short > 0.2, slope_mid >
                             0.12, slope_long > 0.05, hh > ll, volatility < 3])
    elif slope_short < 0 and slope_mid < 0:
        trend = "down"
        bounce = "up"
        strength_score = sum([slope_short < -0.2, slope_mid < -
                             0.12, slope_long < -0.05, ll > hh, volatility < 3])
    else:
        trend = "sideways"
        bounce = "up"
        strength_score = 1

    if strength_score >= 4:
        strength = "strong"
    elif strength_score >= 2:
        strength = "medium"
    else:
        strength = "weak"

    return {"trend": trend, "bounce": bounce, "trend_direction": trend, "bounce_direction": bounce, "strength": strength, "timeframe": "1h"}


def calculate_rsi_talib(closes, period=14):
    if len(closes) < period + 1:
        logger.warning(
            f"Недостаточно данных для RSI: {len(closes)} < {period + 1}")
        return 50

    closes_arr = np.array(closes, dtype=float)
    if np.any(np.isnan(closes_arr)) or np.any(~np.isfinite(closes_arr)):
        logger.error("Некорректные значения в массиве closes для расчета RSI")
        return 50

    rsi_series = talib.RSI(closes_arr, timeperiod=period)
    rsi = float(rsi_series[-1])
    if np.isnan(rsi):
        logger.error("talib.RSI вернул NaN")
        return 50
    return rsi


async def get_rsi(pair, interval='1', period=14):

    FIXED_RSI_INTERVAL = '1'

    if interval != '1':
        logger.warning(
            f"⚠️ RSI calculation for {pair}: Forced 1m timeframe (requested: {interval})")

    interval_api = _parse_timeframe_to_api(FIXED_RSI_INTERVAL)

    candles = await get_klines(pair, interval_api, max(200, period * 2))
    if not candles:
        logger.debug(
            f"Не удалось получить свечи для RSI: {pair} (forced 1m interval)")
        return 50, datetime.datetime.now()

    if len(candles) < period + 1:
        logger.warning(f"Недостаточно данных для RSI: {len(candles)} свечей")
        return 50, datetime.datetime.now()

    closes = []
    for candle in candles:
        try:
            closes.append(float(candle[4]))
        except (ValueError, TypeError):
            logger.error(f"Некорректная цена закрытия в свече")
            return 50, datetime.datetime.now()

    rsi = calculate_rsi_talib(closes, period)

    return rsi, datetime.datetime.now()


async def analyze_price_trend(pair):
    try:
        candles = await get_klines(pair, '60', 20)
        if candles:
            trend_data = analyze_trend(candles)
            price_trend(pair, trend_data["trend"], trend_data["bounce"])
            return {
                "trend": trend_data["trend"],
                "bounce": trend_data["bounce"],
                "price_direction": trend_data["trend"],
                "expected_bounce": trend_data["bounce"],
                "trend_direction": trend_data["trend"],
                "bounce_direction": trend_data["bounce"],
                "strength": trend_data["strength"],
                "timeframe": "1h"
            }
    except Exception as e:
        logger.error(f"Ошибка анализа тренда: {e}")
        return {"trend": "up", "bounce": "down", "price_direction": "up", "expected_bounce": "down", "trend_direction": "up", "bounce_direction": "down", "strength": "medium", "timeframe": "1h"}
    return None


async def show_rsi_for_pairs(pairs_list):
    """Показати поточний RSI для списку пар"""
    logger.info("📊 ═══════════ ПОТОЧНИЙ RSI ═══════════")

    signal_handler = SignalHandler()
    try:

        rsi_settings = await signal_handler._get_rsi_settings_db()
        rsi_period_val = rsi_settings.get('rsi_period')
        if rsi_period_val is None:
            rsi_period_val = 14
        rsi_period = int(rsi_period_val)

        rsi_high = rsi_settings.get('rsi_high')
        rsi_low = rsi_settings.get('rsi_low')

        if rsi_high is not None:
            rsi_high = float(rsi_high)
        if rsi_low is not None:
            rsi_low = float(rsi_low)

        for pair in pairs_list:
            try:
                rsi_value, _ = await get_rsi(pair, interval='1', period=rsi_period)

                if rsi_high is not None and rsi_value >= rsi_high:
                    status = f"🔴 ПЕРЕКУПЛЕНІСТЬ (>= {rsi_high})"
                elif rsi_low is not None and rsi_value <= rsi_low:
                    status = f"🟢 ПЕРЕПРОДАНІСТЬ (<= {rsi_low})"
                else:
                    status = "⚪ НЕЙТРАЛЬНА ЗОНА"

                logger.info(f"   {pair}: RSI={rsi_value:.2f} {status}")
            except Exception as e:
                logger.warning(f"   {pair}: ⚠ Не вдалося отримати RSI - {e}")
    except Exception as e:
        logger.error(f"Помилка отримання RSI: {e}")
    finally:
        await signal_handler.close()

    logger.info("📊 ══════════════════════════════════════")


def _coerce_setting(value, cast, default):
    try:
        if value is None:
            raise ValueError
        return cast(value)
    except (TypeError, ValueError):
        return default


def _normalise_interval(value, default='1m'):
    """Normalize timeframe to display format (1m, 5m, 1h) - what we store in DB"""
    if not value:
        return default

    value = str(value).strip().upper()

    internal_to_display = {
        '1': '1m', '3': '3m', '5': '5m', '15': '15m', '30': '30m',
        '60': '1h', '120': '2h', '240': '4h', '360': '6h', '720': '12h',
        'D': '1d', 'W': '1w', 'M': '1M'
    }

    if value in ('1M', '3M', '5M', '15M', '30M', '1H', '2H', '4H', '6H', '12H', '1D', '1W', '1MON'):
        return value.lower() if value != '1MON' else '1M'

    if value in internal_to_display:
        return internal_to_display[value]

    return default


def _parse_timeframe_to_api(value):
    """Parse timeframe to API format (1, 5, 60, D, W, M)"""
    if not value:
        return '1'

    value = str(value).strip().upper()

    display_to_api = {
        '1M': '1', '3M': '3', '5M': '5', '15M': '15', '30M': '30',
        '1H': '60', '2H': '120', '4H': '240', '6H': '360', '12H': '720',
        '1D': 'D', '1W': 'W', '1MON': 'M'
    }

    if value in display_to_api:
        return display_to_api[value]

    valid_api = {'1', '3', '5', '15', '30', '60',
                 '120', '240', '360', '720', 'D', 'W', 'M'}
    if value in valid_api:
        return value

    return '1'


def _interval_to_seconds(interval):
    """Convert interval to seconds"""

    interval_api = _parse_timeframe_to_api(interval)

    mapping = {
        '1': 60, '3': 180, '5': 300, '15': 900, '30': 1800,
        '60': 3600, '120': 7200, '240': 14400, '360': 21600, '720': 43200,
        'D': 86400, 'W': 604800, 'M': 2592000,
    }
    return mapping.get(interval_api, 60)


def _format_timeframe_for_display(internal_value):
    """Format timeframe for display - just return as is now since DB stores display format"""
    if not internal_value:
        return "1m"
    return str(internal_value).lower()


def _is_new_extremum(candles, direction, lookback=5):
    try:
        highs = [float(c[2]) for c in candles[-(lookback+1):]]
        lows = [float(c[3]) for c in candles[-(lookback+1):]]
        if len(highs) < lookback + 1:
            return False
        last_high = highs[-1]
        last_low = lows[-1]
        if direction == 'long':
            return last_low <= min(lows[:-1])
        else:
            return last_high >= max(highs[:-1])
    except Exception:
        return False


def _is_rsi_extreme(rsi, direction, rsi_low=30, rsi_high=70):
    try:
        if direction == 'long':
            return rsi <= float(rsi_low) if rsi_low is not None else True
        else:
            return rsi >= float(rsi_high) if rsi_high is not None else True
    except Exception:
        return False


async def monitor_and_trade(pair, target, direction, settings):
    _active_monitors[pair] = True

    timeframe_raw = str(settings.get('timeframe') or '1m')
    timeframe = _normalise_interval(timeframe_raw)

    timeframe_api = _parse_timeframe_to_api(timeframe)

    if timeframe_api in ('D', 'W', 'M'):
        logger.warning(f"⛔ Большой таймфрейм ({timeframe}), используем 1м")
        timeframe = '1m'
        timeframe_api = '1'

    timeframe_display = timeframe
    tf_secs = _interval_to_seconds(timeframe)

    stop_loss_offset_raw = settings.get('stop_loss_offset')
    stop_buffer_type = settings.get('stop_buffer_type', None)
    stop_buffer_value = settings.get('stop_buffer_value', None)

    if stop_loss_offset_raw is None or stop_loss_offset_raw == 0:
        if stop_buffer_type and stop_buffer_value:
            logger.info(
                f"   - Буфер стопа: {stop_buffer_type} = {stop_buffer_value}")
        else:
            logger.info(
                f"   - Смещение стоп-лосса: ВЫКЛЮЧЕНО (стоп на экстремуме свечи)")
    else:
        logger.warning(
            f" stop_loss_offset ({stop_loss_offset_raw}) ИГНОРИРУЕТСЯ - используется только stop_buffer")

    rsi_period = None
    rsi_high = None
    rsi_low = None
    rsi_interval = None

    limit_timeout_val = settings.get('limit_timeout')
    limit_timeout_minutes = int(
        limit_timeout_val) if limit_timeout_val is not None else None
    limit_timeout_seconds = (
        limit_timeout_minutes * 60) if limit_timeout_minutes is not None else float('inf')

    pinbar_timeout_min = settings.get("pinbar_timeout") if settings.get(
        "pinbar_timeout") is not None else 10
    timeout = (pinbar_timeout_min *
               60) if pinbar_timeout_min not in (None, 0) else None

    pinbar_body_ratio = settings.get('pinbar_body_ratio', 2.5)

    ignore_pinbar = settings.get(
        "ignore_pinbar", CONFIG.get("IGNORE_PINBAR", False))

    signal_handler = SignalHandler()
    signal_handler._take_profit_helper
    risk_manager = signal_handler._risk_manager

    rsi_settings_from_db = await signal_handler._get_rsi_settings_db()

    def safe_int(val, default):
        return int(val) if val is not None else default

    def safe_float(val, default):
        return float(val) if val is not None else default

    rsi_period = safe_int(rsi_settings_from_db.get('rsi_period'), 14)

    rsi_high = rsi_settings_from_db.get('rsi_high')
    rsi_low = rsi_settings_from_db.get('rsi_low')

    if rsi_high is not None:
        rsi_high = float(rsi_high)
    if rsi_low is not None:
        rsi_low = float(rsi_low)

    rsi_interval = '1'

    db_rsi_interval = rsi_settings_from_db.get('rsi_interval', '1')
    if db_rsi_interval != '1':
        logger.warning(
            f"⚠️ RSI interval from DB is '{db_rsi_interval}', but FORCED to '1' (1 minute)")

    try:
        current_rsi, _ = await get_rsi(pair, interval=rsi_interval, period=rsi_period)
        logger.info(f"📊 RSI {pair}: {current_rsi:.2f}")
    except Exception as e:
        logger.warning(f"⚠ Не вдалося отримати RSI для {pair}: {e}")
        current_rsi = None

    logger.info(f"⚙ Настройки для {pair}:")
    logger.info(f"   - Целевой уровень: {target}")
    logger.info(f"   - Направление: {direction}")
    logger.info(
        f"   - RSI (з БД): період={rsi_period}, high={rsi_high}, low={rsi_low}, interval={rsi_interval}")
    logger.info(f"   - Таймфрейм: {timeframe_display} (API: {timeframe_api})")
    logger.info(f"   - Pinbar body ratio: {pinbar_body_ratio}")
    logger.info(f"   - Таймаут пинбара: {pinbar_timeout_min}м")
    logger.info(f"   - Таймаут уровня: {limit_timeout_minutes}м")
    logger.info(f"   - IGNORE_PINBAR: {ignore_pinbar}")

    strategy = settings.get('strategy')

    if not strategy:

        channel_id = settings.get('channel_id')
        if channel_id:
            from trading.strategies import get_strategy_for_channel
            strategy = get_strategy_for_channel(channel_id)
            logger.info(
                f" Стратегія отримана через channel_id {channel_id}: {strategy.name}")
        else:

            from trading.strategies import QuantumPremium2Strategy
            strategy = QuantumPremium2Strategy(-1002990245762)
            logger.warning(
                f" Використовую FALLBACK стратегію: {strategy.name}")
    else:
        logger.info(
            f" Стратегія отримана з settings: {strategy.name if hasattr(strategy, 'name') else strategy}")

    wait_for_level = strategy.should_wait_for_level()
    requires_pullback = strategy.requires_pullback()
    trade_direction = strategy.get_entry_direction(direction)
    search_zone = strategy.get_pinbar_search_zone(float(target), direction)

    logger.info(f" Стратегия: {strategy.name}")
    logger.info(f"   - ID каналу: {strategy.channel_id}")
    logger.info(f"   - Направление сигнала з каналу: {direction}")
    logger.info(
        f"   - Направление трейда (після стратегії): {trade_direction}")
    logger.info(f"   - Ожидание уровня: {wait_for_level}")
    logger.info(f"   - Требуется откат: {requires_pullback}")
    logger.info(f"   - Зона поиска: {search_zone}")

    logger.info(f"📍 ЛОГІКА СТРАТЕГІЇ:")
    if strategy.name == "Quantum Premium2":
        logger.info(f"   ═══ СТРАТЕГІЯ 1: QUANTUM PREMIUM2 ═══")
        logger.info(f"   📌 Торгівля ВІД РІВНЯ з пінбаром")
        logger.info(f"   ✓ Чекаємо торкання рівня {target:.8f}")
        logger.info(f"   ✓ Після торкання аналізуємо N свічок (з timeframe)")
        if direction == "long":
            logger.info(
                f"   ✓ LONG (підтримка): пінбар оновлює МІНІМУМ → вхід на HIGH")
        else:
            logger.info(
                f"   ✓ SHORT (опір): пінбар оновлює МАКСИМУМ → вхід на LOW")
        logger.info(
            f"   ✓ Торгуємо у ВІДСКОК: {direction.upper()} сигнал → {trade_direction.upper()} угода")
    elif strategy.name == "Quantum Gravity2":
        logger.info(f"   ═══ СТРАТЕГІЯ 2: QUANTUM GRAVITY2 ═══")
        logger.info(f"   📌 Торгівля з RSI екстремумами (з БД)")
        if direction == "long":
            logger.info(f"   📊 Сигнал з каналу: LONG (підтримка)")
            logger.info(
                f"   ✓ Чекаємо RSI ПЕРЕКУПЛЕНОСТІ (RSI >= {rsi_high} з БД)")
            logger.info(f"   ✓ Ставимо лімітку на LOW свічки")
            logger.info(f"   ✓ Відкриваємо SHORT позицію")
            logger.info(
                f"   🔄 Логіка: Підтримка + перекупленість = SHORT на LOW")
        else:
            logger.info(f"   📊 Сигнал з каналу: SHORT (опір)")
            logger.info(
                f"   ✓ Чекаємо RSI ПЕРЕПРОДАНОСТІ (RSI <= {rsi_low} з БД)")
            logger.info(f"   ✓ Ставимо лімітку на HIGH свічки")
            logger.info(f"   ✓ Відкриваємо LONG позицію")
            logger.info(f"   🔄 Логіка: Опір + перепроданість = LONG на HIGH")
    else:
        logger.info(f"   ⚠ Невідома стратегія: {strategy.name}")

    limit_order_timeout_minutes = settings.get('limit_order_timeout', 3)
    logger.info(
        f"   - Таймаут лимитного ордера: {limit_order_timeout_minutes}м")

    logger.info(f"🔧 Параметри стратегії {strategy.name}:")
    logger.info(f"   - Wait for Level: {wait_for_level}")
    logger.info(f"   - Requires Pullback (RSI): {requires_pullback}")
    logger.info(f"   - Ignore Pinbar: {ignore_pinbar}")
    logger.info(f"   - Trade Direction: {trade_direction}")

    historical_candles = []
    level_touched = False
    consolidated = False
    start_touch_time = None

    _throttle_interval = 900.0
    _search_log_interval = 60.0
    _last_log_search = 0.0
    _last_log_fail = 0.0
    _last_rsi_time = 0.0
    _last_rsi_val = None
    _last_rsi_log_time = 0.0

    candles = await get_klines(pair, timeframe_api, 200)
    if candles:
        historical_candles = candles[-10:]

    try:
        prev_price = None
        try:
            prev_price = await signal_handler.get_real_time_price(pair)
            logger.info(f" Стартова ціна для {pair}: {prev_price:.8f}")
            logger.info(f" Цільовий рівень: {target:.8f}")
            logger.info(
                f"📍 Відстань до цілі: {abs(prev_price - float(target)):.8f}")
        except Exception:
            prev_price = None

        while _active_monitors.get(pair, False):

            if not is_bot_active():
                logger.warning(
                    f"🛑 Бот зупинено (STOP команда) - припиняємо моніторинг {pair}")
                _active_monitors[pair] = False
                break

            if not _active_monitors.get(pair, False):
                logger.info(f" Моніторинг зупинено: {pair}")
                break

            if wait_for_level and not level_touched:
                if start_touch_time is None:
                    start_touch_time = time.time()
                elapsed_wait = time.time() - start_touch_time
                if elapsed_wait > limit_timeout_seconds:
                    logger.warning(
                        f"⏱ Таймаут рівня {limit_timeout_minutes}м для {pair}")
                    break

            current_price = await signal_handler.get_real_time_price(pair)
            if current_price is None:
                await asyncio.sleep(1)
                continue

            candles = await get_klines(pair, timeframe_api, 200)
            if candles:
                historical_candles = candles[-10:]
            if candles is None:
                await asyncio.sleep(1)
                continue

            if wait_for_level and not level_touched:
                t = float(target)
                ranges = [float(c[2]) - float(c[3]) for c in candles[-20:-1]]
                avg_range = sum(ranges) / len(ranges) if ranges else 0.001
                base_tol = max(avg_range * 0.1, t * 0.0005)

                max_tol = t * 0.003
                tol = min(base_tol, max_tol)

                if trade_direction == 'long':

                    crossed = (prev_price is not None and prev_price <
                               t and current_price >= t)
                    near = abs(current_price - t) <= tol
                else:

                    crossed = (prev_price is not None and prev_price >
                               t and current_price <= t)
                    near = abs(current_price - t) <= tol

                reached = crossed or near

                if not reached:
                    now_debug = time.time()
                    if now_debug - _last_log_search >= 600.0:
                        logger.info(f"⏸ Чекаємо торкання рівня {t:.8f}")
                        logger.info(f"   Поточна ціна: {current_price:.8f}")
                        logger.info(
                            f"   Напрямок торгівлі: {trade_direction.upper()}")
                        logger.info(
                            f"   Відстань до рівня: {abs(current_price - t):.8f}")
                        logger.info(
                            f"   Толерантність: {tol:.8f} (max {max_tol:.8f})")
                        logger.info(f"   Стратегія: {strategy.name}")
                        _last_log_search = now_debug

                if reached:
                    level_touched = True
                    logger.info(f"✅ Рівень ДОСЯГНУТО {pair} @ {t:.6f}")
                    logger.info(
                        f"   Попередня ціна: {prev_price:.6f}" if prev_price else "   Попередня ціна: N/A")
                    logger.info(f"   Поточна ціна: {current_price:.6f}")
                    logger.info(
                        f"   Напрямок торгівлі: {trade_direction.upper()}")
                    logger.info(f"   Tolerance: {tol:.8f} ({tol/t*100:.3f}%)")
                    logger.info(
                        f"   Відстань до рівня: {abs(current_price - t):.8f}")
                    if crossed:
                        logger.info(
                            f"   ✓ Причина: ПЕРЕТИН рівня (crossed from {'below' if trade_direction == 'long' else 'above'})")
                    if near:
                        logger.info(
                            f"   ✓ Причина: В межах tolerance ({abs(current_price - t):.8f} <= {tol:.8f})")
                    logger.info(f"   Стратегія: {strategy.name}")

                    if ignore_pinbar and not requires_pullback:
                        logger.info(
                            f"🚀 PREMIUM2: МИТТЄВИЙ ВХІД на рівні з каналу!")
                        logger.info(f"   Рівень: {float(target):.8f}")
                        logger.info(f"   Напрямок: {trade_direction.upper()}")

                        try:
                            specs = await signal_handler._get_symbol_specs(pair)
                            price_step = specs.get("price_step", 0.0)

                            entry_price = signal_handler._quantize_price(float(target), price_step,
                                                                         ROUND_UP if trade_direction == 'long' else ROUND_DOWN)

                            logger.info(
                                f"   Ціна входу (рівень): {entry_price:.8f}")

                            extremum_data = await signal_handler.get_candle_extremum_from_db_timeframe(
                                pair,
                                trade_direction,
                                pinbar_candle_index=None
                            )

                            if extremum_data:
                                stop_loss_price = extremum_data["stop_price"]
                            else:

                                if trade_direction == 'long':
                                    stop_loss_price = float(target) * 0.995
                                else:
                                    stop_loss_price = float(target) * 1.005
                                stop_loss_price = signal_handler._quantize_price(stop_loss_price, price_step,
                                                                                 ROUND_DOWN if trade_direction == 'long' else ROUND_UP)

                            logger.info(f"   Stop Loss: {stop_loss_price:.8f}")

                            if trade_direction == 'long' and stop_loss_price >= entry_price:
                                logger.error(
                                    f"❌ КРИТИЧНО: SL >= Entry для LONG!")
                                stop_loss_price = signal_handler._quantize_price(
                                    entry_price * 0.995, price_step, ROUND_DOWN)
                            if trade_direction == 'short' and stop_loss_price <= entry_price:
                                logger.error(
                                    f"❌ КРИТИЧНО: SL <= Entry для SHORT!")
                                stop_loss_price = signal_handler._quantize_price(
                                    entry_price * 1.005, price_step, ROUND_UP)

                            qty_usdt = await risk_manager.calculate_position_size(pair, entry_price)
                            if not qty_usdt or qty_usdt < 10:
                                qty_usdt = 10

                            metadata = {
                                "source": f"premium2_instant_entry",
                                "signal_level": float(target),
                                "actual_entry": entry_price,
                                "direction": trade_direction,
                                "timeframe": timeframe,
                                "strategy": strategy.name,
                                "ignore_pinbar": True,
                                "instant_entry": True,
                                "entry_on_touch": True
                            }

                            if trade_direction == 'long':

                                logger.info(
                                    f"📈 PREMIUM2 LONG: Лімітний ордер на рівні")
                                result = await signal_handler.place_limit_order(
                                    pair=pair,
                                    direction=trade_direction,
                                    price=entry_price,
                                    quantity_usdt=qty_usdt,
                                    stop_loss=stop_loss_price,
                                    take_profit=None,
                                    metadata=metadata
                                )
                            else:

                                logger.info(
                                    f"📉 PREMIUM2 SHORT: Тригерний ордер на рівні")
                                result = await signal_handler.place_trigger_order(
                                    pair=pair,
                                    direction=trade_direction,
                                    trigger_price=entry_price,
                                    quantity_usdt=qty_usdt,
                                    stop_loss=stop_loss_price,
                                    take_profit=None,
                                    metadata=metadata
                                )

                            if result:
                                logger.info(
                                    f"✅ ОРДЕР РОЗМІЩЕНО МИТТЄВО: {pair} @ {entry_price:.8f}")
                                await signal_handler.start_external_trade_monitor()
                                _active_monitors[pair] = False
                                break
                            else:
                                logger.error(
                                    f"❌ Не вдалося розмістити ордер для {pair}")

                        except Exception as e:
                            logger.error(
                                f"❌ Помилка миттєвого входу PREMIUM2: {pair} - {e}")
                            import traceback
                            logger.error(traceback.format_exc())

                        _active_monitors[pair] = False
                        break
                    else:
                        if ignore_pinbar:
                            logger.info(
                                f"🚀 Рівень досягнуто. IGNORE_PINBAR=True -> Виконуємо вхід...")
                        else:
                            logger.info(
                                f"🚀 Рівень досягнуто. Починаємо пошук паттерна (Strategy: {strategy.name})...")
                    if start_touch_time is None:
                        start_touch_time = time.time()
                    _last_log_search = 0.0

            if requires_pullback and not consolidated:

                if timeout is not None and start_touch_time is not None:
                    elapsed_minutes = (time.time() - start_touch_time) / 60
                    if elapsed_minutes > timeout / 60:
                        logger.warning(
                            f"⏱ GRAVITY2: ТАЙМАУТ ОЧІКУВАННЯ RSI ВИЧЕРПАНО!")
                        logger.warning(
                            f"   Час очікування: {elapsed_minutes:.1f} хв > {timeout / 60:.0f} хв (ліміт)")
                        logger.warning(
                            f"   ❌ Ордер НЕ буде відкрито для {pair}")
                        logger.warning(f"   RSI так і не досягнув екстремуму")
                        break

                if not level_touched:

                    now_check = time.time()
                    if now_check - _last_log_search >= 600.0:
                        current_price = await signal_handler.get_real_time_price(pair)
                        if current_price:
                            distance = abs(current_price - float(target))
                            logger.info(
                                f"⏸ GRAVITY2: Чекаємо торкання рівня {float(target):.8f}")
                            logger.info(
                                f"   Поточна ціна: {current_price:.8f}")
                            logger.info(
                                f"   Відстань до рівня: {distance:.8f}")
                            logger.info(
                                f"   RSI буде перевірятися ПІСЛЯ торкання рівня")
                        _last_log_search = now_check

                    prev_price = current_price
                    await asyncio.sleep(1)
                    continue

                now = time.time()
                if now - _last_rsi_time >= 5:
                    try:
                        _last_rsi_val, _ = await get_rsi(pair, interval=rsi_interval, period=rsi_period)
                    except Exception as e:
                        logger.debug(f"Помилка отримання RSI для {pair}: {e}")
                        _last_rsi_val = None
                    _last_rsi_time = now

                if _last_rsi_val is not None:

                    should_log = (now - _last_rsi_log_time >= 900.0)

                    if direction == 'long':

                        if _last_rsi_val >= rsi_high:
                            consolidated = True
                            logger.info(
                                f"✅ GRAVITY2: RSI ПЕРЕКУПЛЕНІСТЬ для SHORT")
                            logger.info(
                                f"   Сигнал з каналу: LONG (підтримка)")
                            logger.info(f"   Відкриваємо: SHORT позицію")
                            logger.info(
                                f"   RSI: {_last_rsi_val:.2f} >= {rsi_high} (поріг перекупленості)")
                            logger.info(f"   Вхід буде на LOW свічки")
                        elif should_log:
                            logger.info(
                                f"⏳ GRAVITY2: Чекаємо перекупленості для SHORT")
                            logger.info(f"   Сигнал: ПІДТРИМКА")
                            logger.info(
                                f"   Рівень {float(target):.8f} вже торкнуто ✅")
                            logger.info(
                                f"   RSI: {_last_rsi_val:.2f} (потрібно >= {rsi_high})")
                            logger.info(
                                f"   Відстань: {rsi_high - _last_rsi_val:.2f} до перекупленості")
                            _last_rsi_log_time = now

                    else:

                        if _last_rsi_val <= rsi_low:
                            consolidated = True
                            logger.info(
                                f"✅ GRAVITY2: RSI ПЕРЕПРОДАНІСТЬ для LONG")
                            logger.info(f"   Сигнал з каналу: SHORT (опір)")
                            logger.info(f"   Відкриваємо: LONG позицію")
                            logger.info(
                                f"   RSI: {_last_rsi_val:.2f} <= {rsi_low} (поріг перепроданості)")
                            logger.info(f"   Вхід буде на HIGH свічки")
                        elif should_log:
                            logger.info(
                                f"⏳ GRAVITY2: Чекаємо перепроданості для LONG")
                            logger.info(f"   Сигнал: ОПІР")
                            logger.info(
                                f"   Рівень {float(target):.8f} вже торкнуто ✅")
                            logger.info(
                                f"   RSI: {_last_rsi_val:.2f} (потрібно <= {rsi_low})")
                            logger.info(
                                f"   Відстань: {_last_rsi_val - rsi_low:.2f} до перепроданості")
                            _last_rsi_log_time = now

            prev_price = current_price

            if historical_candles:
                last_candle = historical_candles[-1]
                last_timestamp = int(last_candle[0])
                current_timestamp = int(time.time() * 1000)

                candle_close_time = last_timestamp + (tf_secs * 1000)
                if current_timestamp < candle_close_time:
                    o = float(last_candle[1])
                    h = max(float(last_candle[2]), current_price)
                    l = min(float(last_candle[3]), current_price)
                    c = current_price
                    historical_candles[-1] = [str(last_timestamp), str(o), str(
                        h), str(l), str(c), last_candle[5], last_candle[6]]

                    await asyncio.sleep(1)
                    continue

            should_search = False

            if wait_for_level and requires_pullback:

                should_search = level_touched and consolidated
            elif wait_for_level:

                should_search = level_touched
            elif requires_pullback:

                should_search = consolidated

            if should_search:

                if timeout is not None and start_touch_time is not None:
                    elapsed_minutes = (time.time() - start_touch_time) / 60
                    if elapsed_minutes > timeout / 60:
                        logger.warning(
                            f"⏱ ТАЙМАУТ ВИЧЕРПАНО: {elapsed_minutes:.1f} хв > {timeout / 60:.0f} хв (ліміт)")
                        logger.warning(
                            f"   ❌ Ордер НЕ буде відкрито для {pair}")
                        break
                    else:
                        logger.debug(
                            f"⏳ Час очікування: {elapsed_minutes:.1f} хв / {timeout / 60:.0f} хв (ліміт)")

                if requires_pullback and consolidated:
                    logger.info(
                        f"🚀 GRAVITY2: Умови виконані - відкриваємо позицію")
                    logger.info(f"   Стратегія: {strategy.name}")
                    logger.info(f"   Сигнал з каналу: {direction}")
                    logger.info(f"   Направлення угоди: {trade_direction}")

                    try:
                        tf_display = settings.get('timeframe', '1m')
                        tf_api = signal_handler._parse_timeframe_for_api(
                            tf_display)

                        current_candles = await signal_handler.get_klines(pair, tf_api, limit=3)

                        if not current_candles or len(current_candles) < 2:
                            logger.error(
                                f"❌ Не вдалося отримати свічки для {pair}")
                            break

                        last_closed = current_candles[-2]
                        c_high = float(last_closed[2])
                        c_low = float(last_closed[3])

                        logger.info(
                            f"📊 Остання закрита свічка (TF {tf_display}):")
                        logger.info(f"   HIGH: {c_high:.8f}")
                        logger.info(f"   LOW: {c_low:.8f}")

                        extremum_data = await signal_handler.get_candle_extremum_from_db_timeframe(
                            pair,
                            trade_direction,
                            pinbar_candle_index=None
                        )

                        if not extremum_data:
                            logger.error(
                                f"❌ Не вдалося отримати дані екстремума для {pair}")
                            break

                        stop_loss_price = extremum_data["stop_price"]
                        candle_high = extremum_data["candle_high"]
                        candle_low = extremum_data["candle_low"]
                        tf_display = extremum_data["timeframe"]

                        logger.info(f"📏 Розрахований Stop Loss:")
                        logger.info(
                            f"   Свічка: H={candle_high:.8f} L={candle_low:.8f}")
                        logger.info(f"   Stop Loss: {stop_loss_price:.8f}")

                        if trade_direction == 'short':

                            trigger_price = c_low
                            logger.info(
                                f"🎯 SHORT: Розміщуємо TRIGGER ORDER на LOW сигнальної свічки: {trigger_price:.8f}")

                            if stop_loss_price <= trigger_price:
                                logger.error(
                                    f"❌ КРИТИЧНО: SL <= Trigger для SHORT!")
                                logger.error(
                                    f"   Trigger: {trigger_price:.8f}, SL: {stop_loss_price:.8f}")
                                break

                            qty_usdt = await risk_manager.calculate_position_size(pair, trigger_price)
                            if not qty_usdt or qty_usdt < 10:
                                qty_usdt = 10

                            logger.info(f"💰 Розмір позиції: {qty_usdt} USDT")

                            metadata = {
                                "source": f"gravity2_short_trigger",
                                "signal_level": float(target),
                                "direction": trade_direction,
                                "timeframe": timeframe,
                                "strategy": strategy.name,
                                "signal_direction": direction,
                                "candle_high": candle_high,
                                "candle_low": candle_low,
                                "rsi_value": _last_rsi_val,
                                "trigger_price": trigger_price
                            }

                            logger.info(
                                f"� Розміщення TRIGGER ORDER для SHORT...")
                            logger.info(
                                f"   Trigger ціна: {trigger_price:.8f}")
                            logger.info(f"   Stop Loss: {stop_loss_price:.8f}")
                            logger.info(f"   RSI: {_last_rsi_val:.2f}")

                            result = await signal_handler.place_trigger_order(
                                pair=pair,
                                direction=trade_direction,
                                trigger_price=trigger_price,
                                quantity_usdt=qty_usdt,
                                stop_loss=stop_loss_price,
                                take_profit=None,
                                metadata=metadata
                            )

                            if result:
                                logger.info(
                                    f"✅ TRIGGER ORDER розміщено для SHORT {pair}")
                                logger.info(f"   Trigger: {trigger_price:.8f}")
                                logger.info(
                                    f"   Stop Loss: {stop_loss_price:.8f}")
                                await signal_handler.start_external_trade_monitor()
                            else:
                                logger.error(
                                    f"❌ Не вдалося розмістити trigger order для {pair}")

                        else:

                            logger.info(
                                f"📈 LONG: Розміщуємо MARKET ORDER (без trigger)")

                            current_market_price = await signal_handler.get_real_time_price(pair)
                            if stop_loss_price >= current_market_price:
                                logger.error(
                                    f"❌ КРИТИЧНО: SL >= Market Price для LONG!")
                                logger.error(
                                    f"   Market: {current_market_price:.8f}, SL: {stop_loss_price:.8f}")
                                break

                            qty_usdt = await risk_manager.calculate_position_size(pair, current_market_price)
                            if not qty_usdt or qty_usdt < 10:
                                qty_usdt = 10

                            logger.info(f"💰 Розмір позиції: {qty_usdt} USDT")

                            metadata = {
                                "source": f"gravity2_long_market",
                                "signal_level": float(target),
                                "direction": trade_direction,
                                "timeframe": timeframe,
                                "strategy": strategy.name,
                                "signal_direction": direction,
                                "candle_high": candle_high,
                                "candle_low": candle_low,
                                "rsi_value": _last_rsi_val
                            }

                            logger.info(
                                f"📝 Розміщення MARKET ORDER для LONG...")
                            logger.info(
                                f"   Поточна ціна: {current_market_price:.8f}")
                            logger.info(f"   Stop Loss: {stop_loss_price:.8f}")
                            logger.info(f"   RSI: {_last_rsi_val:.2f}")

                            specs = await signal_handler._get_symbol_specs(pair)
                            price_step = specs.get("price_step", 0.0)

                            entry_price = signal_handler._quantize_price(
                                c_high, price_step, ROUND_UP)
                            logger.info(
                                f"🎯 LONG: Розміщуємо LIMIT ORDER на HIGH сигнальної свічки: {entry_price:.8f}")

                            result = await signal_handler.place_limit_order(
                                pair=pair,
                                direction=trade_direction,
                                price=entry_price,
                                quantity_usdt=qty_usdt,
                                stop_loss=stop_loss_price,
                                take_profit=None,
                                metadata=metadata
                            )

                            if result:
                                logger.info(
                                    f"✅ LIMIT ORDER розміщено для LONG {pair}")
                                logger.info(f"   Вхід: {entry_price:.8f}")
                                logger.info(
                                    f"   Stop Loss: {stop_loss_price:.8f}")
                                await signal_handler.start_external_trade_monitor()

                                await signal_handler.start_rsi_tracker(pair, trade_direction)
                            else:
                                logger.error(
                                    f"❌ Не вдалося розмістити order для {pair}")

                    except Exception as e:
                        logger.error(
                            f"❌ Помилка відкриття Gravity2 позиції для {pair}: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                    finally:
                        _active_monitors[pair] = False

                    break

                use_trigger = strategy.use_trigger_logic(trade_direction)

                if use_trigger and settings.get('trigger_wait_enabled', False):
                    logger.info(
                        f"🎯 TRIGGER LOGIC: Gravity2 {trade_direction.upper()}")
                    logger.info(f"   Стратегія: {strategy.name}")
                    logger.info(f"   Сигнал з каналу: {direction}")
                    logger.info(f"   Направлення угоди: {trade_direction}")

                    try:

                        trigger_wait_seconds = int(
                            settings.get('trigger_wait_seconds', 3))
                        trigger_timeout_minutes = int(
                            settings.get('trigger_timeout_minutes', 60))
                        trigger_candle_check = settings.get(
                            'trigger_candle_check_enabled', True)

                        logger.info(f"   Налаштування trigger:")
                        logger.info(
                            f"      - Очікування після активації: {trigger_wait_seconds}s")
                        logger.info(
                            f"      - Таймаут: {trigger_timeout_minutes}m")
                        logger.info(
                            f"      - Перевірка виходу за свічку: {trigger_candle_check}")

                        tf_display = settings.get('timeframe', '1m')
                        tf_api = signal_handler._parse_timeframe_for_api(
                            tf_display)

                        current_candles = await signal_handler.get_klines(pair, tf_api, limit=3)

                        if not current_candles or len(current_candles) < 2:
                            logger.error(
                                f"❌ Не вдалося отримати свічки для {pair}")
                            break

                        signal_candle = current_candles[-2]
                        candle_high = float(signal_candle[2])
                        candle_low = float(signal_candle[3])

                        logger.info(f"📊 Сигнальна свічка (TF {tf_display}):")
                        logger.info(f"      HIGH: {candle_high:.8f}")
                        logger.info(f"      LOW: {candle_low:.8f}")

                        if trade_direction == 'short':
                            trigger_price = candle_low
                            logger.info(
                                f"🎯 TRIGGER dla SHORT: {trigger_price:.8f} (LOW свічки)")
                        else:
                            trigger_price = candle_high
                            logger.info(
                                f"🎯 TRIGGER dla LONG: {trigger_price:.8f} (HIGH свічки)")

                        logger.info(
                            f"👀 Очікування активації trigger для {pair}...")

                        trigger_activated = False
                        trigger_start_time = time.time()
                        trigger_timeout = trigger_timeout_minutes * 60

                        while time.time() - trigger_start_time < trigger_timeout:
                            if not _active_monitors.get(pair, False):
                                logger.info(
                                    f"⛔ Моніторинг зупинено для {pair}")
                                break

                            current_price = await signal_handler.get_real_time_price(pair)
                            if current_price is None:
                                await asyncio.sleep(1)
                                continue

                            if trade_direction == 'short' and current_price <= trigger_price:
                                trigger_activated = True
                                logger.info(f"✅ TRIGGER AKTYWOWANY dla SHORT!")
                                logger.info(
                                    f"   Поточна ціна: {current_price:.8f}")
                                logger.info(f"   Trigger: {trigger_price:.8f}")
                                break
                            elif trade_direction == 'long' and current_price >= trigger_price:
                                trigger_activated = True
                                logger.info(f"✅ TRIGGER AKTYWOWANY dla LONG!")
                                logger.info(
                                    f"   Поточна ціна: {current_price:.8f}")
                                logger.info(f"   Trigger: {trigger_price:.8f}")
                                break

                            await asyncio.sleep(0.5)

                        if not trigger_activated:
                            logger.warning(
                                f"⏱ Таймаут trigger для {pair} ({trigger_timeout_minutes}m)")
                            break

                        if trigger_wait_seconds > 0:
                            logger.info(
                                f"⏳ Очікування {trigger_wait_seconds}s після активації trigger...")
                            await asyncio.sleep(trigger_wait_seconds)

                        if trigger_candle_check:
                            final_price = await signal_handler.get_real_time_price(pair)
                            if final_price is None:
                                logger.error(
                                    f"❌ Не вдалося отримати фінальну ціну для {pair}")
                                break

                            if trade_direction == 'short':
                                if final_price > candle_low:
                                    logger.warning(
                                        f"❌ Ціна повернулась у діапазон свічки!")
                                    logger.warning(
                                        f"   Поточна: {final_price:.8f} > LOW: {candle_low:.8f}")
                                    logger.warning(
                                        f"   Пропускаємо відкриття позиції")
                                    break
                                else:
                                    logger.info(
                                        f"✅ Ціна залишається за межами свічки: {final_price:.8f} <= {candle_low:.8f}")
                            else:
                                if final_price < candle_high:
                                    logger.warning(
                                        f"❌ Ціна повернулась у діапазон свічки!")
                                    logger.warning(
                                        f"   Поточна: {final_price:.8f} < HIGH: {candle_high:.8f}")
                                    logger.warning(
                                        f"   Пропускаємо відкриття позиції")
                                    break
                                else:
                                    logger.info(
                                        f"✅ Ціна залишається за межами свічки: {final_price:.8f} >= {candle_high:.8f}")

                        logger.info(f"🚀 Відкриття MARKET ORDER для {pair}")

                        extremum_data = await signal_handler.get_candle_extremum_from_db_timeframe(
                            pair,
                            trade_direction,
                            pinbar_candle_index=None
                        )

                        if not extremum_data:
                            logger.error(
                                f"❌ Не вдалося отримати дані екстремума для {pair}")
                            break

                        stop_loss_price = extremum_data["stop_price"]

                        market_price = await signal_handler.get_real_time_price(pair)
                        if trade_direction == 'long' and stop_loss_price >= market_price:
                            logger.error(
                                f"❌ КРИТИЧНО: SL >= Market Price для LONG!")
                            logger.error(
                                f"   Market: {market_price:.8f}, SL: {stop_loss_price:.8f}")
                            break
                        if trade_direction == 'short' and stop_loss_price <= market_price:
                            logger.error(
                                f"❌ КРИТИЧНО: SL <= Market Price для SHORT!")
                            logger.error(
                                f"   Market: {market_price:.8f}, SL: {stop_loss_price:.8f}")
                            break

                        qty_usdt = await risk_manager.calculate_position_size(pair, market_price)
                        if not qty_usdt or qty_usdt < 10:
                            qty_usdt = 10

                        metadata = {
                            "source": f"trigger_{strategy.name.lower().replace(' ', '_')}",
                            "signal_level": float(target),
                            "trigger_price": trigger_price,
                            "direction": trade_direction,
                            "timeframe": timeframe,
                            "strategy": strategy.name,
                            "trigger_logic": True,
                            "signal_direction": direction,
                            "candle_high": candle_high,
                            "candle_low": candle_low
                        }

                        logger.info(f"📝 Розміщення MARKET ORDER (TRIGGER)...")

                        result = await signal_handler.place_market_order(
                            pair=pair,
                            direction=trade_direction,
                            quantity_usdt=qty_usdt,
                            stop_loss=stop_loss_price,
                            take_profit=None,
                            metadata=metadata
                        )

                        if result:
                            logger.info(f"✅ Market order розміщено: {pair}")
                            await signal_handler.start_external_trade_monitor()
                        else:
                            logger.error(
                                f"❌ Не вдалося розмістити market order для {pair}")

                    except Exception as e:
                        logger.error(
                            f"❌ Помилка trigger logic для {pair}: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                    finally:
                        _active_monitors[pair] = False

                    break

                if not ignore_pinbar:
                    now_ts = time.time()

                    if now_ts - _last_log_search >= _search_log_interval:
                        logger.info(
                            f"🔍 [IGNORE_PINBAR=False] Шукаю пін-бар для {pair} ({strategy.name})...")
                        logger.info(f"   Рівень: {float(target):.8f}")
                        if strategy.name == "Quantum Premium2":
                            logger.info(
                                f"   Умова: Пінбар має оновити екстремум (lookback={lookback_candles if 'lookback_candles' in locals() else 'dynamic'})")
                        _last_log_search = now_ts

                    if wait_for_level and start_touch_time and (timeout is not None) and (time.time() - start_touch_time > timeout):
                        logger.warning(f"⏱ Таймаут пінбара {timeout}с: {pair}")
                        break

                    if strategy.name == "Quantum Premium2" and search_zone.get('analyze_n_candles'):

                        logger.info(
                            f"🔍 PREMIUM2: Аналіз останніх свічок після торкання")
                        logger.info(
                            f"   Шукаємо пінбар з оновленням екстремуму")

                        lookback_candles = 5
                        if start_touch_time:
                            try:

                                touch_ts_ms = start_touch_time * 1000

                                candles_since_touch = 0
                                for i in range(len(candles)-1, -1, -1):
                                    candle_open_time = int(candles[i][0])

                                    if candle_open_time >= touch_ts_ms:
                                        candles_since_touch += 1
                                    else:

                                        candles_since_touch += 1
                                        break

                                lookback_candles = max(5, candles_since_touch)
                                logger.info(
                                    f"⏱ Динамічний lookback: {lookback_candles} свічок (з моменту торкання)")
                            except Exception as e:
                                logger.error(
                                    f"Помилка розрахунку lookback: {e}")
                                lookback_candles = 5

                        pinbar = detect_pinbar(
                            candles, trade_direction, pinbar_body_ratio, lookback=lookback_candles)
                    else:

                        pinbar = detect_pinbar(
                            candles, trade_direction, pinbar_body_ratio)

                    if not pinbar:
                        now_ts = time.time()
                        if now_ts - _last_log_fail >= _throttle_interval:

                            _last_log_fail = now_ts
                        await asyncio.sleep(1)
                        continue

                    logger.info(
                        f"🔥 ПІНБАР виявлений для {pair} ({strategy.name})")
                    logger.info(
                        f"   📊 Сигнальна свічка: O={pinbar['open']:.8f} H={pinbar['high']:.8f} L={pinbar['low']:.8f} C={pinbar['close']:.8f}")
                    logger.info(
                        f"   📍 Індекс пін-бара: {pinbar.get('candle_index', 'N/A')}")
                    logger.info(f"   💰 Вхід: {pinbar['entry_price']:.8f}")
                    logger.info(
                        f"   🛑 Інвалидація при пробої: {pinbar['invalidate_price']:.8f}")

                    try:
                        specs = await signal_handler._get_symbol_specs(pair)
                        price_step = specs.get("price_step", 0.0)

                        entry_price = signal_handler._quantize_price(pinbar['entry_price'], price_step,
                                                                     ROUND_UP if trade_direction == 'long' else ROUND_DOWN)

                        pinbar_index = pinbar.get('candle_index')

                        if pinbar_index is not None:
                            negative_index = pinbar_index - len(candles)
                            logger.info(
                                f"🎯 Стоп-лосс за екстремумом пін-бара (індекс: {pinbar_index}, відносний: {negative_index})")
                        else:
                            negative_index = None
                            logger.warning(
                                f"⚠️ Індекс пін-бара не знайдено, використовую fallback")

                        extremum_data = await signal_handler.get_candle_extremum_from_db_timeframe(
                            pair,
                            trade_direction,
                            pinbar_candle_index=negative_index
                        )

                        if not extremum_data:
                            logger.error(
                                f"❌ Не вдалося отримати дані екстремума для {pair}")
                            break

                        stop_loss_price = extremum_data["stop_price"]

                        invalidate_price = pinbar['invalidate_price']

                        from utils.logger import signal_candle_detected

                        signal_candle_detected(
                            pair,
                            trade_direction,
                            {"open": pinbar['open'], "high": pinbar['high'],
                                "low": pinbar['low'], "close": pinbar['close']},
                            True,
                            "pinbar"
                        )

                        logger.info(
                            f"📍 ПАРАМЕТРИ {trade_direction.upper()} ОРДЕРА:")
                        logger.info(
                            f"   🕯 Пінбар: H={pinbar['high']:.8f} L={pinbar['low']:.8f}")
                        logger.info(f"    Вхід: {entry_price:.8f}")
                        logger.info(
                            f"    Стоп (з буфером): {stop_loss_price:.8f}")
                        logger.info(
                            f"    Ціна інвалидації: {invalidate_price:.8f}")
                        logger.info(
                            f"   ⏱ Таймаут лимітного ордера: {limit_order_timeout_minutes} хвилин")

                        if trade_direction == 'long' and stop_loss_price >= entry_price:
                            logger.error(f" КРИТИЧНО: SL >= Вхід для LONG!")
                            break
                        if trade_direction == 'short' and stop_loss_price <= entry_price:
                            logger.error(f" КРИТИЧНО: SL <= Вхід для SHORT!")
                            break

                        qty_usdt = await risk_manager.calculate_position_size(pair, entry_price)
                        if not qty_usdt or qty_usdt < 10:
                            qty_usdt = 10

                        metadata = {
                            "source": f"monitor_{strategy.name.lower().replace(' ', '_')}",
                            "signal_level": float(target),
                            "actual_entry": entry_price,
                            "direction": trade_direction,
                            "timeframe": timeframe,
                            "strategy": strategy.name,
                            "pinbar": pinbar,
                            "invalidate_price": invalidate_price,
                            "limit_order_timeout": limit_order_timeout_minutes
                        }

                        logger.info(
                            f" Розміщення лимітного ордера з моніторингом життєвого циклу...")

                        result = await signal_handler.place_limit_order(
                            pair=pair,
                            direction=trade_direction,
                            price=entry_price,
                            quantity_usdt=qty_usdt,
                            stop_loss=stop_loss_price,
                            take_profit=None,
                            metadata=metadata
                        )

                        if result:
                            logger.info(
                                f" Лимітний ордер розміщено: {pair} @ {entry_price:.8f}")
                            await signal_handler.start_external_trade_monitor()
                        else:
                            logger.error(
                                f" Не вдалося розмістити лимітний ордер для {pair}")

                    except Exception as e:
                        logger.error(
                            f" Помилка розміщення ордера: {pair} - {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                    finally:
                        _active_monitors[pair] = False

            await asyncio.sleep(1)

    except asyncio.CancelledError:
        logger.info(f"Моніторинг скасовано: {pair}")
    except Exception as e:
        logger.error(f"Помилка моніторингу: {pair} - {e}")
    finally:
        try:
            _active_monitors.pop(pair, None)
        except Exception:
            pass
        try:
            await signal_handler.close()
        except Exception as e:
            logger.warning(f"Помилка закриття обробника: {e}")
        logger.info(f"Моніторинг завершено: {pair}")
        return
