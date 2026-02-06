import asyncio
import json
import ssl

import aiohttp
import numpy as np
from loguru import logger

from utils.logger import pattern_detected
from utils.settings_manager import get_bybit_base_url


async def get_klines(pair, interval, limit):
    base_url = get_bybit_base_url()
    url = f"{base_url}/v5/market/kline?category=linear&symbol={pair}&interval={interval}&limit={limit}"
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    timeout = aiohttp.ClientTimeout(total=10)
    max_retries = 3
    retry_delay = 1
    for attempt in range(max_retries):
        try:
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        text = await response.text()
                        if not text or len(text.strip()) == 0:
                            logger.warning(f"Пустой ответ при получении klines для {pair}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(retry_delay * (2**attempt))
                                continue
                            return None

                        try:
                            data = json.loads(text)
                            if data.get("retCode") == 0:
                                candles = data["result"]["list"]
                                return list(reversed(candles))
                            else:
                                logger.error(f"Bybit API error for {pair}: {data.get('retMsg')}")
                        except json.JSONDecodeError:
                            logger.error(f"Некорректный JSON ответ при получении klines для {pair}: {text[:100]}")
                    else:
                        logger.error(f"API вернул статус {response.status} для {pair}")
        except Exception as e:
            logger.error(f"Ошибка API запроса (попытка {attempt+1}) для {pair}: {e}")

        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay * (2**attempt))

    logger.error(f"Не удалось получить klines для {pair} после {max_retries} попыток")
    return None


def is_pinbar(o, h, l, c, direction, tail_percent_min=0, body_percent_max=100, opposite_percent_max=100):
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
    elif direction == "short":
        main_tail_percent = upper_wick_percent
        opposite_tail_percent = lower_wick_percent
    else:
        return False

    if main_tail_percent < tail_percent_min:
        return False
    if body_percent > body_percent_max:
        return False
    if opposite_tail_percent > opposite_percent_max:
        return False

    return True


def is_valid_pinbar(previous_candles, level, direction):
    lookback = 5
    recent_closes = [float(c[4]) for c in previous_candles[-lookback:]]
    if direction == "long":
        return not any(close < level for close in recent_closes)
    else:
        return not any(close > level for close in recent_closes)


def touched_level(l, h, level, tol):
    return l - tol <= level <= h + tol


def _slope_percent(values):
    x = np.arange(len(values))
    slope, _ = np.polyfit(x, values, 1)
    return (slope / values[-1]) * 100


def determine_trend_direction(candles, lookback=20):
    closes = np.array([float(candle[4]) for candle in candles], dtype=float)
    highs = np.array([float(candle[2]) for candle in candles], dtype=float)
    lows = np.array([float(candle[3]) for candle in candles], dtype=float)

    if len(closes) < 15:
        return "sideways"

    short_len = min(len(closes), max(lookback, 20))
    mid_len = min(len(closes), 60)
    long_len = min(len(closes), 120)

    slope_short = _slope_percent(closes[-short_len:])
    slope_mid = _slope_percent(closes[-mid_len:])
    slope_long = _slope_percent(closes[-long_len:])

    hh = sum(highs[i] > highs[i - 1] for i in range(len(highs) - short_len, len(highs)))
    ll = sum(lows[i] < lows[i - 1] for i in range(len(lows) - short_len, len(lows)))

    if slope_short > 0 and slope_mid > 0:
        return "uptrend" if hh >= ll else "sideways"
    if slope_short < 0 and slope_mid < 0:
        return "downtrend" if ll >= hh else "sideways"
    return "sideways"


def determine_bounce_direction(trend, pinbar_direction):
    result = {"valid": False, "type": "undefined", "strength": "weak"}

    if trend == "uptrend" and pinbar_direction == "long":
        result["valid"] = True
        result["type"] = "trend_continuation"
        result["strength"] = "strong"
    elif trend == "uptrend" and pinbar_direction == "short":
        result["valid"] = True
        result["type"] = "trend_reversal"
        result["strength"] = "medium"
    elif trend == "downtrend" and pinbar_direction == "short":
        result["valid"] = True
        result["type"] = "trend_continuation"
        result["strength"] = "strong"
    elif trend == "downtrend" and pinbar_direction == "long":
        result["valid"] = True
        result["type"] = "trend_reversal"
        result["strength"] = "medium"
    elif trend == "sideways":
        result["valid"] = True
        result["type"] = "range_bounce"
        result["strength"] = "weak"

    return result


def calculate_rsi(closes, period=14):
    closes_arr = np.array(closes)
    deltas = np.diff(closes_arr)

    if len(deltas) < period:
        return 50

    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period

    if down == 0:
        return 100

    rs = up / down
    rsi = 100 - 100 / (1 + rs)

    for i in range(period, len(deltas)):
        delta = deltas[i]

        if delta > 0:
            upval = delta
            downval = 0
        else:
            upval = 0
            downval = -delta

        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period

        if down == 0:
            rsi = 100
        else:
            rs = up / down
            rsi = 100 - 100 / (1 + rs)

    return rsi


def get_price_direction_and_bounce(candles, lookback=20):
    trend = determine_trend_direction(candles, lookback)

    bounce_direction = "down" if trend == "uptrend" else "up"
    trade_direction = "short" if bounce_direction == "down" else "long"

    strength = "medium"
    recent = candles[-5:]
    closes = [float(c[4]) for c in recent]

    if trend == "uptrend" and all(closes[i] >= closes[i - 1] for i in range(1, len(closes))):
        strength = "strong"
    elif trend == "downtrend" and all(closes[i] <= closes[i - 1] for i in range(1, len(closes))):
        strength = "strong"

    return {
        "main_trend": trend,
        "bounce_direction": bounce_direction,
        "trade_direction": trade_direction,
        "signal_strength": strength,
    }


async def wait_for_signal(pair, direction=None, settings=None):
    while True:
        candles = await get_klines(pair, "1", 200)
        if not candles:
            await asyncio.sleep(60)
            continue

        last_candle = candles[-1]
        o, h, l, c = map(float, last_candle[1:5])

        if direction == "long" and c > o:
            return "long"
        elif direction == "short" and c < o:
            return "short"

        await asyncio.sleep(5)


async def process_signal(pair, direction, target_price, settings=None):
    candles = await get_klines(pair, "1", 200)
    if not candles:
        logger.warning(f"Could not fetch candles for {pair}")
        return {"trade": False, "reason": "No candle data"}

    last_candle = candles[-1]
    o, h, l, c = map(float, last_candle[1:5])

    if direction == "long" and c <= target_price:
        if is_pinbar(
            o, h, l, c, direction, settings["body_tail_ratio"], settings["pinbar_size"], settings["avg_range"]
        ):
            return {
                "trade": True,
                "reason": "Bullish pinbar confirmed",
                "direction": "long",
                "signal_type": "pinbar",
                "entry_price": target_price,
            }
    elif direction == "short" and c >= target_price:
        if is_pinbar(
            o, h, l, c, direction, settings["body_tail_ratio"], settings["pinbar_size"], settings["avg_range"]
        ):
            return {
                "trade": True,
                "reason": "Bearish pinbar confirmed",
                "direction": "short",
                "signal_type": "pinbar",
                "entry_price": target_price,
            }

    return {"trade": False, "reason": "Conditions not met"}


async def get_hourly_trend(pair, hours=4):
    candles = await get_klines(pair, "60", hours)
    if not candles:
        logger.warning(f"Could not fetch hourly candles for {pair}")
        return None

    return determine_trend_direction(candles, lookback=hours)


async def get_immediate_price_direction(pair):
    hourly_candles = await get_klines(pair, "60", 24)
    if not hourly_candles:
        logger.warning(f"Could not get hourly candle data for {pair}")
        return {"error": "Could not get hourly candle data"}

    price_info = get_price_direction_and_bounce(hourly_candles, lookback=12)

    return {
        "price_direction": price_info["main_trend"],
        "expected_bounce": price_info["bounce_direction"],
        "trade_direction": price_info["trade_direction"],
        "timeframe": "1h",
    }


def is_engulfing(current_candle, previous_candle, volume_history, direction):
    try:
        curr_o, curr_h, curr_l, curr_c = map(float, current_candle[1:5])
        prev_o, prev_h, prev_l, prev_c = map(float, previous_candle[1:5])
        curr_vol = float(current_candle[5]) if len(current_candle) > 5 else 0

        if len(volume_history) >= 5 and curr_vol > 0:
            avg_volume = sum(volume_history[-5:]) / 5 if sum(volume_history[-5:]) > 0 else 1
            if curr_vol <= avg_volume * 1.2:
                logger.debug(f"Engulfing volume check failed: {curr_vol} <= {avg_volume * 1.2}")
                return False
        elif curr_vol == 0:
            logger.debug("No volume data available for engulfing pattern, skipping volume check")

        curr_body = abs(curr_c - curr_o)
        prev_body = abs(prev_c - prev_o)

        if curr_body <= prev_body:
            return False

        if direction == "long":
            is_prev_bearish = prev_c < prev_o
            is_curr_bullish = curr_c > curr_o
            opens_below = curr_o <= prev_c
            closes_above = curr_c >= prev_o

            if is_prev_bearish and is_curr_bullish and opens_below and closes_above:
                return True

        elif direction == "short":
            is_prev_bullish = prev_c > prev_o
            is_curr_bearish = curr_c < curr_o
            opens_above = curr_o >= prev_c
            closes_below = curr_c <= prev_o

            if is_prev_bullish and is_curr_bearish and opens_above and closes_below:
                return True

        return False

    except (ValueError, IndexError) as e:
        logger.error(f"Error in engulfing pattern detection: {e}")
        return False


def check_patterns_with_divergence(pair, candles, direction, take_profit_helper=None):
    result = {"pinbar": False, "engulfing": False, "divergence": None, "signal_strength": "weak"}

    if len(candles) < 10:
        return result

    last_candle = candles[-1]
    prev_candle = candles[-2]

    o, h, l, c = map(float, last_candle[1:5])

    ranges = [float(c[2]) - float(c[3]) for c in candles[-20:-1]]
    avg_range = sum(ranges) / len(ranges) if ranges else 0.001

    try:
        from analysis.signals import get_settings

        settings = get_settings()
        body_tail_ratio = settings.get("body_tail_ratio", 2.5)
        pinbar_size = settings.get("pinbar_size", 0.5)
        tail_percent_min = settings.get("pinbar_tail_percent", 70)
        body_percent_max = settings.get("pinbar_body_percent", 20)
        opposite_percent_max = settings.get("pinbar_opposite_percent", 10)
    except:
        body_tail_ratio = 2.5
        pinbar_size = 0.5
        tail_percent_min = 70
        body_percent_max = 20
        opposite_percent_max = 10

    if is_pinbar(
        o,
        h,
        l,
        c,
        direction,
        body_tail_ratio,
        pinbar_size,
        avg_range,
        tail_percent_min,
        body_percent_max,
        opposite_percent_max,
    ):
        result["pinbar"] = True
        pattern_detected(pair, "PIN BAR", direction)

    volumes = [float(c[5]) for c in candles[-6:]]
    if is_engulfing(last_candle, prev_candle, volumes, direction):
        result["engulfing"] = True
        pattern_detected(pair, "ENGULFING", direction)

    pattern_count = sum([result["pinbar"], result["engulfing"]])

    if pattern_count >= 2:
        result["signal_strength"] = "strong"
    elif pattern_count == 1:
        result["signal_strength"] = "medium"

    return result


def is_new_high(candles, index=-1, lookback=5):
    """Check if candle at index made a new high"""
    try:
        if len(candles) < lookback + 1:
            return False

        actual_index = len(candles) + index if index < 0 else index

        if actual_index < lookback:
            return False

        start_idx = max(0, actual_index - lookback)
        end_idx = actual_index + 1

        if end_idx > len(candles):
            return False

        highs = [float(c[2]) for c in candles[start_idx:end_idx]]

        if len(highs) < 2:
            return False

        return highs[-1] >= max(highs[:-1])
    except Exception as e:
        logger.error(f"Error checking new high: {e}")
        return False


def is_new_low(candles, index=-1, lookback=5):
    """Check if candle at index made a new low"""
    try:
        if len(candles) < lookback + 1:
            return False

        actual_index = len(candles) + index if index < 0 else index

        if actual_index < lookback:
            return False

        start_idx = max(0, actual_index - lookback)
        end_idx = actual_index + 1

        if end_idx > len(candles):
            return False

        lows = [float(c[3]) for c in candles[start_idx:end_idx]]

        if len(lows) < 2:
            return False

        return lows[-1] <= min(lows[:-1])
    except Exception as e:
        logger.error(f"Error checking new low: {e}")
        return False


def is_extremum_candle(candles, direction, lookback=5):
    """
    Перевірка чи є свічка екстремумом

    Для LONG: свічка повинна оновити мінімум (new low)
    Для SHORT: свічка повинна оновити максимум (new high)
    """
    try:
        if direction == "long":
            result = is_new_low(candles, index=-1, lookback=lookback)
            if not result:
                logger.debug(f"Свічка НЕ є new_low для LONG (lookback={lookback})")
            return result
        elif direction == "short":
            result = is_new_high(candles, index=-1, lookback=lookback)
            if not result:
                logger.debug(f"Свічка НЕ є new_high для SHORT (lookback={lookback})")
            return result
        return False
    except Exception as e:
        logger.error(f"Error checking extremum: {e}")
        return False


class CandleAnalyzer:
    def __init__(self):
        pass

    def get_recent_candles_sync(self, pair: str, interval: str = "1", limit: int = 3):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop:
            return None

        try:
            return asyncio.run(get_klines(pair, interval, limit))
        except Exception:
            return None

    def find_signal_candle(self, pair: str, timeframe: str = "1"):
        candles = self.get_recent_candles_sync(pair, timeframe, limit=3)
        if not candles:
            return None
        last = candles[-1]
        try:
            return {
                "timestamp": int(last[0]),
                "open": float(last[1]),
                "high": float(last[2]),
                "low": float(last[3]),
                "close": float(last[4]),
                "volume": float(last[5]) if len(last) > 5 else None,
            }
        except Exception:
            return None


def detect_pinbar(candles, direction, lookback=5, tail_percent_min=0, body_percent_max=100, opposite_percent_max=100):
    """
    Знаходить сигнальну свічку-пінбар після торкання рівня
    """
    if len(candles) < 5:
        return None

    try:
        from config import CONFIG

        stop_offset = CONFIG.get("STOP_BEHIND_EXTREMUM_OFFSET", 0.002)
    except:
        stop_offset = 0.002

    search_depth = 5

    for i in range(len(candles) - 2, max(len(candles) - (search_depth + 2), 0), -1):
        signal_candle = candles[i]

        try:
            timestamp = int(signal_candle[0])
            o = float(signal_candle[1])
            h = float(signal_candle[2])
            l = float(signal_candle[3])
            c = float(signal_candle[4])

            if not is_pinbar(o, h, l, c, direction, tail_percent_min, body_percent_max, opposite_percent_max):
                continue

            test_candles = candles[: i + 1]
            if not is_extremum_candle(test_candles, direction, lookback=min(lookback, 5)):
                continue

            if direction == "long":

                stop_price = l * (1 - stop_offset)

                logger.info(f"📍 LONG пінбар знайдено:")
                logger.info(f"   - Екстремум (LOW): {l:.8f}")
                logger.info(f"   - Стоп: {stop_price:.8f}")

                return {
                    "timestamp": timestamp,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "direction": "long",
                    "entry_price": h,
                    "stop_price": stop_price,
                    "extremum_price": l,
                    "invalidate_price": l,
                    "body": abs(c - o),
                    "total_range": h - l,
                    "is_extremum": True,
                    "candle_index": i,
                    "stop_offset_percent": stop_offset * 100,
                }
            else:  # direction == "short"
                stop_price = h * (1 + stop_offset)

                logger.info(f"📍 SHORT пінбар знайдено:")
                logger.info(f"   - Екстремум (HIGH): {h:.8f}")
                logger.info(f"   - Стоп: {stop_price:.8f}")

                return {
                    "timestamp": timestamp,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "direction": "short",
                    "entry_price": l,
                    "stop_price": stop_price,
                    "extremum_price": h,
                    "invalidate_price": h,
                    "body": abs(c - o),
                    "total_range": h - l,
                    "is_extremum": True,
                    "candle_index": i,
                    "stop_offset_percent": stop_offset * 100,
                }

        except (ValueError, IndexError) as e:
            logger.error(f"Error detecting pinbar: {e}")
            continue

    return None
