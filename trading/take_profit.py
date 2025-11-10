import ssl
import time
import hmac
import hashlib
import urllib.parse
import aiohttp
import asyncio
import numpy as np
import talib
import json
from typing import Callable, Optional, Awaitable
from config import CONFIG
from utils.settings_manager import get_settings, update_setting, get_bybit_base_url
from utils.logger import divergence_detected, rsi_auto_close
from loguru import logger


class TakeProfit:
    def __init__(self):
        self.api_key = CONFIG['BYBIT_API_KEY']
        self.api_secret = CONFIG['BYBIT_API_SECRET']
        self.base_url = get_bybit_base_url()
        self.recv_window = "20000"
        settings = get_settings()
        self.rsi_period = int(settings.get('rsi_period', CONFIG.get('RSI_PERIOD', 14)))
        self.rsi_overbought = float(settings.get('rsi_high', CONFIG.get('RSI_OVERBOUGHT', 70)))
        self.rsi_oversold = float(settings.get('rsi_low', CONFIG.get('RSI_OVERSOLD', 30)))
        self._time_offset = settings.get('timestamp_offset', 0)
        self.active_positions = {}
        self._session = None
        self._timeout = aiohttp.ClientTimeout(total=10)
        self._session_timeout = aiohttp.ClientTimeout(total=10)  # Added for compatibility
        self._sem = asyncio.Semaphore(5)
        self._closed = False

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
                                    await asyncio.to_thread(update_setting, 'timestamp_offset', offset)
                                    return offset
            except Exception:
                pass

            await asyncio.sleep(retry_delay * (2 ** attempt))

        logger.warning("Could not get server time, using local time")
        return 0

    async def close(self):
        """Properly close all resources"""
        if self._closed:
            return
        self._closed = True
        
        if self._session and not self._session.closed:
            try:
                await self._session.close()
                # Wait for proper cleanup
                await asyncio.sleep(0.25)
            except Exception as e:
                logger.warning(f"Error closing TakeProfit session: {e}")
        self._session = None

    async def _get_session(self):
        """Get or create session with proper state checking"""
        if self._closed:
            # Reset closed state and create new session
            self._closed = False
            self._session = None
        
        if self._session is None or self._session.closed:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_context, limit=30, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(connector=connector, timeout=self._timeout)
            logger.debug("TakeProfit session created")
        return self._session

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
                                    await asyncio.to_thread(update_setting, 'timestamp_offset', offset)
                                    return offset
            except Exception:
                pass

            await asyncio.sleep(retry_delay * (2 ** attempt))

        logger.warning("Could not get server time, using local time")
        return 0

    async def _signed_request(self, method, endpoint, params=None):
        if self._time_offset == 0:
            settings = get_settings()
            stored_offset = settings.get('timestamp_offset', 0)
            if stored_offset:
                self._time_offset = stored_offset
            else:
                self._time_offset = await self.get_server_time_offset()

        url = f"{self.base_url}{endpoint}"
        timestamp = str(int(time.time() * 1000) + self._time_offset)
        params = params or {}
        sorted_params = {k: params[k] for k in sorted(params)}

        if method.upper() == "GET":
            payload = urllib.parse.urlencode(sorted_params)
        else:
            payload = json.dumps(sorted_params, separators=(',', ':'))

        pre_sign = f"{timestamp}{self.api_key}{self.recv_window}{payload}"
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            pre_sign.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
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
                    if method.upper() == "GET":
                        resp = await session.get(url, params=sorted_params, headers=headers)
                    else:
                        resp = await session.post(url, data=payload, headers=headers)
                    async with resp:
                        text = await resp.text()
                    if not text.strip():
                        if attempt == max_retries:
                            print(f"Empty response {method} {endpoint}")
                            return None
                        await asyncio.sleep(base_delay * attempt)
                        continue
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        if attempt == max_retries:
                            print(f"Bad JSON {endpoint}: {text[:120]}")
                            return None
                        await asyncio.sleep(base_delay * attempt)
                        continue
                    if data.get('retCode') == 10002:
                        self._time_offset = await self.get_server_time_offset()
                        if attempt < max_retries:
                            await asyncio.sleep(base_delay * attempt)
                            continue
                    return data
            except aiohttp.ClientError as e:
                if attempt == max_retries:
                    print(f"Client error {endpoint}: {e}")
                    return None
                await asyncio.sleep(base_delay * attempt)
            except Exception as e:
                if attempt == max_retries:
                    print(f"Unexpected {endpoint}: {e}")
                    return None
                await asyncio.sleep(base_delay * attempt)
        return None

    async def get_klines(self, pair, interval, limit=200):
        params = {
            "category": "linear",
            "symbol": pair,
            "interval": interval,
            "limit": str(limit)
        }
        result = await self._signed_request("GET", "/v5/market/kline", params)
        if result and result.get("retCode") == 0:
            # Bybit повертає свічки від найновішої до найстарішої
            # Перевертаємо для хронологічного порядку (старіша → новіша)
            candles = result["result"]["list"]
            return list(reversed(candles))
        return None

    async def calculate_rsi(self, pair, interval: str = '1'):
        candles = await self.get_klines(pair, interval, self.rsi_period * 2 + 1)
        if not candles or len(candles) < self.rsi_period + 1: 
            logger.warning(f"Insufficient data for RSI calculation for {pair}")
            return None
        # Свічки вже в хронологічному порядку після get_klines
        closes = [float(c[4]) for c in candles]
        rsi_array = talib.RSI(np.array(closes), timeperiod=self.rsi_period)
        
        if not np.isnan(rsi_array[-1]):
            return rsi_array[-1]
        return None

    async def get_position(self, pair):
        params = {
            "category": "linear",
            "symbol": pair
        }
        result = await self._signed_request("GET", "/v5/position/list", params)
        if result and result.get("retCode") == 0:
            positions = result["result"]["list"]
            for pos in positions:
                if pos["side"] in ["Buy", "Sell"] and float(pos["size"]) > 0:
                    return pos
        return None

    async def close_position(self, pair, side):
        position = await self.get_position(pair)
        if not position:
            logger.info(f"No active position found for {pair}")
            return False

        qty = float(position["size"])
        params = {
            "category": "linear",
            "symbol": pair,
            "side": "Sell" if side == "Buy" else "Buy",
            "orderType": "Market",
            "qty": str(qty),
            "reduceOnly": True,
            "positionIdx": 0
        }
        result = await self._signed_request("POST", "/v5/order/create", params)
        if result and result.get("retCode") == 0:
            logger.info(f"Position closed for {pair} due to RSI condition")
            return True
        logger.error(f"Failed to close position for {pair}: {result}")
        return False

    async def set_stop_loss(self, pair, price, direction):
        position = await self.get_position(pair)
        if not position:
            logger.error(f"No active position to set stop loss for {pair}")
            return False

        qty = float(position["size"])
        side = "Sell" if direction == "long" else "Buy"
        params = {
            "category": "linear",
            "symbol": pair,
            "side": side,
            "orderType": "Limit",
            "price": str(price),
            "qty": str(qty),
            "timeInForce": "GTC",
            "reduceOnly": True,
            "stopLoss": str(price),
            "slTriggerBy": "LastPrice"
        }
        result = await self._signed_request("POST", "/v5/order/create", params)
        if result and result.get("retCode") == 0:
            return True
        logger.error(f"Failed to set stop loss for {pair}: {result}")
        return False

    def _find_rsi_extremes(self, rsi_values, extreme_type='high'):
        extremes = []
        
        for i in range(2, len(rsi_values) - 2):
            if extreme_type == 'high':
                is_extreme = (rsi_values[i] > rsi_values[i-1] and 
                            rsi_values[i] > rsi_values[i-2] and
                            rsi_values[i] > rsi_values[i+1] and 
                            rsi_values[i] > rsi_values[i+2])
            else:
                is_extreme = (rsi_values[i] < rsi_values[i-1] and 
                            rsi_values[i] < rsi_values[i-2] and
                            rsi_values[i] < rsi_values[i+1] and 
                            rsi_values[i] < rsi_values[i+2])
            
            if is_extreme:
                extremes.append({'index': i, 'value': rsi_values[i]})
        
        return extremes

    def _find_divergence_base(self, extremes, extreme_type='high'):
        threshold = self.rsi_overbought if extreme_type == 'high' else self.rsi_oversold
        
        for i in range(len(extremes) - 2, -1, -1):
            if extreme_type == 'high' and extremes[i]['value'] >= threshold:
                return extremes[i]
            elif extreme_type == 'low' and extremes[i]['value'] <= threshold:
                return extremes[i]
        
        return None

    async def detect_bullish_divergence(self, pair):
        try:
            candles = await self.get_klines(pair, '1', self.rsi_period * 3 + 10)
            if not candles or len(candles) < self.rsi_period + 10:
                return None
            
            closes = [float(c[4]) for c in candles]
            lows = [float(c[3]) for c in candles]
            
            rsi_values = []
            for i in range(self.rsi_period, len(closes)):
                window = closes[i - self.rsi_period:i + 1]
                rsi = talib.RSI(np.array(window), timeperiod=self.rsi_period)[-1]
                if not np.isnan(rsi):
                    rsi_values.append(rsi)
            
            if len(rsi_values) < 10:
                return None
            
            rsi_lows = self._find_rsi_extremes(rsi_values, 'low')
            
            if len(rsi_lows) < 2:
                return None
            
            current_rsi_low = rsi_lows[-1]
            base_rsi_low = self._find_divergence_base(rsi_lows, 'low')
            
            if not base_rsi_low:
                return None
            
            current_price_low = lows[current_rsi_low['index'] + self.rsi_period]
            base_price_low = lows[base_rsi_low['index'] + self.rsi_period]
            
            if current_price_low < base_price_low and current_rsi_low['value'] > base_rsi_low['value']:
                divergence_detected(pair, 'BULLISH', base_rsi_low['value'], 
                                  current_rsi_low['value'], base_price_low, current_price_low)
                return {
                    'type': 'bullish',
                    'base_rsi': base_rsi_low['value'],
                    'current_rsi': current_rsi_low['value'],
                    'base_price': base_price_low,
                    'current_price': current_price_low
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Error detecting bullish divergence for {pair}: {e}")
            return None

    async def detect_bearish_divergence(self, pair):
        try:
            candles = await self.get_klines(pair, '1', self.rsi_period * 3 + 10)
            if not candles or len(candles) < self.rsi_period + 10:
                return None
            
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            
            rsi_values = []
            for i in range(self.rsi_period, len(closes)):
                window = closes[i - self.rsi_period:i + 1]
                rsi = talib.RSI(np.array(window), timeperiod=self.rsi_period)[-1]
                if not np.isnan(rsi):
                    rsi_values.append(rsi)
            
            if len(rsi_values) < 10:
                return None
            
            rsi_highs = self._find_rsi_extremes(rsi_values, 'high')
            
            if len(rsi_highs) < 2:
                return None
            
            current_rsi_high = rsi_highs[-1]
            base_rsi_high = self._find_divergence_base(rsi_highs, 'high')
            
            if not base_rsi_high:
                return None
            
            current_price_high = highs[current_rsi_high['index'] + self.rsi_period]
            base_price_high = highs[base_rsi_high['index'] + self.rsi_period]
            
            if current_price_high > base_price_high and current_rsi_high['value'] < base_rsi_high['value']:
                divergence_detected(pair, 'BEARISH', base_rsi_high['value'], 
                                  current_rsi_high['value'], base_price_high, current_price_high)
                return {
                    'type': 'bearish',
                    'base_rsi': base_rsi_high['value'],
                    'current_rsi': current_rsi_high['value'],
                    'base_price': base_price_high,
                    'current_price': current_price_high
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Error detecting bearish divergence for {pair}: {e}")
            return None

    async def track_position_rsi(self,
                                 pair,
                                 direction,
                                 on_exit: Optional[Callable[[str, str, str, Optional[dict]], Awaitable[None]]] = None,
                                 rsi_high_override: Optional[float] = None,
                                 rsi_low_override: Optional[float] = None,
                                 rsi_period_override: Optional[int] = None,
                                 rsi_interval_override: Optional[str] = None,
                                 poll_seconds: int = 60):
        self.active_positions[pair] = True
        notified_reason = None

        settings = get_settings()
        
        # ВИПРАВЛЕНО: Беремо значення з БД
        rsi_high = float(rsi_high_override if rsi_high_override is not None else settings.get('rsi_exit_long', 70))
        rsi_low = float(rsi_low_override if rsi_low_override is not None else settings.get('rsi_exit_short', 30))
        
        if rsi_period_override is not None:
            try:
                self.rsi_period = int(rsi_period_override)
            except Exception:
                pass
        rsi_interval = str(rsi_interval_override if rsi_interval_override is not None else settings.get('rsi_interval', '1'))

        logger.info(f"RSI TP трекер для {pair}:")
        logger.info(f"   - Направлення: {direction.upper()}")
        logger.info(f"   - RSI High (для закриття LONG): {rsi_high}")
        logger.info(f"   - RSI Low (для закриття SHORT): {rsi_low}")
        logger.info(f"   - RSI Period: {self.rsi_period}")
        logger.info(f"   - RSI Interval: {rsi_interval}")

        # ДОДАНО: Лічильник для періодичних оновлень
        check_count = 0
        last_telegram_update = 0
        telegram_update_interval = 10  # Кожні 10 перевірок (10 хвилин якщо poll_seconds=60)

        try:
            while pair in self.active_positions:
                try:
                    rsi = await self.calculate_rsi(pair, interval=rsi_interval)
                    if rsi is None:
                        await asyncio.sleep(poll_seconds)
                        continue

                    check_count += 1

                    position = await self.get_position(pair)
                    if not position:
                        logger.info(f"Position closed for {pair}")
                        del self.active_positions[pair]
                        if on_exit:
                            try:
                                notified_reason = 'position_closed'
                                await on_exit(pair, direction, notified_reason, {"reason": "missing_position"})
                            except Exception as exc:
                                logger.error(f"RSI exit callback failed: {exc}")
                        break

                    # ДОДАНО: Періодичне оновлення статусу в TG
                    if check_count - last_telegram_update >= telegram_update_interval:
                        try:
                            from telegram_bot import notify_user
                            if direction == "long":
                                distance = rsi_high - rsi
                                status = f"RSI МОНІТОРИНГ: {pair}\n" \
                                        f"━━━━━━━━━━━━━━━━━━\n" \
                                        f"Напрямок: LONG\n" \
                                        f"Поточний RSI: {rsi:.2f}\n" \
                                        f"Поріг закриття: {rsi_high}\n" \
                                        f"До закриття: {distance:.2f} пунктів\n" \
                                        f"Умова: RSI >= {rsi_high}"
                            else:
                                distance = rsi - rsi_low
                                status = f"RSI МОНІТОРИНГ: {pair}\n" \
                                        f"━━━━━━━━━━━━━━━━━━\n" \
                                        f"Напрямок: SHORT\n" \
                                        f"Поточний RSI: {rsi:.2f}\n" \
                                        f"Поріг закриття: {rsi_low}\n" \
                                        f"До закриття: {distance:.2f} пунктів\n" \
                                        f"Умова: RSI <= {rsi_low}"
                            
                            await notify_user(status)
                            last_telegram_update = check_count
                        except Exception as e:
                            logger.debug(f"Не вдалося відправити RSI статус: {e}")

                    # ВИПРАВЛЕНО: LONG закривається при RSI >= rsi_high, SHORT при RSI <= rsi_low
                    if direction == "long" and rsi >= rsi_high:
                        logger.warning(f"RSI TAKE-PROFIT: {pair} LONG")
                        logger.warning(f"   - Поточний RSI: {rsi:.2f}")
                        logger.warning(f"   - Поріг RSI High: {rsi_high}")
                        logger.warning(f"   - Умова: RSI {rsi:.2f} >= {rsi_high}")
                        
                        rsi_auto_close(pair, direction, rsi, rsi_high)
                        success = await self.close_position(pair, "Buy")
                        
                        del self.active_positions[pair]
                        if on_exit:
                            context = {"rsi": rsi, "success": success, "method": "market_order", "rsi_threshold": rsi_high}
                            try:
                                notified_reason = 'take_profit'
                                await on_exit(pair, direction, notified_reason, context)
                            except Exception as exc:
                                logger.error(f"RSI exit callback failed: {exc}")
                        break
                        
                    elif direction == "short" and rsi <= rsi_low:
                        logger.warning(f"RSI TAKE-PROFIT: {pair} SHORT")
                        logger.warning(f"   - Поточний RSI: {rsi:.2f}")
                        logger.warning(f"   - Поріг RSI Low: {rsi_low}")
                        logger.warning(f"   - Умова: RSI {rsi:.2f} <= {rsi_low}")
                        
                        rsi_auto_close(pair, direction, rsi, rsi_low)
                        success = await self.close_position(pair, "Sell")
                        
                        del self.active_positions[pair]
                        if on_exit:
                            context = {"rsi": rsi, "success": success, "method": "market_order", "rsi_threshold": rsi_low}
                            try:
                                notified_reason = 'take_profit'
                                await on_exit(pair, direction, notified_reason, context)
                            except Exception as exc:
                                logger.error(f"RSI exit callback failed: {exc}")
                        break
                    else:
                        # Логування поточного стану RSI (кожні 10 перевірок)
                        if check_count % 10 == 0:
                            if direction == "long":
                                logger.info(f"{pair} LONG: RSI={rsi:.2f} (чекаємо >= {rsi_high}, залишилось {rsi_high - rsi:.2f})")
                            else:
                                logger.info(f"{pair} SHORT: RSI={rsi:.2f} (чекаємо <= {rsi_low}, залишилось {rsi - rsi_low:.2f})")

                except asyncio.CancelledError:
                    logger.info(f"RSI tracking cancelled: {pair}")
                    raise
                except Exception as e:
                    logger.error(f"Error in RSI tracking: {pair} - {e}")
                
                await asyncio.sleep(poll_seconds)

        except asyncio.CancelledError:
            logger.info(f"RSI tracker cancelled for {pair}")
        finally:
            if on_exit and pair not in self.active_positions and not notified_reason:
                try:
                    await on_exit(pair, direction, 'tracking_stopped', {"reason": "tracker_finished"})
                except Exception as exc:
                    logger.error(f"RSI exit finalizer failed: {exc}")
            
            logger.debug(f"RSI tracker finished for {pair}")
# ...existing code...
    async def _get_live_price(self, symbol):
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        try:
            result = await self._signed_request("GET", "/v5/market/tickers", params)
            if result and result.get("retCode") == 0:
                tickers = result.get("result", {}).get("list", [])
                if tickers:
                    return float(tickers[0].get("lastPrice", 0))
            return None
        except Exception as e:
            logger.error(f"Error getting live price for {symbol}: {e}")
            return None


async def main():
    tp = TakeProfit()
    await tp.track_position_rsi("BTCUSDT", "long")
    await tp.close()


if __name__ == "__main__":
    asyncio.run(main())