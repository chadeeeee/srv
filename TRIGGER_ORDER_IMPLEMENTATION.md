# Trigger Order Implementation - Bybit Strategy Bot

## 📋 Огляд змін

Реалізовано функціональність **TRIGGER ORDER** (Conditional Order) для Bybit API замість звичайних Market Orders. Це дозволяє боту автоматично відкривати позиції коли ціна досягає певного рівня.

---

## 🎯 Що таке Trigger Order?

**Trigger Order** (або Conditional Order) - це ордер, який **чекає** досягнення певної ціни перш ніж бути розміщеним на ринку.

### Параметри Bybit API:
```python
{
    "orderType": "Market",           # Після активації виконується як Market
    "triggerPrice": "65000.00",      # Ціна активації
    "triggerDirection": 1 або 2,     # 1 = зростання, 2 = падіння
    "triggerBy": "LastPrice",        # Тригер по LastPrice
    "orderFilter": "StopOrder"       # КРИТИЧНО: Conditional order
}
```

---

## 🔄 Логіка роботи

### ⚡ КРИТИЧНО: Trigger Order ТІЛЬКИ для SHORT

**TRIGGER ORDER використовується ТІЛЬКИ для SHORT позицій.**
**LONG позиції відкриваються звичайним LIMIT ORDER.**

### Для SHORT позиції (TRIGGER ORDER):
- `triggerDirection = 2` (спрацює при **падінні** до triggerPrice)
- Trigger ставиться на **LOW свічки**
- Приклад: поточна ціна 66000 → trigger 65900 (LOW) → ордер активується коли ціна падає до 65900

### Для LONG позиції (LIMIT ORDER):
- Звичайний limit order на поточній ціні
- Виконується одразу без очікування trigger
- Приклад: поточна ціна 64000 → limit на 64000 → ордер виконується негайно

### 🤔 Чому така різниця?

**SHORT через trigger:**
- Чекаємо RSI перекупленості (>= 70)
- Потім чекаємо **підтвердження** розвороту вниз (ціна досягла LOW свічки)
- Це дає більшу впевненість що розворот почався

**LONG без trigger:**
- Чекаємо RSI перепроданості (<= 30)
- Відкриваємо позицію **одразу** на поточній ціні
- Перепроданість вже відбулась, не потрібно додаткове підтвердження

---

## 📁 Файли які були змінені

### 1. `trading/signal_handler.py`

#### Додано функції:

##### `place_trigger_order()`
```python
async def place_trigger_order(self, pair, direction, trigger_price, quantity_usdt, 
                              stop_loss=None, take_profit=None, metadata=None):
    """
    Розміщує TRIGGER ORDER (Conditional Order) на Bybit
    
    Параметри:
    - pair: символ (наприклад "BTCUSDT")
    - direction: "long" або "short"
    - trigger_price: ціна активації ордеру
    - quantity_usdt: розмір позиції в USDT
    - stop_loss: ціна стоп-лосса (опціонально)
    - take_profit: ціна тейк-профіта (опціонально)
    - metadata: додаткові дані для відстеження
    
    Повертає:
    - dict з orderId при успіху
    - None при помилці
    """
```

**Особливості:**
- Автоматично визначає `triggerDirection` на основі напрямку торгівлі
- Квантизує trigger_price згідно з tickSize інструменту
- Валідує stop_loss відносно trigger_price
- Додає ордер до системи моніторингу
- Відправляє Telegram повідомлення про розміщення

##### `_watch_trigger_order()`
```python
async def _watch_trigger_order(self, pair, order_id, poll_interval=5):
    """
    Моніторинг trigger order до його активації та подальше відстеження позиції
    
    Етапи:
    1. Чекає активації trigger order (перевіряє статус кожні 5 секунд)
    2. При активації перевіряє чи відкрилась позиція
    3. НЕГАЙНО встановлює Stop Loss (MAX_SL_ATTEMPTS=5 спроб)
    4. Запускає RSI Take-Profit трекер
    5. Переключається на моніторинг позиції
    """
```

**Логіка моніторингу:**
- Опитує статус ордеру через `/v5/order/realtime`
- Відстежує статуси: `New`, `Untriggered`, `Filled`, `Cancelled`
- При виконанні перевіряє відкриття позиції
- Автоматично встановлює SL після відкриття
- Логує всі етапи для прозорості

---

### 2. `analysis/signals.py`

#### Зміни в логіці Gravity2:

**До:**
```python
if requires_pullback and consolidated:
    # ... отримуємо свічку ...
    result = await signal_handler.place_market_order(
        pair=pair,
        direction=trade_direction,
        quantity_usdt=qty_usdt,
        stop_loss=stop_loss_price,
        take_profit=None,
        metadata=metadata
    )
```

**Після:**
```python
if requires_pullback and consolidated:
    # ... отримуємо свічку ...
    
    # Визначаємо trigger price на основі екстремума свічки
    if trade_direction == 'long':
        trigger_price = c_high  # HIGH свічки для LONG
    else:
        trigger_price = c_low   # LOW свічки для SHORT
    
    result = await signal_handler.place_trigger_order(
        pair=pair,
        direction=trade_direction,
        trigger_price=trigger_price,  # ✅ НОВИЙ ПАРАМЕТР
        quantity_usdt=qty_usdt,
        stop_loss=stop_loss_price,
        take_profit=None,
        metadata=metadata
    )
```

---

## 🎮 Як це працює в стратегії Gravity2

### Сценарій 1: SHORT після перекупленості (TRIGGER ORDER)

1. **Отримуємо сигнал з каналу:** "LONG (підтримка)"
2. **Чекаємо RSI перекупленості:** RSI >= 70 (з БД)
3. **При досягненні екстремуму RSI:**
   - Отримуємо останню закриту свічку
   - Визначаємо HIGH і LOW свічки
   - Для SHORT: trigger = LOW свічки
4. **Розміщуємо Trigger Order:**
   ```
   Поточна ціна: 65000
   Trigger (LOW): 64900
   Stop Loss: 65300 (на екстремумі свічки вище)
   ```
5. **Система чекає активації:**
   - Опитує статус кожні 5 секунд
   - Логує поточну відстань до trigger
6. **Коли ціна падає до 64900:**
   - Trigger активується
   - Відкривається SHORT позиція
   - НЕГАЙНО встановлюється SL на 65300
   - Запускається RSI трекер

### Сценарій 2: LONG після перепроданості (LIMIT ORDER)

1. **Отримуємо сигнал з каналу:** "SHORT (опір)"
2. **Чекаємо RSI перепроданості:** RSI <= 30 (з БД)
3. **При досягненні екстремуму RSI:**
   - Отримуємо останню закриту свічку
   - Визначаємо HIGH і LOW свічки
   - Для LONG: одразу входимо на поточній ціні
4. **Розміщуємо Limit Order:**
   ```
   Поточна ціна: 64000
   Entry (limit): 64000
   Stop Loss: 63700 (на екстремумі свічки нижче)
   ```
5. **Ордер виконується одразу:**
   - LONG позиція відкривається
   - НЕГАЙНО встановлюється SL на 63700
   - Запускається RSI трекер

---

## 📊 Переваги Trigger Order (для SHORT)

### ✅ Чому trigger тільки для SHORT:

1. **SHORT потребує підтвердження розвороту:**
   - RSI перекупленість не гарантує негайного падіння
   - Trigger на LOW свічки підтверджує що падіння почалось
   - Краща ціна входу після підтвердження тренду

2. **LONG входить одразу:**
   - RSI перепроданість часто означає дно
   - Відкладання входу може призвести до пропуску руху
   - Limit на поточній ціні дає швидкий вхід

### Порівняння:

| Аспект | SHORT (Trigger) | LONG (Limit) |
|--------|----------------|--------------|
| Тип ордеру | Conditional (StopOrder) | Limit (GTC) |
| Вхід | При досягненні LOW свічки | Одразу на поточній ціні |
| Підтвердження | Так (падіння до trigger) | Ні (RSI достатньо) |
| Ризик помилкового входу | Нижчий | Середній |
| Швидкість виконання | Затримка до trigger | Негайно |

---

## 🧪 Тестування

### Запуск тесту:
```bash
python test_trigger_order.py
```

### Що тестується:
1. **Валідація параметрів:**
   - Правильність triggerDirection для LONG/SHORT
   - Квантизація ціни згідно з tickSize
   - Валідація stop_loss відносно trigger

2. **Розміщення тестового ордеру:**
   - Реальний запит до Bybit testnet
   - Перевірка відповіді API
   - Моніторинг статусу ордеру

---

## 📝 Приклад логів

### Розміщення Trigger Order:
```
🚀 GRAVITY2: Умови виконані - розміщуємо TRIGGER ORDER
   Стратегія: Quantum Gravity2
   Сигнал з каналу: long
   Направлення угоди: short

📊 Остання закрита свічка (TF 5m):
   HIGH: 65150.50
   LOW: 64920.00

🎯 SHORT: Trigger на LOW свічки: 64920.00

📏 Розрахований Stop Loss:
   Свічка: H=65150.50 L=64920.00
   Stop Loss: 65350.00

📝 Розміщення TRIGGER ORDER для Gravity2...
   Напрямок: SHORT
   Trigger ціна: 64920.00
   Stop Loss: 65350.00
   RSI: 72.45

🎯 TRIGGER ORDER: BTCUSDT SHORT
   Поточна ціна: 65080.00
   Trigger ціна: 64920.00
   Trigger direction: 2 (падіння)
   Розмір: 0.015 (100.00 USDT @ 10x)

✅ TRIGGER ORDER розміщено: BTCUSDT SHORT
```

### Активація Trigger:
```
📡 Моніторинг trigger order для BTCUSDT
   Order ID: 1234567890
   Trigger: 64920.00
   Direction: SHORT

🔍 Trigger order 1234567890 не знайдено в активних - перевіряємо позицію...

✅ Trigger order BTCUSDT АКТИВОВАНО - позиція відкрита!
   Фактична ціна входу: 64918.50

🛡 Встановлення SL: 65350.00
✅ SL встановлено після активації trigger

✅ TRIGGER АКТИВОВАНО
━━━━━━━━━━━━━━━━━━
Монета: BTCUSDT
Напрямок: SHORT
Trigger: 64920.00
Вхід: 64918.50
Stop Loss: 65350.00
Статус: Позиція відкрита

🎯 Запуск RSI трекера для BTCUSDT
```

---

## ⚠️ Важливі нюанси

### 1. Різниця між triggerDirection
- `triggerDirection = 1`: ціна **зросла** до triggerPrice
- `triggerDirection = 2`: ціна **впала** до triggerPrice

### 2. Stop Loss валідація
- Для LONG: `stop_loss < trigger_price`
- Для SHORT: `stop_loss > trigger_price`

### 3. Моніторинг активації
- Перевіряємо статус кожні 5 секунд
- Якщо ордер зник зі списку - перевіряємо позицію
- Встановлюємо SL НЕГАЙНО після відкриття

### 4. Metadata
- Додаємо `"trigger_price"` для відстеження
- Додаємо `"order_type": "trigger"` для розрізнення

---

## 🔧 Налаштування

Всі налаштування RSI беруться з БД через `_get_rsi_settings_db()`:
- `rsi_low`: поріг для SHORT виходу (дефолт 30)
- `rsi_high`: поріг для LONG виходу (дефолт 70)
- `rsi_period`: період RSI (дефолт 14)
- `rsi_interval`: таймфрейм для RSI (жорстко 1m)

---

## 📞 Telegram повідомлення

### При розміщенні:
```
🎯 TRIGGER ORDER РОЗМІЩЕНО
━━━━━━━━━━━━━━━━━━
Монета: BTCUSDT
Напрямок: SHORT
Поточна ціна: 65080.00
Trigger: 64920.00
Stop Loss: 65350.00
Тип: Conditional (Gravity2)
```

### При активації:
```
✅ TRIGGER АКТИВОВАНО
━━━━━━━━━━━━━━━━━━
Монета: BTCUSDT
Напрямок: SHORT
Trigger: 64920.00
Вхід: 64918.50
Stop Loss: 65350.00
Статус: Позиція відкрита
```

---

## 🚀 Наступні кроки

1. Протестувати на testnet з реальними сигналами
2. Переконатися що trigger orders коректно активуються
3. Перевірити логи встановлення SL
4. Переконатися що RSI трекер працює після активації
5. За потреби налаштувати параметри в БД

---

## 📚 Документація Bybit API

- [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order)
- [Order Types](https://bybit-exchange.github.io/docs/v5/order/order-type)
- [Conditional Orders](https://bybit-exchange.github.io/docs/v5/order/conditional-order)

---

**Дата:** 15.11.2025  
**Версія:** 1.0.0  
**Статус:** ✅ Готово до тестування
