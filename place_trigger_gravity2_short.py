import asyncio
from trading.signal_handler import SignalHandler
from trading.strategies import QuantumGravity2Strategy
from config import CONFIG
from loguru import logger

async def place_gravity2_short_trigger(pair: str, wait_for_rsi: bool = True):
    """
    Розміщує SHORT trigger order для GRAVITY2 стратегії
    
    Параметри:
    - pair: Торгова пара (наприклад "BTCUSDT", "ETHUSDT")
    - wait_for_rsi: Чи чекати на RSI перекупленість перед розміщенням (True за замовчуванням)
    
    Логіка:
    1. Отримує останню закриту свічку з timeframe з БД
    2. Якщо wait_for_rsi=True: чекає на RSI >= rsi_high (перекупленість)
    3. Розміщує TRIGGER ORDER на LOW свічки
    4. Stop Loss встановлюється на HIGH свічки (з буфером)
    5. Trigger спрацює коли ціна впаде до LOW свічки
    """
    
    handler = SignalHandler()
    
    try:
        # Ініціалізація стратегії Gravity2
        strategy = QuantumGravity2Strategy(channel_id=-1003193138774)
        
        logger.info(f"═══════════════════════════════════════")
        logger.info(f"🎯 GRAVITY2 SHORT TRIGGER ORDER")
        logger.info(f"═══════════════════════════════════════")
        logger.info(f"Монета: {pair}")
        logger.info(f"Стратегія: {strategy.name}")
        logger.info(f"Канал: {strategy.channel_id}")
        logger.info(f"Очікування RSI: {'ТАК' if wait_for_rsi else 'НІ'}")
        logger.info(f"───────────────────────────────────────")
        
        # Отримуємо поточну ціну
        current_price = await handler.get_real_time_price(pair)
        if not current_price:
            logger.error(f"❌ Не вдалося отримати ціну для {pair}")
            return
        
        logger.info(f"💰 Поточна ціна: {current_price:.8f}")
        
        # Отримуємо RSI налаштування з БД
        rsi_settings = await handler._get_rsi_settings_db()
        rsi_high = float(rsi_settings.get('rsi_high', 70))
        rsi_low = float(rsi_settings.get('rsi_low', 30))
        rsi_period = int(rsi_settings.get('rsi_period', 14))
        rsi_interval = '1'  # ЖОРСТКО ФІКСУЄМО 1 ХВИЛИНУ для RSI
        
        logger.info(f"📊 RSI Налаштування (з БД):")
        logger.info(f"   RSI High (перекупленість): {rsi_high}")
        logger.info(f"   RSI Low (перепроданість): {rsi_low}")
        logger.info(f"   RSI Period: {rsi_period}")
        logger.info(f"   RSI Interval: {rsi_interval}m")
        
        # Отримуємо поточний RSI
        from analysis.signals import get_rsi
        current_rsi, _ = await get_rsi(pair, interval=rsi_interval, period=rsi_period)
        logger.info(f"📈 Поточний RSI: {current_rsi:.2f}")
        
        # Якщо потрібно чекати на RSI перекупленість
        if wait_for_rsi:
            if current_rsi < rsi_high:
                logger.info(f"⏳ Очікування RSI перекупленості для SHORT...")
                logger.info(f"   Поточний RSI: {current_rsi:.2f}")
                logger.info(f"   Потрібно: >= {rsi_high}")
                logger.info(f"   Відстань: {rsi_high - current_rsi:.2f} пунктів")
                logger.info(f"───────────────────────────────────────")
                logger.info(f"💡 Підказка: Запустіть скрипт знову коли RSI досягне {rsi_high}")
                logger.info(f"   або використайте wait_for_rsi=False для негайного розміщення")
                return
            else:
                logger.info(f"✅ RSI ПЕРЕКУПЛЕНІСТЬ: {current_rsi:.2f} >= {rsi_high}")
        else:
            logger.info(f"⚠ Пропуск перевірки RSI (wait_for_rsi=False)")
        
        # Отримуємо timeframe з БД
        from utils.settings_manager import get_settings
        settings = get_settings()
        tf_display = settings.get('timeframe', '1m')
        tf_api = handler._parse_timeframe_for_api(tf_display)
        
        logger.info(f"⏱ Timeframe: {tf_display} (API: {tf_api})")
        
        # Отримуємо останню закриту свічку
        candles = await handler.get_klines(pair, tf_api, limit=3)
        if not candles or len(candles) < 2:
            logger.error(f"❌ Не вдалося отримати свічки для {pair}")
            return
        
        last_closed = candles[-2]  # Остання закрита свічка
        candle_high = float(last_closed[2])
        candle_low = float(last_closed[3])
        candle_open = float(last_closed[1])
        candle_close = float(last_closed[4])
        
        logger.info(f"🕯 Сигнальна свічка (TF {tf_display}):")
        logger.info(f"   Open:  {candle_open:.8f}")
        logger.info(f"   High:  {candle_high:.8f}")
        logger.info(f"   Low:   {candle_low:.8f}")
        logger.info(f"   Close: {candle_close:.8f}")
        
        # ✅ ДЛЯ SHORT: 
        # - Trigger на LOW свічки (ціна має впасти до LOW)
        # - Stop Loss на HIGH свічки (вище для SHORT)
        trigger_price = candle_low
        
        # Отримуємо stop loss з буфером
        extremum_data = await handler.get_candle_extremum_from_db_timeframe(
            pair, 
            "short",
            pinbar_candle_index=None
        )
        
        if not extremum_data:
            logger.error(f"❌ Не вдалося розрахувати stop loss для {pair}")
            return
        
        stop_loss = extremum_data["stop_price"]
        
        logger.info(f"───────────────────────────────────────")
        logger.info(f"🎯 Параметри TRIGGER ORDER:")
        logger.info(f"   Trigger ціна: {trigger_price:.8f} (LOW свічки)")
        logger.info(f"   Stop Loss:    {stop_loss:.8f} (HIGH свічки + буфер)")
        logger.info(f"   Напрямок:     SHORT")
        
        # Валідація: для SHORT trigger має бути НИЖЧЕ поточної ціни
        if trigger_price >= current_price:
            logger.warning(f"⚠ УВАГА: Trigger ціна ({trigger_price:.8f}) >= поточної ({current_price:.8f})")
            logger.warning(f"   Для SHORT trigger має бути НИЖЧЕ поточної ціни")
            logger.warning(f"   Можливо свічка вже закрилась або ринок змінився")
        
        # Валідація: Stop Loss має бути ВИЩЕ trigger price для SHORT
        if stop_loss <= trigger_price:
            logger.error(f"❌ КРИТИЧНА ПОМИЛКА: Stop Loss <= Trigger для SHORT!")
            logger.error(f"   Stop Loss: {stop_loss:.8f}")
            logger.error(f"   Trigger:   {trigger_price:.8f}")
            return
        
        # Розрахунок відстаней
        trigger_distance = current_price - trigger_price
        trigger_distance_percent = (trigger_distance / current_price) * 100
        sl_distance = stop_loss - trigger_price
        sl_distance_percent = (sl_distance / trigger_price) * 100
        
        logger.info(f"📏 Відстані:")
        logger.info(f"   До trigger:  {trigger_distance:.8f} ({trigger_distance_percent:.2f}%)")
        logger.info(f"   SL від entry: {sl_distance:.8f} ({sl_distance_percent:.2f}%)")
        
        # Розрахунок розміру позиції
        from trading.risk_manager import RiskManager
        risk_manager = RiskManager()
        quantity_usdt = await risk_manager.calculate_position_size(pair, trigger_price)
        
        if not quantity_usdt or quantity_usdt < 10:
            quantity_usdt = 10
            logger.warning(f"⚠ Використовую мінімальний розмір: {quantity_usdt} USDT")
        else:
            logger.info(f"💰 Розмір позиції: {quantity_usdt} USDT")
        
        # Метадані
        metadata = {
            "source": "gravity2_short_trigger_manual",
            "signal_level": current_price,
            "direction": "short",
            "timeframe": tf_display,
            "strategy": strategy.name,
            "channel_id": strategy.channel_id,
            "candle_high": candle_high,
            "candle_low": candle_low,
            "rsi_value": current_rsi,
            "rsi_threshold": rsi_high,
            "trigger_type": "gravity2"
        }
        
        logger.info(f"───────────────────────────────────────")
        logger.info(f"📝 Розміщення TRIGGER ORDER...")
        
        # Розміщуємо trigger order
        result = await handler.place_trigger_order(
            pair=pair,
            direction="short",
            trigger_price=trigger_price,
            quantity_usdt=quantity_usdt,
            stop_loss=stop_loss,
            take_profit=None,
            metadata=metadata
        )
        
        if result:
            order_id = result.get("orderId")
            logger.success(f"✅ TRIGGER ORDER РОЗМІЩЕНО!")
            logger.success(f"═══════════════════════════════════════")
            logger.success(f"📋 Деталі ордера:")
            logger.success(f"   Order ID:     {order_id}")
            logger.success(f"   Монета:       {pair}")
            logger.success(f"   Напрямок:     SHORT")
            logger.success(f"   Trigger:      {trigger_price:.8f}")
            logger.success(f"   Stop Loss:    {stop_loss:.8f}")
            logger.success(f"   Розмір:       {quantity_usdt} USDT")
            logger.success(f"   RSI:          {current_rsi:.2f}")
            logger.success(f"═══════════════════════════════════════")
            logger.info(f"")
            logger.info(f"🎯 Trigger спрацює коли ціна впаде до {trigger_price:.8f}")
            logger.info(f"📊 Перевірити статус: python check_orders.py")
            logger.info(f"⏳ Ордер залишається активним на біржі")
        else:
            logger.error(f"❌ Не вдалося розмістити trigger order")
        
    except Exception as e:
        logger.error(f"❌ Помилка: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await handler.close()

async def main():
    """
    Головна функція - розміщує SHORT trigger order для GRAVITY2
    
    Використання:
    1. За замовчуванням: чекає на RSI перекупленість
       python place_trigger_gravity2_short.py
    
    2. Без очікування RSI (негайне розміщення):
       Змініть wait_for_rsi=False у виклику функції нижче
    """
    
    # ═══════════════════════════════════════════════════════════
    # НАЛАШТУВАННЯ - Змініть тут параметри
    # ═══════════════════════════════════════════════════════════
    
    pair = "ETHUSDT"           # Торгова пара
    wait_for_rsi = True        # True = чекати RSI перекупленість
                               # False = розмістити негайно
    
    # ═══════════════════════════════════════════════════════════
    
    await place_gravity2_short_trigger(pair, wait_for_rsi)

if __name__ == "__main__":
    asyncio.run(main())
