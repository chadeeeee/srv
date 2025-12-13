import asyncio
import datetime
import time

import numpy as np
import talib
from loguru import logger

from analysis.candle_analysis import get_klines
from config import CONFIG

DB_HOST = CONFIG["DB_HOST"]
DB_PORT = CONFIG["DB_PORT"]
DB_NAME = CONFIG["DB_NAME"]
DB_USER = CONFIG["DB_USER"]
DB_PASS = CONFIG["DB_PASS"]

_active_monitors = {}


def stop_monitoring(pair):
    from analysis.trigger_strategy import stop_monitoring as ts_stop

    return ts_stop(pair)


def stop_all_monitoring():
    from analysis.trigger_strategy import stop_all_monitoring as ts_stop_all

    return ts_stop_all()


def _slope_percent(values):
    x = np.arange(len(values))
    slope, _ = np.polyfit(x, values, 1)
    return (slope / values[-1]) * 100 if values[-1] != 0 else 0


def analyze_trend(candles):
    closes = np.array([float(c[4]) for c in candles], dtype=float)
    highs = np.array([float(c[2]) for c in candles], dtype=float)
    lows = np.array([float(c[3]) for c in candles], dtype=float)

    if len(closes) < 15:
        return {"trend": "sideways", "bounce": "up", "strength": "weak"}

    short_len = min(len(closes), 20)
    mid_len = min(len(closes), 60)
    long_len = min(len(closes), 120)

    slope_short = _slope_percent(closes[-short_len:])
    slope_mid = _slope_percent(closes[-mid_len:])
    slope_long = _slope_percent(closes[-long_len:])

    hh = sum(highs[i] > highs[i - 1] for i in range(len(highs) - short_len, len(highs)))
    ll = sum(lows[i] < lows[i - 1] for i in range(len(lows) - short_len, len(lows)))
    volatility = np.std(closes[-mid_len:]) / closes[-1] * 100 if mid_len > 1 else 0

    if slope_short > 0 and slope_mid > 0:
        trend = "up"
        bounce = "down"
        strength_score = sum([slope_short > 0.2, slope_mid > 0.12, slope_long > 0.05, hh > ll, volatility < 3])
    elif slope_short < 0 and slope_mid < 0:
        trend = "down"
        bounce = "up"
        strength_score = sum([slope_short < -0.2, slope_mid < -0.12, slope_long < -0.05, ll > hh, volatility < 3])
    else:
        trend = "sideways"
        bounce = "up"
        strength_score = 1

    strength = "strong" if strength_score >= 4 else ("medium" if strength_score >= 2 else "weak")
    return {"trend": trend, "bounce": bounce, "strength": strength}


def calculate_rsi_talib(closes, period=14):
    if len(closes) < period + 1:
        return 50
    closes_arr = np.array(closes, dtype=float)
    if np.any(np.isnan(closes_arr)) or np.any(~np.isfinite(closes_arr)):
        return 50
    rsi_series = talib.RSI(closes_arr, timeperiod=period)
    rsi = float(rsi_series[-1])
    return 50 if np.isnan(rsi) else rsi


async def get_rsi(pair, interval="1", period=14):
    interval_api = _parse_timeframe_to_api("1")
    candles = await get_klines(pair, interval_api, max(200, period * 2))
    if not candles or len(candles) < period + 1:
        return 50, datetime.datetime.now()
    closes = []
    for candle in candles:
        try:
            closes.append(float(candle[4]))
        except (ValueError, TypeError):
            return 50, datetime.datetime.now()
    rsi = calculate_rsi_talib(closes, period)
    return rsi, datetime.datetime.now()


# async def show_rsi_for_pairs(pairs_list):
#     from trading.signal_handler import SignalHandler

#     logger.info("📊 ═══════════ ПОТОЧНИЙ RSI ═══════════")
#     signal_handler = SignalHandler()
#     try:
#         rsi_settings = await signal_handler._get_rsi_settings_db()
#         rsi_period = int(rsi_settings.get("rsi_period") or 14)
#         rsi_high = float(rsi_settings.get("rsi_high") or 70)
#         rsi_low = float(rsi_settings.get("rsi_low") or 30)

#         for pair in pairs_list:
#             try:
#                 rsi_value, _ = await get_rsi(pair, interval="1", period=rsi_period)
#                 if rsi_value >= rsi_high:
#                     status = f"🔴 ПЕРЕКУПЛЕНІСТЬ (>= {rsi_high})"
#                 elif rsi_value <= rsi_low:
#                     status = f"🟢 ПЕРЕПРОДАНІСТЬ (<= {rsi_low})"
#                 else:
#                     status = "⚪ НЕЙТРАЛЬНА ЗОНА"
#                 logger.info(f"   {pair}: RSI={rsi_value:.2f} {status}")
#             except Exception as e:
#                 logger.warning(f"   {pair}: ⚠ Помилка RSI - {e}")
#     except Exception as e:
#         logger.error(f"Помилка RSI: {e}")
#     finally:
#         await signal_handler.close()
#     logger.info("📊 ══════════════════════════════════════")


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
    return value if value in valid else "1"


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


def get_settings():
    from utils.settings_manager import get_settings as gs

    return gs()
