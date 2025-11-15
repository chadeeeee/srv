"""
Перевірка активних ордерів на Bybit
"""
import asyncio
from trading.signal_handler import SignalHandler
from loguru import logger

async def check_orders():
    handler = SignalHandler()
    
    try:
        logger.info("=" * 60)
        logger.info("ПЕРЕВІРКА АКТИВНИХ ОРДЕРІВ")
        logger.info("=" * 60)
        
        # Перевіряємо активні ордери
        params = {
            "category": "linear",
            "symbol": "BTCUSDT",
            "limit": 20
        }
        
        logger.info("\n📋 Активні ордери (realtime):")
        result = await handler._signed_request("GET", "/v5/order/realtime", params)
        
        if result and result.get("retCode") == 0:
            orders = result.get("result", {}).get("list", [])
            if orders:
                for order in orders:
                    logger.info(f"\n   Order ID: {order.get('orderId')}")
                    logger.info(f"   Symbol: {order.get('symbol')}")
                    logger.info(f"   Side: {order.get('side')}")
                    logger.info(f"   Type: {order.get('orderType')}")
                    logger.info(f"   Status: {order.get('orderStatus')}")
                    logger.info(f"   Qty: {order.get('qty')}")
                    logger.info(f"   Price: {order.get('price')}")
                    logger.info(f"   Trigger Price: {order.get('triggerPrice')}")
                    logger.info(f"   Order Filter: {order.get('orderFilter')}")
                    logger.info(f"   Stop Order Type: {order.get('stopOrderType')}")
            else:
                logger.info("   ⚠ Немає активних ордерів")
        else:
            logger.error(f"❌ Помилка: {result}")
        
        # Перевіряємо історію
        logger.info("\n📜 Історія ордерів:")
        result2 = await handler._signed_request("GET", "/v5/order/history", params)
        
        if result2 and result2.get("retCode") == 0:
            history = result2.get("result", {}).get("list", [])
            if history:
                for order in history[:5]:  # Показуємо останні 5
                    logger.info(f"\n   ═══ ОРДЕР ═══")
                    logger.info(f"   Order ID: {order.get('orderId')}")
                    logger.info(f"   Symbol: {order.get('symbol')}")
                    logger.info(f"   Side: {order.get('side')}")
                    logger.info(f"   Order Type: {order.get('orderType')}")
                    logger.info(f"   Status: {order.get('orderStatus')}")
                    logger.info(f"   Qty: {order.get('qty')}")
                    logger.info(f"   Price: {order.get('price')}")
                    logger.info(f"   Trigger Price: {order.get('triggerPrice')}")
                    logger.info(f"   Trigger By: {order.get('triggerBy')}")
                    logger.info(f"   Order Filter: {order.get('orderFilter')}")
                    logger.info(f"   Stop Order Type: {order.get('stopOrderType')}")
                    logger.info(f"   Reject Reason: {order.get('rejectReason', 'N/A')}")
                    logger.info(f"   Created: {order.get('createdTime')}")
                    logger.info(f"   Updated: {order.get('updatedTime')}")
            else:
                logger.info("   ⚠ Немає історії")
        else:
            logger.error(f"❌ Помилка історії: {result2}")
        
        # Перевіряємо позиції
        logger.info("\n📊 Поточні позиції:")
        result3 = await handler._signed_request("GET", "/v5/position/list", {"category": "linear", "symbol": "BTCUSDT"})
        
        if result3 and result3.get("retCode") == 0:
            positions = result3.get("result", {}).get("list", [])
            if positions:
                for pos in positions:
                    size = float(pos.get("size", 0) or 0)
                    if size > 0:
                        logger.info(f"\n   Position:")
                        logger.info(f"   Symbol: {pos.get('symbol')}")
                        logger.info(f"   Side: {pos.get('side')}")
                        logger.info(f"   Size: {pos.get('size')}")
                        logger.info(f"   Entry: {pos.get('avgPrice')}")
                        logger.info(f"   Stop Loss: {pos.get('stopLoss')}")
            else:
                logger.info("   ⚠ Немає активних позицій")
        
    except Exception as e:
        logger.error(f"Помилка: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await handler.close()

if __name__ == "__main__":
    asyncio.run(check_orders())
