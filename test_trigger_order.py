import asyncio
from trading.signal_handler import SignalHandler
from config import CONFIG
from loguru import logger

async def test_trigger_order():
    """Тест розміщення trigger order"""
    
    # Ініціалізація handler
    handler = SignalHandler()
    
    try:
        
        # Тестові параметри
        test_pair = "ETHUSDT"
        test_direction = "short"  # Для SHORT: trigger спрацює при зростанні
        
        # Отримуємо поточну ціну
        current_price = await handler.get_real_time_price(test_pair)
        if not current_price:
            logger.error(f"Не вдалося отримати ціну для {test_pair}")
            return
        
        
        # ✅ ВИПРАВЛЕНО: Для SHORT trigger має бути НИЖЧЕ поточної ціни (на LOW свічки)
        # Коли ціна впаде до trigger - відкриється SHORT
        trigger_price = current_price * 0.998  # -0.2% (нижче поточної)
        stop_loss = current_price * 1.005  # +0.5% від ПОТОЧНОЇ (вище для SHORT)
        
        
        # Метадані
        metadata = {
            "source": "test_trigger",
            "test": True
        }
        
        
        # Розміщуємо trigger order
        result = await handler.place_trigger_order(
            pair=test_pair,
            direction=test_direction,
            trigger_price=trigger_price,
            quantity_usdt=10,  # Мінімальна сума для тесту
            stop_loss=stop_loss,
            take_profit=None,
            metadata=metadata
        )
        
        if result:
            logger.info("✅ Trigger order розміщено і залишається активним")
            logger.info("⏳ Натисніть Ctrl+C для завершення (ордер залишиться активним)")
            
            # Чекаємо необмежено довго, щоб ордер залишився активним
            try:
                while True:
                    await asyncio.sleep(60)
            except KeyboardInterrupt:
                logger.info("⚠️ Зупинка моніторингу. Trigger order залишається активним на біржі.")
        
    except KeyboardInterrupt:
        logger.info("⚠️ Тест зупинено. Trigger order залишається активним на біржі.")
    except Exception as e:
        logger.error(f"❌ Помилка тесту: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        # Закриваємо handler БЕЗ скасування ордера
        await handler.close()

async def test_trigger_validation():
    """Тест валідації параметрів trigger order"""
    
    handler = SignalHandler()
    
    try:
        
        test_pair = "ETHUSDT"
        
        # Отримуємо поточну ціну
        current_price = await handler.get_real_time_price(test_pair)
        if not current_price:
            logger.error(f"Не вдалося отримати ціну для {test_pair}")
            return
        
        
    except Exception as e:
        logger.error(f"❌ Помилка валідації: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await handler.close()

async def main():
    """Головна функція тестування"""

    
    # Тест 1: Валідація
    await test_trigger_validation()
    
    # Невелика пауза
    await asyncio.sleep(2)
    
    # Тест 2: Реальне розміщенн
    try:
        await asyncio.sleep(3)
        await test_trigger_order()
    except KeyboardInterrupt:
        return

if __name__ == "__main__":
    asyncio.run(main())
