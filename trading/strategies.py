from enum import Enum
from loguru import logger

class StrategyType(Enum):
    QUANTUM_PREMIUM2 = "quantum_premium2"
    QUANTUM_GRAVITY2 = "quantum_gravity2"

class BaseStrategy:
    
    def __init__(self, name: str, channel_id: int):
        self.name = name
        self.channel_id = channel_id
    
    def should_wait_for_level(self) -> bool:
        raise NotImplementedError
    
    def get_entry_direction(self, signal_direction: str) -> str:
        raise NotImplementedError
    
    def needs_reversal_signal(self) -> bool:
        raise NotImplementedError
    
    def requires_pullback(self) -> bool:
        raise NotImplementedError

class QuantumPremium2Strategy(BaseStrategy):
    """
    Стратегія 1: Торгівля від рівня з пінбаром
    
    Логіка:
    1. Після повідомлення "чекаємо на дотик рівня" - чекаємо торкання
    2. Від свічки, що торкнулась рівня, починаємо пошук пінбара
    3. Аналізуємо тільки N свічок (з налаштувань timeframe)
    4. Для LONG (підтримка): пінбар повинен оновити мінімум після торкання
    5. Для SHORT (опір): пінбар повинен оновити максимум після торкання
    6. Відкриваємо позицію в напрямку сигналу з каналу
    """
    def __init__(self, channel_id: int):
        super().__init__("Quantum Premium2", channel_id)
    
    def should_wait_for_level(self) -> bool:
        """Чекаємо торкання рівня"""
        return True
    
    def get_entry_direction(self, signal_direction: str) -> str:
        """Торгуємо в напрямку сигналу: LONG при підтримці, SHORT при опорі"""
        return signal_direction
    
    def needs_reversal_signal(self) -> bool:
        return False
    
    def requires_pullback(self) -> bool:
        """Не потрібен відкат, працюємо від рівня"""
        return False
    
    def get_pinbar_search_zone(self, level: float, direction: str) -> dict:
        """
        Параметри пошуку пінбара:
        - search_after_touch: починаємо пошук після торкання рівня
        - wait_for_extremum: чекаємо оновлення екстремуму
        - analyze_n_candles: аналізуємо N свічок після торкання
        """
        if direction == "long":
            # LONG (підтримка): пінбар оновлює мінімум, входимо на HIGH
            return {
                "search_after_touch": True,
                "wait_for_extremum": True,
                "extremum_type": "new_low",  # Оновлює мінімум
                "trade_direction": "long",
                "entry_on": "high",
                "stop_on": "low",
                "invalidate_on": "low",
                "analyze_n_candles": True  # Аналізуємо точну кількість з налаштувань
            }
        else:
            # SHORT (опір): пінбар оновлює максимум, входимо на LOW
            return {
                "search_after_touch": True,
                "wait_for_extremum": True,
                "extremum_type": "new_high",  # Оновлює максимум
                "trade_direction": "short",
                "entry_on": "low",
                "stop_on": "high",
                "invalidate_on": "high",
                "analyze_n_candles": True  # Аналізуємо точну кількість з налаштувань
            }

class QuantumGravity2Strategy(BaseStrategy):
    """
    Стратегія 2: Торгівля з відкатом та RSI
    
    Логіка:
    1. Сигнал ПІДТРИМКИ (LONG з каналу):
       - Чекаємо зону ПЕРЕКУПЛЕНОСТІ (RSI > rsi_high)
       - Відкриваємо SHORT на LOW свічки
    
    2. Сигнал ОПОРУ (SHORT з каналу):
       - Чекаємо зону ПЕРЕПРОДАНОСТІ (RSI < rsi_low)
       - Відкриваємо LONG на HIGH свічки
    
    ВАЖЛИВО: Напрямок угоди протилежний сигналу!
    """
    def __init__(self, channel_id: int):
        super().__init__("Quantum Gravity2", channel_id)
    
    def should_wait_for_level(self) -> bool:
        """НЕ чекаємо торкання рівня"""
        return False
    
    def get_entry_direction(self, signal_direction: str) -> str:
        """
        КРИТИЧНО: Інвертуємо напрямок!
        - Сигнал LONG (підтримка) → відкриваємо SHORT (на перекупленості)
        - Сигнал SHORT (опір) → відкриваємо LONG (на перепроданості)
        """
        return "short" if signal_direction == "long" else "long"
    
    def needs_reversal_signal(self) -> bool:
        """Потрібен розворотний сигнал через RSI"""
        return True
    
    def requires_pullback(self) -> bool:
        """Потрібен відкат до екстремальної зони RSI"""
        return True
    
    def get_pinbar_search_zone(self, level: float, direction: str) -> dict:
        """
        Параметри для пошуку точки входу на основі RSI:
        
        direction - це напрямок СИГНАЛУ з каналу:
        - "long" (підтримка) → чекаємо RSI > rsi_high → відкриваємо SHORT на LOW
        - "short" (опір) → чекаємо RSI < rsi_low → відкриваємо LONG на HIGH
        """
        if direction == "long":
            # Сигнал LONG (підтримка) → відкриваємо SHORT
            return {
                "search_after_consolidation": True,
                "wait_for_rsi": True,
                "rsi_condition": "overbought",  # Чекаємо перекупленості
                "rsi_threshold": "high",  # RSI > rsi_high
                "trade_direction": "short",  # Відкриваємо SHORT
                "entry_on": "low",  # Вхід на дні свічки
                "stop_on": "high",
                "invalidate_on": "high"
            }
        else:
            # Сигнал SHORT (опір) → відкриваємо LONG
            return {
                "search_after_consolidation": True,
                "wait_for_rsi": True,
                "rsi_condition": "oversold",  # Чекаємо перепроданості
                "rsi_threshold": "low",  # RSI < rsi_low
                "trade_direction": "long",  # Відкриваємо LONG
                "entry_on": "high",  # Вхід на верху свічки
                "stop_on": "low",
                "invalidate_on": "low"
            }

def get_strategy_for_channel(channel_id: int) -> BaseStrategy:
    """
    Отримати стратегію для каналу
    
    -1002990245762 → Quantum Premium2 (торгівля від рівня) - Стратегія 1
    -1003193138774 → Quantum Gravity2 (торгівля на пробій) - Стратегія 2
    """
    CHANNEL_STRATEGIES = {
        -1002990245762: QuantumPremium2Strategy,   # Strategy 1: trade FROM level
        -1003193138774: QuantumGravity2Strategy,   # Strategy 2: trade THROUGH level
    }
    
    strategy_class = CHANNEL_STRATEGIES.get(channel_id)
    if strategy_class:
        strategy = strategy_class(channel_id)
        logger.info(f"Обрано стратегію: {strategy.name} для каналу {channel_id}")
        return strategy

    logger.warning(f"Невідомий канал {channel_id}, використовуємо Quantum Premium2 за замовчуванням")
    return QuantumPremium2Strategy(channel_id)
