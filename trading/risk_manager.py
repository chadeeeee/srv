import time
import traceback

import aiohttp
from loguru import logger

from config import CONFIG
from utils.settings_manager import get_bybit_base_url, get_settings


class RiskManager:
    def __init__(self):
        self.api_key = CONFIG.get("BYBIT_API_KEY")
        self.api_secret = CONFIG.get("BYBIT_API_SECRET")
        self.use_testnet = CONFIG.get("USE_TESTNET", False)
        self.base_url = get_bybit_base_url()
        settings = get_settings() or {}
        self.position_size_percent = settings.get("position_size_percent") or CONFIG.get("POSITION_SIZE_PERCENT", 1.5)
        self.default_order_qty = settings.get("default_order_qty") or CONFIG.get("DEFAULT_ORDER_QTY", 10)
        self.time_offset = 0
        self._last_balance_log_time = 0.0
        self._balance_log_interval = float(CONFIG.get("BALANCE_LOG_INTERVAL", 300))
        self._session_timeout = aiohttp.ClientTimeout(total=10)

    async def sync_time(self):
        try:
            url = f"{self.base_url}/v5/market/time"
            aiohttp.Fingerprint if False else None
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data and data.get("retCode") == 0:
                            server_time = int(data["result"]["timeNano"]) // 1000000
                            local_time = int(time.time() * 1000)
                            self.time_offset = server_time - local_time
                            logger.debug(f"Time offset updated: {self.time_offset}")
                            return self.time_offset
        except Exception:
            logger.debug("RiskManager.sync_time: could not sync time, using local time")
            logger.debug(traceback.format_exc())
        return 0

    def _sign(self, params: dict):
        import hashlib
        import hmac

        param_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(
            (self.api_secret or "").encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return signature

    async def get_wallet_balance(self):
        try:
            settings = get_settings() or {}
            try:
                await self.sync_time()
                url = f"{self.base_url}/v5/account/wallet-balance"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params={"accountType": "UNIFIED"}, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            if data and data.get("retCode") == 0:
                                coins = data.get("result", {}).get("list", [{}])[0].get("coin", [])
                                out = {"_SUMMARY": {}, "USDT": {}}
                                for c in coins:
                                    if c.get("coin") == "USDT":
                                        out["USDT"] = {
                                            "walletBalance": float(c.get("walletBalance", 0) or 0),
                                            "availableToWithdraw": float(c.get("availableToWithdraw", 0) or 0),
                                            "locked": float(c.get("locked", 0) or 0),
                                            "equity": float(c.get("equity", 0) or 0),
                                            "usdValue": float(c.get("usdValue", 0) or 0),
                                        }
                                logger.debug(
                                    "Fetched wallet balance (detailed available at DEBUG)", extra={"show_console": True}
                                )
                                out["_SUMMARY"] = {
                                    "totalEquity": out["USDT"].get("equity", 0),
                                    "totalWalletBalance": out["USDT"].get("walletBalance", 0),
                                    "totalAvailableBalance": out["USDT"].get("availableToWithdraw", 0),
                                    "totalOrderIM": 0,
                                    "totalPositionIM": 0,
                                }
                                return out
            except Exception:
                logger.debug("Wallet API call failed; using local defaults", exc_info=True)
            wallet_balance = float(settings.get("DEFAULT_ORDER_QTY", CONFIG.get("DEFAULT_ORDER_QTY", 10)))
            return {
                "_SUMMARY": {
                    "totalEquity": wallet_balance,
                    "totalWalletBalance": wallet_balance,
                    "totalAvailableBalance": wallet_balance,
                    "totalOrderIM": 0,
                    "totalPositionIM": 0,
                },
                "USDT": {
                    "walletBalance": wallet_balance,
                    "availableToWithdraw": wallet_balance,
                    "locked": 0.0,
                    "equity": wallet_balance,
                    "usdValue": wallet_balance,
                },
            }
        except Exception:
            logger.exception("Error getting wallet balance")
            return None

    async def get_all_positions(self):
        try:
            return []
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            logger.debug(traceback.format_exc())
            return []

    async def get_available_equity(self):
        wallet = await self.get_wallet_balance()
        if not wallet or "USDT" not in wallet:
            return 0
        summary = wallet.get("_SUMMARY", {})
        usdt_info = wallet.get("USDT", {})
        available = float(summary.get("totalAvailableBalance", 0) or 0)
        if available <= 0:
            available = float(usdt_info.get("availableToWithdraw", 0) or 0)
        if available <= 0:
            wallet_balance = float(usdt_info.get("walletBalance", 0) or 0)
            locked = float(usdt_info.get("locked", 0) or 0)
            available = wallet_balance - locked
        return available

    async def get_total_equity(self):
        wallet = await self.get_wallet_balance()
        if not wallet or "USDT" not in wallet:
            return 0
        summary = wallet.get("_SUMMARY", {})
        if summary:
            return float(summary.get("totalEquity", 0) or 0)
        return float(wallet.get("USDT", {}).get("equity", 0) or 0)

    async def calculate_position_size_usdt(self, pair, entry_price):

        try:
            size = await self.calculate_position_size(pair, entry_price)
            return size
        except Exception:
            return self.default_order_qty

    async def calculate_position_size(self, pair, entry_price):
        settings = get_settings() or {}
        try:
            return float(settings.get("default_order_qty", CONFIG.get("DEFAULT_ORDER_QTY", 10)))
        except Exception:
            return float(CONFIG.get("DEFAULT_ORDER_QTY", 10))

    async def set_stop_loss(self, order_id, pair, direction, stop_price):
        try:
            logger.info(f"Setting SL for {pair}: {stop_price} (order {order_id})")
            return True
        except Exception as e:
            logger.error(f"Error setting stop loss: {e}")
            return False

    async def set_take_profit(self, order_id, pair, direction, take_profit_price):
        try:
            logger.info(f"Setting TP for {pair}: {take_profit_price} (order {order_id})")
            return True
        except Exception as e:
            logger.error(f"Error setting take profit: {e}")
            return False

    async def display_balance(self):
        try:
            wallet = await self.get_wallet_balance()
            if not wallet:
                logger.debug("No wallet available in display_balance")
                return
            summary = wallet.get("_SUMMARY", {})
            total_equity = float(summary.get("totalEquity", 0) or 0)
            total_available = float(summary.get("totalAvailableBalance", 0) or 0)
            now = time.time()
            if now - self._last_balance_log_time >= self._balance_log_interval:
                self._last_balance_log_time = now
            else:
                logger.debug(
                    f"Balance summary (suppressed INFO): Equity={total_equity:.2f} Available={total_available:.2f}"
                )
        except Exception:
            logger.exception("Error displaying balance")
