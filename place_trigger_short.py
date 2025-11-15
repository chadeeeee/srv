import asyncio
from trading.signal_handler import SignalHandler
from loguru import logger

async def place_short_trigger():
    """Розміщує SHORT trigger order і завершується"""
    
    handler = SignalHandler()
    
    try:
        test_pair = "BTCUSDT"
        test_direction = "short"
        
        # Отримуємо поточну ціну
        current_price = await handler.get_real_time_price(test_pair)
        if not current_price:
            logger.error(f"Не вдалося отримати ціну для {test_pair}")
            return
        
        logger.info(f"💰 Поточна ціна {test_pair}: {current_price}")
        
        # Для SHORT: trigger нижче поточної, SL вище
        trigger_price = current_price * 0.998  # -0.2%
        stop_loss = current_price * 1.005  # +0.5%
        
        logger.info(f"🎯 Trigger: {trigger_price:.2f}")
        logger.info(f"🛡 Stop Loss: {stop_loss:.2f}")
        
        # Метадані
        metadata = {
            "source": "manual_trigger",
            "test": True
        }
        
        # Розміщуємо trigger order
        result = await handler.place_trigger_order(
            pair=test_pair,
            direction=test_direction,
            trigger_price=trigger_price,
            quantity_usdt=10,
            stop_loss=stop_loss,
            take_profit=None,
            metadata=metadata
        )
        
        if result:
            order_id = result.get("orderId")
            logger.success(f"✅ Trigger order розміщено!")
            logger.success(f"📝 Order ID: {order_id}")
            logger.info(f"⏳ Ордер залишається активним на біржі")
            logger.info(f"📊 Перевірити статус: python check_orders.py")
        
    except Exception as e:
        logger.error(f"❌ Помилка: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await handler.close()

if __name__ == "__main__":
    asyncio.run(place_short_trigger())
