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

    def use_trigger_logic(self, trade_direction: str) -> bool:
        """
        Określa czy strategia używa logiki trigger dla danego kierunku.
        Trigger = obserwacja ceny i opóźnione otwarcie zlecenia.
        """
        return False


class QuantumPremium2Strategy(BaseStrategy):
    def __init__(self, channel_id: int):
        super().__init__("Quantum Premium2", channel_id)

    def should_wait_for_level(self) -> bool:
        """Чекаємо торкання рівня"""
        return True

    def get_entry_direction(self, signal_direction: str) -> str:
        """Торгуємо в напрямку сигналу: LONG при підтримці, SHORT при опорі"""
        return signal_direction

    def needs_reversal_signal(self) -> bool:
        """✅ НОВИНКА: Потрібна перевірка RSI або дивергенції"""
        return True

    def requires_pullback(self) -> bool:
        """Не потрібен відкат, працюємо від рівня"""
        return False

    def get_pinbar_search_zone(self, level: float, direction: str) -> dict:
        """
        Зона пошуку пінбара після торкання рівня

        direction - напрямок сигналу:
        - "long" (підтримка) → пінбар має оновити LOW → вхід на HIGH
        - "short" (опір) → пінбар має оновити HIGH → вхід на LOW
        """
        if direction == "long":

            return {
                "search_after_touch": True,
                "wait_for_extremum": True,
                "extremum_type": "new_low",
                "analyze_n_candles": True,
                "trade_direction": "long",
                "entry_on": "high",
                "stop_on": "low",
                "invalidate_on": "low",
                "check_rsi_or_divergence": True,
                "rsi_condition": "oversold",
                "rsi_threshold": "low",
                "allow_divergence": True,
            }
        else:

            return {
                "search_after_touch": True,
                "wait_for_extremum": True,
                "extremum_type": "new_high",
                "analyze_n_candles": True,
                "trade_direction": "short",
                "entry_on": "low",
                "stop_on": "high",
                "invalidate_on": "high",
                "check_rsi_or_divergence": True,
                "rsi_condition": "overbought",
                "rsi_threshold": "high",
                "allow_divergence": True,
            }


class QuantumGravity2Strategy(BaseStrategy):
    def __init__(self, channel_id: int):
        super().__init__("Quantum Gravity2", channel_id)

    def should_wait_for_level(self) -> bool:
        """Gravity2: Чекаємо торкання рівня перед початком пошуку сигналу"""
        return True

    def get_entry_direction(self, signal_direction: str) -> str:
        """
        ✅ ІНВЕРСІЯ ДЛЯ GRAVITY2!
        Підтримка (long signal) → SHORT позиція (чекаємо перекупленості)
        Опір (short signal) → LONG позиція (чекаємо перепроданості)
        """
        return "short" if signal_direction == "long" else "long"

    def needs_reversal_signal(self) -> bool:
        """Потрібен розворотний сигнал через RSI"""
        return True

    def requires_pullback(self) -> bool:
        """Потрібен відкат до екстремальної зони RSI"""
        return True

    def use_trigger_logic(self, trade_direction: str) -> bool:
        """
        ✅ NOWA LOGIKA: Trigger tylko dla SHORT
        Bot obserwuje cenę i otwiera zlecenie gdy:
        1. Cena osiągnie poziom trigger (low świecy)
        2. Po aktywacji czeka N sekund
        3. Sprawdza że cena pozostaje poza świecą
        4. Otwiera zlecenie rynkowe
        """
        return trade_direction == "short"

    def get_pinbar_search_zone(self, level: float, direction: str) -> dict:
        if direction == "long":

            return {
                "search_after_consolidation": True,
                "wait_for_rsi": True,
                "rsi_condition": "overbought",
                "rsi_threshold": "high",
                "trade_direction": "short",
                "entry_on": "low",
                "stop_on": "high",
                "invalidate_on": "high",
            }
        else:

            return {
                "search_after_consolidation": True,
                "wait_for_rsi": True,
                "rsi_condition": "oversold",
                "rsi_threshold": "low",
                "trade_direction": "long",
                "entry_on": "high",
                "stop_on": "low",
                "invalidate_on": "low",
            }


def get_strategy_for_channel(channel_id: int) -> BaseStrategy:
    from config import CONFIG

    CHANNEL_STRATEGIES = {
        CONFIG["CHANNEL_PREMIUM2_ID"]: QuantumPremium2Strategy,
        CONFIG["CHANNEL_GRAVITY2_ID"]: QuantumGravity2Strategy,
    }

    strategy_class = CHANNEL_STRATEGIES.get(channel_id)
    if strategy_class:
        strategy = strategy_class(channel_id)
        logger.info(f"Обрано стратегію: {strategy.name} для каналу {channel_id}")
        return strategy

    logger.warning(f"Невідомий канал {channel_id}, використовуємо Quantum Premium2 за замовчуванням")
    return QuantumPremium2Strategy(channel_id)
