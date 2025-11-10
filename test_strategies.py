"""
Тестовий файл для перевірки логіки стратегій
"""
from trading.strategies import QuantumPremium2Strategy, QuantumGravity2Strategy

def test_premium2_strategy():
    """Тест стратегії Premium2"""
    print("=" * 80)
    print("ТЕСТ СТРАТЕГІЇ 1: QUANTUM PREMIUM2")
    print("=" * 80)
    
    strategy = QuantumPremium2Strategy(-1002990245762)
    
    print(f"\n📌 Назва стратегії: {strategy.name}")
    print(f"📌 ID каналу: {strategy.channel_id}")
    print(f"\n✅ Чекаємо рівня: {strategy.should_wait_for_level()}")
    print(f"✅ Потрібен відкат: {strategy.requires_pullback()}")
    print(f"✅ Потрібен розворотний сигнал: {strategy.needs_reversal_signal()}")
    
    print("\n" + "-" * 80)
    print("СЦЕНАРІЙ 1: Сигнал LONG (підтримка)")
    print("-" * 80)
    direction_signal = "long"
    trade_direction = strategy.get_entry_direction(direction_signal)
    search_zone = strategy.get_pinbar_search_zone(100.0, direction_signal)
    
    print(f"📊 Сигнал з каналу: {direction_signal.upper()}")
    print(f"📈 Напрямок угоди: {trade_direction.upper()}")
    print(f"\n🔍 Параметри пошуку:")
    for key, value in search_zone.items():
        print(f"   • {key}: {value}")
    
    print("\n📝 ЛОГІКА:")
    print(f"   1. Чекаємо торкання рівня підтримки")
    print(f"   2. Від свічки торкання аналізуємо N свічок")
    print(f"   3. Шукаємо пінбар з оновленням МІНІМУМУ (new_low)")
    print(f"   4. Вхід на HIGH свічки")
    print(f"   5. Стоп на LOW свічки")
    print(f"   6. Відкриваємо LONG позицію")
    
    print("\n" + "-" * 80)
    print("СЦЕНАРІЙ 2: Сигнал SHORT (опір)")
    print("-" * 80)
    direction_signal = "short"
    trade_direction = strategy.get_entry_direction(direction_signal)
    search_zone = strategy.get_pinbar_search_zone(100.0, direction_signal)
    
    print(f"📊 Сигнал з каналу: {direction_signal.upper()}")
    print(f"📉 Напрямок угоди: {trade_direction.upper()}")
    print(f"\n🔍 Параметри пошуку:")
    for key, value in search_zone.items():
        print(f"   • {key}: {value}")
    
    print("\n📝 ЛОГІКА:")
    print(f"   1. Чекаємо торкання рівня опору")
    print(f"   2. Від свічки торкання аналізуємо N свічок")
    print(f"   3. Шукаємо пінбар з оновленням МАКСИМУМУ (new_high)")
    print(f"   4. Вхід на LOW свічки")
    print(f"   5. Стоп на HIGH свічки")
    print(f"   6. Відкриваємо SHORT позицію")


def test_gravity2_strategy():
    """Тест стратегії Gravity2"""
    print("\n\n" + "=" * 80)
    print("ТЕСТ СТРАТЕГІЇ 2: QUANTUM GRAVITY2")
    print("=" * 80)
    
    strategy = QuantumGravity2Strategy(-1003193138774)
    
    print(f"\n📌 Назва стратегії: {strategy.name}")
    print(f"📌 ID каналу: {strategy.channel_id}")
    print(f"\n✅ Чекаємо рівня: {strategy.should_wait_for_level()}")
    print(f"✅ Потрібен відкат: {strategy.requires_pullback()}")
    print(f"✅ Потрібен розворотний сигнал: {strategy.needs_reversal_signal()}")
    
    print("\n" + "-" * 80)
    print("СЦЕНАРІЙ 1: Сигнал LONG (підтримка)")
    print("-" * 80)
    direction_signal = "long"
    trade_direction = strategy.get_entry_direction(direction_signal)
    search_zone = strategy.get_pinbar_search_zone(100.0, direction_signal)
    
    print(f"📊 Сигнал з каналу: {direction_signal.upper()}")
    print(f"⚠️  ІНВЕРТОВАНИЙ напрямок угоди: {trade_direction.upper()}")
    print(f"\n🔍 Параметри пошуку:")
    for key, value in search_zone.items():
        print(f"   • {key}: {value}")
    
    print("\n📝 ЛОГІКА:")
    print(f"   1. Отримали сигнал LONG (підтримка)")
    print(f"   2. НЕ чекаємо торкання рівня")
    print(f"   3. Чекаємо RSI ПЕРЕКУПЛЕНОСТІ (RSI >= rsi_high)")
    print(f"   4. Вхід на LOW свічки")
    print(f"   5. Стоп на HIGH свічки")
    print(f"   6. Відкриваємо SHORT позицію ⚠️")
    print(f"\n   🔄 ВАЖЛИВО: Сигнал LONG → Угода SHORT!")
    
    print("\n" + "-" * 80)
    print("СЦЕНАРІЙ 2: Сигнал SHORT (опір)")
    print("-" * 80)
    direction_signal = "short"
    trade_direction = strategy.get_entry_direction(direction_signal)
    search_zone = strategy.get_pinbar_search_zone(100.0, direction_signal)
    
    print(f"📊 Сигнал з каналу: {direction_signal.upper()}")
    print(f"⚠️  ІНВЕРТОВАНИЙ напрямок угоди: {trade_direction.upper()}")
    print(f"\n🔍 Параметри пошуку:")
    for key, value in search_zone.items():
        print(f"   • {key}: {value}")
    
    print("\n📝 ЛОГІКА:")
    print(f"   1. Отримали сигнал SHORT (опір)")
    print(f"   2. НЕ чекаємо торкання рівня")
    print(f"   3. Чекаємо RSI ПЕРЕПРОДАНОСТІ (RSI <= rsi_low)")
    print(f"   4. Вхід на HIGH свічки")
    print(f"   5. Стоп на LOW свічки")
    print(f"   6. Відкриваємо LONG позицію ⚠️")
    print(f"\n   🔄 ВАЖЛИВО: Сигнал SHORT → Угода LONG!")


if __name__ == "__main__":
    test_premium2_strategy()
    test_gravity2_strategy()
    
    print("\n\n" + "=" * 80)
    print("✅ ВСІ ТЕСТИ ЗАВЕРШЕНО")
    print("=" * 80)
