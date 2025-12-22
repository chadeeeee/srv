import os
import sys
import time

from loguru import logger

log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(log_dir, exist_ok=True)

logger.remove()


logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="INFO",
    colorize=True,
)


logger.add(
    os.path.join(log_dir, "trading_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="30 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG",
    encoding="utf-8",
)


trade_logger = logger.bind(trade=True)


def trade_signal(pair, price, direction):
    trade_logger.info("Trade signal received", pair=pair, price=price, direction=direction)


def signal_from_channel(pair, price, direction, channel_id, strategy_name):
    """Log signal with channel and strategy info"""
    strategy_display = strategy_name.name if hasattr(strategy_name, "name") else str(strategy_name)

    trade_logger.info(
        "Signal from channel",
        channel_id=channel_id,
        strategy=strategy_display,
        pair=pair,
        price=price,
        direction=direction.upper(),
    )


def pin_bar_search(pair, level, direction):
    logger.debug("Pin bar search", pair=pair, level=level, direction=direction)


def price_trend(pair, trend, bounce):
    logger.debug("Price trend update", pair=pair, trend=trend, bounce=bounce)


def rsi_update(pair, rsi):
    logger.debug("RSI update", pair=pair, rsi=rsi)


def limit_order_placed(direction, pair, price, info=""):
    trade_logger.info("Limit order placed", direction=direction.upper(), pair=pair, price=price, info=info)


def stop_loss_set(pair, price, percent):
    trade_logger.info("Stop loss set", pair=pair, price=price, percent=f"{percent:.2f}%")


def take_profit_set(pair, price, percent):
    trade_logger.info("Take profit set", pair=pair, price=price, percent=f"{percent:.2f}%")


def order_executed(pair, direction, price):
    trade_logger.info("Order executed", direction=direction.upper(), pair=pair, price=price)


def position_closed_by_stop(pair, direction, close_price, stop_price, distance):
    trade_logger.warning(
        "Position closed by stop loss",
        pair=pair,
        direction=direction.upper(),
        close_price=f"{close_price:.6f}",
        stop_price=f"{stop_price:.6f}",
        distance=f"{distance:.6f}",
    )


def position_closed_manually(pair, direction, close_price, stop_price, distance):
    trade_logger.info(
        "Position closed manually",
        pair=pair,
        direction=direction.upper(),
        close_price=f"{close_price:.6f}",
        stop_price=f"{stop_price:.6f}",
        distance=f"{distance:.6f}",
    )


def reentry_allowed(pair, reason=""):
    trade_logger.info("Re-entry allowed", pair=pair, reason=reason)


def reentry_blocked(pair, reason=""):
    trade_logger.warning("Re-entry blocked", pair=pair, reason=reason)


def pattern_detected(pair, pattern, direction):
    global _last_pattern_log
    try:
        key = (str(pair), str(pattern), str(direction))
        now = time.time()
        window = float(os.getenv("PATTERN_LOG_THROTTLE", "10"))
        last = _last_pattern_log.get(key, 0.0)
        if now - last >= window:
            logger.info("Pattern detected", pattern=pattern, pair=pair, direction=direction)
            _last_pattern_log[key] = now
        else:
            logger.debug("Pattern detected (throttled)", pattern=pattern, pair=pair, direction=direction)
    except Exception:
        logger.info("Pattern detected", pattern=pattern, pair=pair, direction=direction)


_last_pattern_log = {}


def setting_changed(name, value):
    logger.info("Setting changed", name=name, value=value)


def divergence_detected(pair, div_type, base_rsi, current_rsi, base_price=None, current_price=None):
    try:
        context = {
            "pair": pair,
            "type": div_type,
            "base_rsi": f"{float(base_rsi):.2f}",
            "current_rsi": f"{float(current_rsi):.2f}",
        }
        if base_price is not None and current_price is not None:
            context.update({"base_price": f"{float(base_price):.6f}", "current_price": f"{float(current_price):.6f}"})
        logger.info("Divergence detected", **context)
    except Exception:
        logger.info("Divergence detected", pair=pair, type=div_type)


def rsi_auto_close(pair, direction, rsi_value, threshold):
    try:
        trade_logger.info(
            "Auto-close by RSI",
            pair=pair,
            direction=direction.upper(),
            rsi=f"{float(rsi_value):.2f}",
            threshold=f"{float(threshold):.2f}",
        )
    except Exception:
        trade_logger.warning("Auto-close by RSI", pair=pair, direction=direction.upper())


def external_position_detected(pair, direction, size, entry_price, source="manual"):
    try:
        trade_logger.warning(
            "External position detected",
            pair=pair,
            direction=direction.upper(),
            size=f"{float(size):.4f}",
            entry_price=f"{float(entry_price):.6f}",
            source=source,
        )
    except Exception:
        trade_logger.warning("External position detected", pair=pair)


def external_position_detect(pair, direction, entry_price, leverage=1):
    """Log when external (manual) position is detected"""
    try:
        trade_logger.warning(
            "External position detected",
            pair=pair,
            direction=direction.upper(),
            entry_price=f"{float(entry_price):.6f}",
            leverage=f"{leverage}x",
        )
    except Exception:
        trade_logger.warning("External position detected", pair=pair)


def external_position_monitoring_started(pair, direction):
    """Log when RSI monitoring starts for an external position"""
    trade_logger.info("RSI monitoring started", pair=pair, direction=direction.upper())


def candle_high_low_info(pair, timeframe, high, low):
    """Log candle High/Low information for a specific timeframe"""
    try:
        logger.debug(
            "Candle High/Low", pair=pair, timeframe=timeframe, high=f"{float(high):.6f}", low=f"{float(low):.6f}"
        )
    except Exception as e:
        logger.debug("Candle High/Low error", pair=pair, error=str(e))


def timeframe_changed(old_value, new_value):
    """Log timeframe change"""
    try:
        old_display = old_value if old_value else "N/A"
        logger.info("Timeframe changed", old=old_display, new=new_value)
    except Exception:
        logger.info("Timeframe changed", old=old_value, new=new_value)


def extremum_detected(pair, extremum_type, high, low):
    """Log when extremum candle is detected"""
    try:
        logger.info(
            "Extremum detected", pair=pair, type=extremum_type, high=f"{float(high):.8f}", low=f"{float(low):.8f}"
        )
    except Exception as e:
        logger.debug("Extremum detection error", pair=pair, error=str(e))


def extremum_validation_failed(pair, reason):
    """Log when extremum validation fails"""
    logger.debug("Extremum validation failed", pair=pair, reason=reason)


def signal_candle_detected(pair, direction, ohlc, is_extremum, extremum_type):
    """Log when valid signal candle is detected"""
    try:
        logger.info(
            "Signal candle detected",
            pair=pair,
            direction=direction.upper(),
            open=f"{ohlc['open']:.8f}",
            high=f"{ohlc['high']:.8f}",
            low=f"{ohlc['low']:.8f}",
            close=f"{ohlc['close']:.8f}",
            is_extremum=is_extremum,
            extremum_type=extremum_type,
        )
    except Exception as e:
        logger.error("Signal candle logging error", error=str(e))


def limit_order_params(pair, direction, entry_price, signal_candle_ohlc, stop_price, stop_buffer_info):
    """Log limit order parameters with signal candle details"""
    try:
        trade_logger.info(
            "Limit order parameters",
            pair=pair,
            direction=direction.upper(),
            signal_high=f"{signal_candle_ohlc['high']:.8f}",
            signal_low=f"{signal_candle_ohlc['low']:.8f}",
            entry_price=f"{entry_price:.8f}",
            stop_price=f"{stop_price:.8f}",
            stop_buffer=stop_buffer_info,
        )
    except Exception as e:
        logger.error("Order parameters logging error", error=str(e))


def stop_calculation_details(
    pair, direction, signal_extremum, buffer_type, buffer_value, final_stop, signal_candle_time=None
):
    """Log detailed stop loss calculation"""
    try:
        context = {
            "pair": pair,
            "direction": direction.upper(),
            "signal_extremum": f"{signal_extremum:.8f}",
            "buffer_type": buffer_type or "NONE",
            "buffer_value": buffer_value if buffer_value else 0,
            "final_stop": f"{final_stop:.8f}",
        }
        if signal_candle_time:
            try:
                import datetime

                dt = datetime.datetime.fromtimestamp(int(signal_candle_time) / 1000)
                context["signal_candle_time"] = dt.strftime("%H:%M:%S %d.%m.%Y")
            except Exception:
                pass
        trade_logger.info("Stop loss calculation", **context)
    except Exception as e:
        logger.error("Stop calculation logging error", error=str(e))


def manual_position_opened(pair, direction, entry_price, stop_price, candle_high, candle_low, timeframe):
    """Log when manual position is opened with SL set"""
    try:
        trade_logger.info(
            "Manual position opened",
            pair=pair,
            direction=direction.upper(),
            entry_price=f"{entry_price:.8f}",
            stop_price=f"{stop_price:.8f}",
            candle_high=f"{candle_high:.8f}",
            candle_low=f"{candle_low:.8f}",
            timeframe=timeframe,
        )
    except Exception as e:
        logger.error("Manual position logging error", error=str(e))


def position_closed_with_details(pair, direction, reason, close_price, rsi_value=None, entry_price=None):
    """Log position close with all details including RSI"""
    try:
        context = {"pair": pair, "direction": direction.upper(), "reason": reason, "close_price": f"{close_price:.8f}"}
        if entry_price:
            pnl_percent = (
                ((close_price - entry_price) / entry_price * 100)
                if direction == "long"
                else ((entry_price - close_price) / entry_price * 100)
            )
            context["pnl"] = f"{pnl_percent:+.2f}%"
        if rsi_value is not None:
            context["rsi"] = f"{rsi_value:.2f}"
        trade_logger.info("Position closed", **context)
    except Exception as e:
        logger.error("Position close logging error", error=str(e))


def limit_order_placed_with_candle(pair, direction, limit_price, candle_high, candle_low, timeframe):
    """Log limit order placement with candle details"""
    try:
        trade_logger.info(
            "Limit order placed with candle",
            pair=pair,
            direction=direction.upper(),
            limit_price=f"{limit_price:.8f}",
            timeframe=timeframe,
            candle_high=f"{candle_high:.8f}",
            candle_low=f"{candle_low:.8f}",
        )
    except Exception as e:
        logger.error("Limit order logging error", error=str(e))


def bybit_ping_info(pair, ping_ms, server_time_ms, local_time_ms):
    """Log Bybit API ping information"""
    try:
        import datetime

        server_dt = datetime.datetime.fromtimestamp(server_time_ms / 1000)
        local_dt = datetime.datetime.fromtimestamp(local_time_ms / 1000)

        context = {
            "pair": pair,
            "ping_ms": ping_ms,
            "server_time": server_dt.strftime("%H:%M:%S.%f")[:-3],
            "local_time": local_dt.strftime("%H:%M:%S.%f")[:-3],
        }

        logger.info("Bybit ping info", **context)

        if ping_ms > 1000:
            logger.warning("High ping detected", ping_ms=ping_ms, threshold=1000)
        elif ping_ms > 500:
            logger.warning("Elevated ping detected", ping_ms=ping_ms, threshold=500)
    except Exception as e:
        logger.error("Bybit ping logging error", error=str(e))


def rsi_settings_from_db(pair, rsi_low, rsi_high, rsi_period, rsi_interval):
    """Log RSI settings loaded from database"""
    try:
        logger.info(
            "RSI settings from DB",
            pair=pair,
            rsi_low=f"{float(rsi_low):.1f}",
            rsi_high=f"{float(rsi_high):.1f}",
            period=int(rsi_period),
            interval=str(rsi_interval),
        )
    except Exception as e:
        logger.debug("RSI settings logging error", error=str(e))
