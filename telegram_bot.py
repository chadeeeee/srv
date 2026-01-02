import asyncio
import sys

if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import CONFIG
from utils.logger import logger, setting_changed
from utils.settings_manager import get_settings, update_setting


class SettingsStates(StatesGroup):
    waiting_for_pinbar_tail = State()
    waiting_for_pinbar_body = State()
    waiting_for_pinbar_opposite = State()
    waiting_for_pinbar_timeout = State()
    waiting_for_rsi_high = State()
    waiting_for_rsi_low = State()
    waiting_for_max_retries = State()
    waiting_for_max_trades = State()
    waiting_for_max_drawdown = State()
    waiting_for_limit_candles = State()
    waiting_for_position_size = State()
    waiting_for_timeframe = State()
    waiting_for_stop_loss_offset = State()
    waiting_for_entry_offset = State()
    waiting_for_max_losses_in_row = State()
    waiting_for_pause_after_losses = State()
    waiting_for_max_equity_drawdown = State()

    waiting_for_limit_order_lifetime = State()
    waiting_for_pinbar_min_size = State()
    waiting_for_pinbar_max_size = State()
    waiting_for_pinbar_avg_candles = State()
    waiting_for_pinbar_min_avg_percent = State()
    waiting_for_pinbar_max_avg_percent = State()


def main_menu_keyboard():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пинбар", callback_data="menu_pinbar")],
            [InlineKeyboardButton(text="RSI", callback_data="menu_rsi")],
            [InlineKeyboardButton(text="Сделки", callback_data="menu_trades")],
            [InlineKeyboardButton(text="Безопасность", callback_data="menu_security")],
            [InlineKeyboardButton(text="Стратегия", callback_data="menu_strategy")],
            [InlineKeyboardButton(text="Размер позиции", callback_data="menu_position")],
            [InlineKeyboardButton(text="Другое", callback_data="menu_other")],
        ]
    )
    return keyboard


def _format_value(value, param_name=""):
    if value is None or value == "disabled":
        hint = ""
        if "tail" in param_name.lower() or "тени" in param_name.lower():
            hint = " (введите число 50-90 для включения)"
        elif "body" in param_name.lower() or "тела" in param_name.lower():
            hint = " (введите число 10-30 для включения)"
        elif "opposite" in param_name.lower() or "противоположной" in param_name.lower():
            hint = " (введите число 5-20 для включения)"
        elif "rsi" in param_name.lower():
            hint = " (введите число 20-80 для включения)"
        elif "retries" in param_name.lower() or "стопа" in param_name.lower():
            hint = " (введите число 1-5 для включения)"
        elif "trades" in param_name.lower() or "сделок" in param_name.lower():
            hint = " (введите число 1-10 для включения)"
        elif "drawdown" in param_name.lower() or "просадка" in param_name.lower():
            hint = " (введите число 5-50 для включения)"
        elif "candles" in param_name.lower() or "свечей" in param_name.lower():
            hint = " (введите число 1-10 для включения)"
        elif "position" in param_name.lower() or "позиции" in param_name.lower():
            hint = " (введите число 1-10 для включения)"
        elif "timeframe" in param_name.lower() or "таймфрейм" in param_name.lower():
            hint = " (введите 1/5/15/60/D для включения)"
        elif "stop_loss_offset" in param_name.lower() or "смещение" in param_name.lower():
            hint = " (стоп будет точно на экстремуме сигнальной свечи)"
        elif "entry_offset" in param_name.lower():
            hint = " (вход будет точно на HIGH/LOW свечи)"
        return f"Отключено{hint}"

    if "stop_loss_offset" in param_name.lower() or "entry_offset" in param_name.lower():
        return f"{float(value) * 100:.4f}%"

    return str(value)


def pinbar_menu_keyboard(settings):
    tail = settings.get("pinbar_tail_percent")
    body = settings.get("pinbar_body_percent")
    opposite = settings.get("pinbar_opposite_percent")
    min_size = settings.get("pinbar_min_size", 0.05)
    max_size = settings.get("pinbar_max_size", 2.0)
    avg_cnt = settings.get("pinbar_avg_candles", 10)
    min_avg_pct = settings.get("pinbar_min_avg_percent", 50)
    max_avg_pct = settings.get("pinbar_max_avg_percent", 200)

    text = (
        f"<b>Настройки Пинбара:</b>\n\n"
        f"Мин. % основной тени: <code>{_format_value(tail, 'tail')}</code>\n"
        f"Макс. % тела: <code>{_format_value(body, 'body')}</code>\n"
        f"Макс. % противоположной тени: <code>{_format_value(opposite, 'opposite')}</code>\n"
        f"Мин. размер (%): <code>{min_size}%</code>\n"
        f"Макс. размер (%): <code>{max_size}%</code>\n"
        f"Ср. свеча (к-сть): <code>{avg_cnt}</code>\n"
        f"Мин % от ср. размера: <code>{min_avg_pct}%</code>\n"
        f"Макс % от ср. размера: <code>{max_avg_pct}%</code>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Мин % основной тени", callback_data="pinbar_tail")],
            [InlineKeyboardButton(text="Макс % тела", callback_data="pinbar_body")],
            [InlineKeyboardButton(text="Макс % противоположной тени", callback_data="pinbar_opposite")],
            [InlineKeyboardButton(text="Мин размер (%)", callback_data="pinbar_min_size")],
            [InlineKeyboardButton(text="Макс размер (%)", callback_data="pinbar_max_size")],
            [InlineKeyboardButton(text="Ср. свеча (к-сть)", callback_data="pinbar_avg_candles")],
            [InlineKeyboardButton(text="Мин % от ср. размера", callback_data="pinbar_min_avg")],
            [InlineKeyboardButton(text="Макс % от ср. размера", callback_data="pinbar_max_avg")],
            [InlineKeyboardButton(text="Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


def rsi_menu_keyboard(settings):
    rsi_high = settings.get("rsi_high")
    rsi_low = settings.get("rsi_low")
    text = (
        f"<b>Настройки RSI:</b>\n\n"
        f"RSI High: <code>{_format_value(rsi_high, 'rsi_high')}</code>\n"
        f"RSI Low: <code>{_format_value(rsi_low, 'rsi_low')}</code>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="RSI High", callback_data="rsi_high")],
            [InlineKeyboardButton(text="RSI Low", callback_data="rsi_low")],
            [InlineKeyboardButton(text="Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


def trades_menu_keyboard(settings):
    max_retries = settings.get("max_retries")
    max_trades = settings.get("max_open_trades")
    text = (
        f"<b>Настройки Сделок:</b>\n\n"
        f"Сделки после стопа: <code>{_format_value(max_retries, 'retries')}</code>\n"
        f"Макс. кол-во откр. сделок: <code>{_format_value(max_trades, 'trades')}</code>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сделки после стопа", callback_data="trades_retries")],
            [InlineKeyboardButton(text="Макс. откр. сделок", callback_data="trades_max")],
            [InlineKeyboardButton(text="Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


def security_menu_keyboard(settings):
    from utils.settings_manager import is_bot_active, is_drawdown_protection_active

    max_drawdown = settings.get("max_drawdown_percent")
    limit_candles = settings.get("limit_max_candles")
    stop_loss_offset = settings.get("stop_loss_offset")
    entry_offset = settings.get("entry_offset_percent", 0.01)
    pinbar_timeout = settings.get("pinbar_timeout")
    settings.get("max_losses_in_row")
    pause_after_losses = settings.get("pause_after_losses")
    settings.get("max_equity_drawdown")
    limit_order_lifetime = settings.get("limit_order_lifetime", 5)
    bot_active = is_bot_active()
    dd_protection = is_drawdown_protection_active()
    bot_status = "Включены" if bot_active else "Отключены"
    " АКТИВНА" if dd_protection else "Выключена"

    text = (
        f"<b>Настройки Безопасности:</b>\n\n"
        f"Макс. просадка (%): <code>{_format_value(max_drawdown, 'drawdown')}</code>\n"
        f"Жизнь лимитки (мин): <code>{_format_value(limit_order_lifetime, 'limit_order_lifetime')}</code>\n"
        f"Смещение стоп-лосса: <code>{_format_value(stop_loss_offset, 'stop_loss_offset')}</code>\n"
        f"Таймаут ожидания пинбара (мин): <code>{_format_value(pinbar_timeout, 'pinbar_timeout')}</code>\n"
        f"Новые сделки: <code>{bot_status}</code>\n"
        f"<i>Напишите STOP для остановки бота или START для возобновления</i>\n\n"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Макс. просадка (%)", callback_data="security_drawdown")],
            [InlineKeyboardButton(text="Жизнь лимитки (мин)", callback_data="security_limit_order_lifetime")],
            [InlineKeyboardButton(text="Таймаут ожидания пинбара (мин)", callback_data="security_pinbar_timeout")],
            [InlineKeyboardButton(text="Смещение стоп-лосса", callback_data="security_stop_loss_offset")],
            [InlineKeyboardButton(text="Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


def position_menu_keyboard(settings):
    position_size = settings.get("position_size_percent")
    text = (
        f"<b>Размер позиции:</b>\n\n" f"Размер (% от баланса): <code>{_format_value(position_size, 'position')}</code>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить размер", callback_data="position_size")],
            [InlineKeyboardButton(text="Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


def other_menu_keyboard(settings):
    timeframe_raw = settings.get("timeframe", "1m")
    timeframe = _format_timeframe_display(timeframe_raw)

    text = f"<b>Другие настройки:</b>\n\n" f"Таймфрейм: <code>{timeframe}</code>"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Таймфрейм", callback_data="other_timeframe")],
            [InlineKeyboardButton(text="Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


def _format_timeframe_display(raw_value):
    if not raw_value:
        return "1m"
    return str(raw_value)


def _normalize_timeframe_for_db(user_input):
    if not user_input:
        return "1m"

    user_input = str(user_input).strip().lower()

    valid_formats = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w", "1M"}

    if user_input in valid_formats:
        return user_input

    if user_input.upper() in {"1M", "3M", "5M", "15M", "30M"}:
        return user_input
    if user_input.upper() in {"1H", "2H", "4H", "6H", "12H"}:
        return user_input
    if user_input.upper() in {"1D", "1W"}:
        return user_input

    return None


def input_keyboard(back_callback):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отключить", callback_data="disable_setting")],
            [InlineKeyboardButton(text="Назад", callback_data=back_callback)],
        ]
    )
    return keyboard


def strategy_menu_keyboard(settings):
    current_raw = settings.get("strategy") or ""
    enabled = set([s.lower() for s in (current_raw or "").replace(";", ",").replace("|", ",").split(",") if s.strip()])

    def label(name, key):
        return (" " if key in enabled else " ") + name

    text = (
        f"<b>Настройка Стратегии:</b>\n\n"
        f"Актуальные стратегии: <code>{_format_value(current_raw, 'strategy')}</code>\n\n"
        f"Нажмите для включения/отключения стратегии:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("Quantum Premium2", "quantum"), callback_data="strategy_quantum")],
            [InlineKeyboardButton(text=label("Quantum Gravity2", "gravity2"), callback_data="strategy_gravity2")],
            [InlineKeyboardButton(text="Назад", callback_data="back_main")],
        ]
    )
    return text, keyboard


bot = None
dp = None


def init_bot():
    global bot, dp
    bot_token = CONFIG.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not found in config!")
        return None, None
    try:
        bot = Bot(token=bot_token)
        storage = MemoryStorage()
        dp = Dispatcher(bot=bot, storage=storage)
    except Exception as e:
        logger.error(f"Failed to initialize aiogram bot/dispatcher: {e}")
        bot = None
        dp = None
        return None, None
    try:

        dp.message.register(cmd_stop, lambda msg: msg.text and msg.text.strip().upper() == "STOP")
        dp.message.register(cmd_start_trading, lambda msg: msg.text and msg.text.strip().upper() == "START")

        dp.message.register(cmd_start, Command("start"))
        dp.message.register(cmd_settings, Command("settings"))
        dp.callback_query.register(process_main_menu, F.data.startswith("menu_"))
        dp.callback_query.register(process_strategy_setting, F.data.startswith("menu_strategy"))
        dp.callback_query.register(process_strategy_setting, F.data.startswith("strategy_"))
        dp.callback_query.register(process_back_main, F.data == "back_main")
        dp.callback_query.register(process_back_pinbar, F.data == "back_pinbar")
        dp.callback_query.register(process_back_rsi, F.data == "back_rsi")
        dp.callback_query.register(process_back_trades, F.data == "back_trades")
        dp.callback_query.register(process_back_security, F.data == "back_security")
        dp.callback_query.register(process_back_other, F.data == "back_other")
        dp.callback_query.register(process_pinbar_setting, F.data.startswith("pinbar_"))
        dp.callback_query.register(process_rsi_setting, F.data.startswith("rsi_"))
        dp.callback_query.register(process_trades_setting, F.data.startswith("trades_"))
        dp.callback_query.register(process_security_setting, F.data.startswith("security_"))
        dp.callback_query.register(process_position_setting, F.data == "position_size")
        dp.callback_query.register(process_other_setting, F.data.startswith("other_"))
        dp.callback_query.register(process_disable_setting, F.data == "disable_setting")
        dp.message.register(process_pinbar_tail_input, SettingsStates.waiting_for_pinbar_tail)
        dp.message.register(process_pinbar_body_input, SettingsStates.waiting_for_pinbar_body)
        dp.message.register(process_pinbar_opposite_input, SettingsStates.waiting_for_pinbar_opposite)
        dp.message.register(process_pinbar_min_size_input, SettingsStates.waiting_for_pinbar_min_size)
        dp.message.register(process_pinbar_max_size_input, SettingsStates.waiting_for_pinbar_max_size)
        dp.message.register(process_pinbar_avg_candles_input, SettingsStates.waiting_for_pinbar_avg_candles)
        dp.message.register(process_pinbar_min_avg_percent_input, SettingsStates.waiting_for_pinbar_min_avg_percent)
        dp.message.register(process_pinbar_max_avg_percent_input, SettingsStates.waiting_for_pinbar_max_avg_percent)
        dp.message.register(process_pinbar_timeout_input, SettingsStates.waiting_for_pinbar_timeout)
        dp.message.register(process_rsi_high_input, SettingsStates.waiting_for_rsi_high)
        dp.message.register(process_rsi_low_input, SettingsStates.waiting_for_rsi_low)
        dp.message.register(process_max_retries_input, SettingsStates.waiting_for_max_retries)
        dp.message.register(process_max_trades_input, SettingsStates.waiting_for_max_trades)
        dp.message.register(process_max_drawdown_input, SettingsStates.waiting_for_max_drawdown)
        dp.message.register(process_limit_candles_input, SettingsStates.waiting_for_limit_candles)
        dp.message.register(process_position_size_input, SettingsStates.waiting_for_position_size)
        dp.message.register(process_timeframe_input, SettingsStates.waiting_for_timeframe)
        dp.message.register(process_stop_loss_offset_input, SettingsStates.waiting_for_stop_loss_offset)
        dp.message.register(process_entry_offset_input, SettingsStates.waiting_for_entry_offset)
        dp.message.register(process_max_losses_in_row_input, SettingsStates.waiting_for_max_losses_in_row)
        dp.message.register(process_pause_after_losses_input, SettingsStates.waiting_for_pause_after_losses)
        dp.message.register(process_max_equity_drawdown_input, SettingsStates.waiting_for_max_equity_drawdown)
        dp.message.register(process_limit_order_lifetime_input, SettingsStates.waiting_for_limit_order_lifetime)

        # Додаємо команду /status
        dp.message.register(cmd_status, Command("status"))
        dp.callback_query.register(process_status_pair, F.data.startswith("status_pair_"))
        dp.callback_query.register(process_status_refresh, F.data.startswith("status_refresh_"))
        dp.callback_query.register(process_status_back, F.data == "status_back")
        dp.callback_query.register(process_status_stop, F.data.startswith("status_stop_"))
    except Exception as e:
        logger.error(f"Error registering aiogram handlers: {e}")
    return bot, dp


# ========== /status command ==========

_status_update_tasks = {}  # Для зберігання задач автооновлення


async def cmd_status(message: Message):
    """Показує список активних моніторингів з inline кнопками"""
    from analysis.trigger_strategy import get_active_monitors_info
    
    monitors = get_active_monitors_info()
    
    if not monitors:
        await message.answer(
            "📊 <b>Статус моніторингу</b>\n\n"
            "❌ Немає активних моніторингів",
            parse_mode="HTML"
        )
        return
    
    text = f"📊 <b>Активні моніторинги:</b> ({len(monitors)})\n\n"
    text += "Натисніть на пару для детальної інформації:"
    
    keyboard = []
    for pair in monitors.keys():
        keyboard.append([InlineKeyboardButton(text=f"📈 {pair}", callback_data=f"status_pair_{pair}")])
    
    keyboard.append([InlineKeyboardButton(text="🔄 Оновити список", callback_data="status_refresh_list")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


async def get_pair_status_text(pair: str) -> str:
    """Формує текст статусу для конкретної пари"""
    from analysis.trigger_strategy import get_active_monitors_info, _monitor_states, BotState
    from analysis.signals import get_rsi
    from trading.signal_handler import SignalHandler
    import time
    
    monitors = get_active_monitors_info()
    
    if pair not in monitors:
        return f"❌ Моніторинг {pair} не активний"
    
    data = monitors[pair]
    
    # Отримуємо поточну ціну
    signal_handler = SignalHandler()
    try:
        current_price = await signal_handler.get_real_time_price(pair)
    except:
        current_price = None
    finally:
        await signal_handler.close()
    
    # Отримуємо RSI
    try:
        rsi_value, _ = await get_rsi(pair, "1", 14)
    except:
        rsi_value = None
    
    # Формуємо текст
    state = data.get("state", "unknown")
    target = data.get("target")
    direction = data.get("direction", "?")
    trade_direction = data.get("trade_direction", "?")
    strategy = data.get("strategy", "?")
    level_touched = data.get("level_touched", False)
    start_time = data.get("start_time")
    
    # Емодзі для напрямку
    dir_emoji = "🟢" if trade_direction == "long" else "🔴"
    
    # Розрахунок часу моніторингу
    elapsed = ""
    if start_time:
        mins = int((time.time() - start_time) / 60)
        elapsed = f"{mins} хв"
    
    # Відстань до рівня
    distance = ""
    if current_price and target:
        diff = ((current_price - target) / target) * 100
        distance = f"{diff:+.2f}%"
    
    # RSI статус
    rsi_status = ""
    if rsi_value:
        if rsi_value >= 70:
            rsi_status = "🔴 Перекупленість"
        elif rsi_value <= 30:
            rsi_status = "🟢 Перепроданість"
        else:
            rsi_status = "⚪ Нейтрально"
    
    # Стан mapping
    state_map = {
        "wait_level_touch": "⏳ Чекаю торкання рівня",
        "wait_rsi": "📊 Чекаю RSI екстремум",
        "wait_pinbar": "🔍 Шукаю пінбар",
        "wait_pinbar_gravity": "🔍 Шукаю пінбар (Gravity)",
        "signal_confirmed": "✅ Сигнал підтверджено",
        "trigger_placed": "📌 Тригер розміщено",
        "trigger_filled": "💰 Позиція відкрита",
        "trigger_expired": "⏱️ Тригер протерміновано",
    }
    state_text = state_map.get(state, state)
    
    # Форматуємо значення перед використанням в f-string
    price_str = f"{current_price:.8f}" if current_price else "N/A"
    rsi_str = f"{rsi_value:.1f}" if rsi_value else "N/A"
    target_str = f"{target:.8f}" if target else "N/A"
    
    text = (
        f"📊 <b>{pair}</b>\n"
        f"{'─' * 25}\n\n"
        f"🎯 <b>Стратегія:</b> {strategy}\n"
        f"{dir_emoji} <b>Напрямок:</b> {trade_direction.upper()}\n"
        f"📍 <b>Рівень:</b> <code>{target_str}</code>\n\n"
        f"💰 <b>Поточна ціна:</b> <code>{price_str}</code>\n"
        f"📏 <b>Відстань:</b> {distance}\n\n"
        f"📈 <b>RSI:</b> <code>{rsi_str}</code> {rsi_status}\n\n"
        f"🔔 <b>Рівень торкнуто:</b> {'✅ Так' if level_touched else '❌ Ні'}\n"
        f"⏱️ <b>Час моніторингу:</b> {elapsed}\n\n"
        f"📋 <b>Стан:</b> {state_text}\n\n"
        f"<i>🔄 Автооновлення кожні 5 сек</i>"
    )
    
    return text


async def process_status_pair(callback: CallbackQuery):
    """Обробник натискання на пару - показує детальну інформацію"""
    pair = callback.data.replace("status_pair_", "")
    
    text = await get_pair_status_text(pair)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Оновити", callback_data=f"status_refresh_{pair}")],
            [InlineKeyboardButton(text="🛑 Зупинити моніторинг", callback_data=f"status_stop_{pair}")],
            [InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="status_back")],
        ]
    )
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()
    
    # Запускаємо автооновлення
    chat_id = callback.message.chat.id
    message_id = callback.message.message_id
    task_key = f"{chat_id}_{message_id}"
    
    # Скасовуємо попередню задачу якщо є
    if task_key in _status_update_tasks:
        _status_update_tasks[task_key].cancel()
    
    # Запускаємо нову задачу автооновлення
    task = asyncio.create_task(auto_refresh_status(pair, chat_id, message_id))
    _status_update_tasks[task_key] = task


async def auto_refresh_status(pair: str, chat_id: int, message_id: int):
    """Автоматично оновлює статус кожні 5 секунд"""
    global bot
    
    try:
        while True:
            await asyncio.sleep(5)
            
            from analysis.trigger_strategy import get_active_monitors_info
            monitors = get_active_monitors_info()
            
            # Якщо моніторинг завершився - зупиняємо оновлення
            if pair not in monitors:
                text = f"✅ Моніторинг {pair} завершено"
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="status_back")],
                    ]
                )
                try:
                    await bot.edit_message_text(
                        text=text,
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                except:
                    pass
                break
            
            text = await get_pair_status_text(pair)
            
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Оновити", callback_data=f"status_refresh_{pair}")],
                    [InlineKeyboardButton(text="🛑 Зупинити моніторинг", callback_data=f"status_stop_{pair}")],
                    [InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="status_back")],
                ]
            )
            
            try:
                await bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
            except Exception as e:
                # Повідомлення могло бути видалено або не змінилось
                if "message is not modified" not in str(e).lower():
                    break
                    
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Auto refresh error: {e}")


async def process_status_refresh(callback: CallbackQuery):
    """Обробник кнопки оновлення"""
    data = callback.data.replace("status_refresh_", "")
    
    if data == "list":
        # Оновлення списку
        from analysis.trigger_strategy import get_active_monitors_info
        monitors = get_active_monitors_info()
        
        if not monitors:
            text = "📊 <b>Статус моніторингу</b>\n\n❌ Немає активних моніторингів"
            await callback.message.edit_text(text, parse_mode="HTML")
            await callback.answer("Список оновлено")
            return
        
        text = f"📊 <b>Активні моніторинги:</b> ({len(monitors)})\n\nНатисніть на пару для детальної інформації:"
        
        keyboard = []
        for pair in monitors.keys():
            keyboard.append([InlineKeyboardButton(text=f"📈 {pair}", callback_data=f"status_pair_{pair}")])
        
        keyboard.append([InlineKeyboardButton(text="🔄 Оновити список", callback_data="status_refresh_list")])
        
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer("Список оновлено")
    else:
        # Оновлення конкретної пари
        pair = data
        text = await get_pair_status_text(pair)
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Оновити", callback_data=f"status_refresh_{pair}")],
                [InlineKeyboardButton(text="🛑 Зупинити моніторинг", callback_data=f"status_stop_{pair}")],
                [InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="status_back")],
            ]
        )
        
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer("Оновлено")


async def process_status_back(callback: CallbackQuery):
    """Повернення до списку моніторингів"""
    # Скасовуємо автооновлення
    chat_id = callback.message.chat.id
    message_id = callback.message.message_id
    task_key = f"{chat_id}_{message_id}"
    
    if task_key in _status_update_tasks:
        _status_update_tasks[task_key].cancel()
        del _status_update_tasks[task_key]
    
    from analysis.trigger_strategy import get_active_monitors_info
    monitors = get_active_monitors_info()
    
    if not monitors:
        text = "📊 <b>Статус моніторингу</b>\n\n❌ Немає активних моніторингів"
        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer()
        return
    
    text = f"📊 <b>Активні моніторинги:</b> ({len(monitors)})\n\nНатисніть на пару для детальної інформації:"
    
    keyboard = []
    for pair in monitors.keys():
        keyboard.append([InlineKeyboardButton(text=f"📈 {pair}", callback_data=f"status_pair_{pair}")])
    
    keyboard.append([InlineKeyboardButton(text="🔄 Оновити список", callback_data="status_refresh_list")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


async def process_status_stop(callback: CallbackQuery):
    """Зупинка моніторингу для пари"""
    pair = callback.data.replace("status_stop_", "")
    
    from analysis.trigger_strategy import stop_monitoring
    
    result = stop_monitoring(pair)
    
    if result:
        await callback.answer(f"✅ Моніторинг {pair} зупинено")
        
        # Скасовуємо автооновлення
        chat_id = callback.message.chat.id
        message_id = callback.message.message_id
        task_key = f"{chat_id}_{message_id}"
        
        if task_key in _status_update_tasks:
            _status_update_tasks[task_key].cancel()
            del _status_update_tasks[task_key]
        
        # Повертаємось до списку
        await process_status_back(callback)
    else:
        await callback.answer(f"❌ Не вдалося зупинити {pair}")


async def cmd_start(message: Message):
    await message.answer("/settings")


async def cmd_settings(message: Message):
    keyboard = main_menu_keyboard()
    await message.answer(" <b>Главное меню настроек:</b>", reply_markup=keyboard, parse_mode="HTML")


async def cmd_stop(message: Message):
    """Зупинка бота - блокування нових угод"""
    from utils.settings_manager import set_bot_active

    set_bot_active(False)
    logger.info("🛑 Бот остановлен через команду STOP")
    await message.answer(
        "🛑 <b>Бот остановлен. Новые сделки заблокированы</b>\n" "Напишите START чтобы возобновить", parse_mode="HTML"
    )


async def cmd_start_trading(message: Message):
    """Відновлення роботи бота"""
    from utils.settings_manager import set_bot_active

    set_bot_active(True)
    logger.info("✅ Бот запущен через команду START")
    await message.answer(
        "✅ <b>Бот запущен. Новые сделки разрешены</b>\n" "Напишите STOP чтобы приостановить", parse_mode="HTML"
    )


async def process_main_menu(callback: CallbackQuery):
    settings = get_settings()
    if callback.data == "menu_pinbar":
        text, keyboard = pinbar_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif callback.data == "menu_rsi":
        text, keyboard = rsi_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif callback.data == "menu_trades":
        text, keyboard = trades_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif callback.data == "menu_security":
        text, keyboard = security_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif callback.data == "menu_position":
        text, keyboard = position_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif callback.data == "menu_other":
        text, keyboard = other_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    elif callback.data == "menu_strategy":
        text, keyboard = strategy_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def process_back_main(callback: CallbackQuery):
    keyboard = main_menu_keyboard()
    await callback.message.edit_text(" <b>Главное меню настроек:</b>", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def process_back_pinbar(callback: CallbackQuery):
    settings = get_settings()
    text, keyboard = pinbar_menu_keyboard(settings)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def process_back_rsi(callback: CallbackQuery):
    settings = get_settings()
    text, keyboard = rsi_menu_keyboard(settings)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def process_back_trades(callback: CallbackQuery):
    settings = get_settings()
    text, keyboard = trades_menu_keyboard(settings)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def process_back_security(callback: CallbackQuery):
    settings = get_settings()
    text, keyboard = security_menu_keyboard(settings)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def process_back_other(callback: CallbackQuery):
    settings = get_settings()
    text, keyboard = other_menu_keyboard(settings)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


async def process_pinbar_setting(callback: CallbackQuery, state: FSMContext):
    if callback.data == "pinbar_tail":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text(
            "Введите минимальный % основной тени пинбара (например, 70):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_tail)
    elif callback.data == "pinbar_body":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text("Введите максимальный % тела пинбара (например, 20):", reply_markup=keyboard)
        await state.set_state(SettingsStates.waiting_for_pinbar_body)
    elif callback.data == "pinbar_opposite":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text(
            "Введите максимальный % противоположной тени (например, 15):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_opposite)
    elif callback.data == "pinbar_min_size":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text(
            "Введите минимальный размер свечи в % (например, 0.05):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_min_size)
    elif callback.data == "pinbar_max_size":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text(
            "Введите максимальный размер свечи в % (например, 2.0):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_max_size)
    elif callback.data == "pinbar_avg_candles":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text(
            "Введите количество свечей для расчета средней (например, 10):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_avg_candles)
    elif callback.data == "pinbar_min_avg":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text(
            "Введите минимальный процент от средней свечи (например, 50):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_min_avg_percent)
    elif callback.data == "pinbar_max_avg":
        keyboard = input_keyboard("back_pinbar")
        await callback.message.edit_text(
            "Введите максимальный процент от средней свечи (например, 200):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_max_avg_percent)
    elif callback.data == "pinbar_timeout":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            "Введите таймаут ожидания пинбара в минутах (например, 10):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_timeout)
    await callback.answer()


async def process_pinbar_tail_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("pinbar_tail_percent", value)
        await message.answer(f" Минимальный % основной тени установлен: {value}")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_pinbar_body_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("pinbar_body_percent", value)
        await message.answer(f" Максимальный % тела установлен: {value}")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_pinbar_opposite_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("pinbar_opposite_percent", value)
        await message.answer(f" Максимальный % противоположной тени установлен: {value}")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_pinbar_min_size_input(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        update_setting("pinbar_min_size", value)
        await message.answer(f" Минимальный размер свечи установлен: {value}%")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число (например 0.05).")


async def process_pinbar_max_size_input(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        update_setting("pinbar_max_size", value)
        await message.answer(f" Максимальный размер свечи установлен: {value}%")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число (например 2.0).")


async def process_pinbar_avg_candles_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("pinbar_avg_candles", value)
        await message.answer(f" Количество свечей для средней установлено: {value}")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите целое число.")


async def process_pinbar_min_avg_percent_input(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        update_setting("pinbar_min_avg_percent", value)
        await message.answer(f" Минимальный % от средней установлен: {value}%")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_pinbar_max_avg_percent_input(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        update_setting("pinbar_max_avg_percent", value)
        await message.answer(f" Максимальный % от средней установлен: {value}%")
        settings = get_settings()
        text, keyboard = pinbar_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_pinbar_timeout_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        if value < 0:
            await message.answer(" Значение должно быть целым неотрицательным числом (минуты).")
            return
        update_setting("pinbar_timeout", value)
        try:
            setting_changed("pinbar_timeout", value)
        except Exception:
            pass
        await message.answer(f" Таймаут ожидания пинбара установлен: {value} минут")
        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число минут (например, 10).")


async def process_rsi_setting(callback: CallbackQuery, state: FSMContext):
    if callback.data == "rsi_high":
        keyboard = input_keyboard("back_rsi")
        await callback.message.edit_text("Введите значение RSI High (например, 70):", reply_markup=keyboard)
        await state.set_state(SettingsStates.waiting_for_rsi_high)
    elif callback.data == "rsi_low":
        keyboard = input_keyboard("back_rsi")
        await callback.message.edit_text("Введите значение RSI Low (например, 30):", reply_markup=keyboard)
        await state.set_state(SettingsStates.waiting_for_rsi_low)
    await callback.answer()


async def process_rsi_high_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        if value < 50 or value > 100:
            await message.answer(" RSI High має бути від 50 до 100")
            return
        update_setting("rsi_high", value)
        await message.answer(f" RSI High встановлено: {value}")
        settings = get_settings()
        text, keyboard = rsi_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Невірне значення. Введіть число від 50 до 100")


async def process_rsi_low_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        if value < 0 or value > 50:
            await message.answer(" RSI Low має бути від 0 до 50")
            return
        update_setting("rsi_low", value)
        await message.answer(f" RSI Low встановлено: {value}")
        settings = get_settings()
        text, keyboard = rsi_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Невірне значення. Введіть число від 0 до 50")


async def process_trades_setting(callback: CallbackQuery, state: FSMContext):
    if callback.data == "trades_retries":
        keyboard = input_keyboard("back_trades")
        await callback.message.edit_text("Введите количество сделок после стопа (например, 2):", reply_markup=keyboard)
        await state.set_state(SettingsStates.waiting_for_max_retries)
    elif callback.data == "trades_max":
        keyboard = input_keyboard("back_trades")
        await callback.message.edit_text(
            "Введите максимальное количество открытых сделок (например, 3):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_max_trades)
    await callback.answer()


async def process_max_retries_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        if value < 0 or value > 10:
            await message.answer(" Значение должно быть от 0 до 10")
            return
        update_setting("max_retries", value)
        if value == 0:
            await message.answer(f" Повторные входы ОТКЛЮЧЕНЫ (установлено: {value})")
        else:
            await message.answer(f" Количество повторных входов после стоп-лосса установлено: {value}")
        settings = get_settings()
        text, keyboard = trades_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число от 0 до 10.")


async def process_max_trades_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("max_open_trades", value)
        await message.answer(f" Макс. открытых сделок установлено: {value}")
        settings = get_settings()
        text, keyboard = trades_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_security_setting(callback: CallbackQuery, state: FSMContext):
    if callback.data == "security_drawdown":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text("Введите максимальную просадку в % (например, 20):", reply_markup=keyboard)
        await state.set_state(SettingsStates.waiting_for_max_drawdown)
    elif callback.data == "security_candles":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text("Введите лимит свечей до отмены лимитки (например, 3):", reply_markup=keyboard)
        await state.set_state(SettingsStates.waiting_for_limit_candles)
    elif callback.data == "security_limit_order_lifetime":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            "Введите время жизни лимитного ордера в минутах (например, 5):\n\n"
            "Если лимитка не исполнилась за это время, она будет автоматически отменена.",
            reply_markup=keyboard,
        )
        await state.set_state(SettingsStates.waiting_for_limit_order_lifetime)
    elif callback.data == "security_pinbar_timeout":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            "Введите таймаут ожидания пинбара в минутах (например, 10):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pinbar_timeout)
    elif callback.data == "security_stop_loss_offset":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            "Введите смещение стоп-лосса (например, 0.007 для 0.7%):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_stop_loss_offset)
    elif callback.data == "security_entry_offset":
        settings = get_settings()
        current = settings.get("entry_offset_percent", 0.01)
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            f"📊 Текущее смещение входа: {current}%\n\n"
            f"Введите новое значение процента смещения входа (0-1):\n\n"
            f"🔹 0 = вход точно на HIGH/LOW\n"
            f"🔹 0.01 = 0.01% за экстремум (рекомендовано)\n"
            f"🔹 0.05 = 0.05% за экстремум\n"
            f"🔹 0.1 = 0.1% за экстремум\n\n"
            f"❗️ Для LONG: вход ВЫШЕ HIGH на этот %\n"
            f"❗️ Для SHORT: вход НИЖЕ LOW на этот %",
            reply_markup=keyboard,
        )
        await state.set_state(SettingsStates.waiting_for_entry_offset)
    elif callback.data == "security_max_losses_in_row":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            "Введите максимальное число подрядных убытков (например, 3):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_max_losses_in_row)
    elif callback.data == "security_pause_after_losses":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            "Введите длительность паузы после убытков (минуты, например, 10):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_pause_after_losses)
    elif callback.data == "security_max_equity_drawdown":
        keyboard = input_keyboard("back_security")
        await callback.message.edit_text(
            "Введите максимальную просадку по equity (например, 100 для $100):", reply_markup=keyboard
        )
        await state.set_state(SettingsStates.waiting_for_max_equity_drawdown)
    await callback.answer()


async def process_max_drawdown_input(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        update_setting("max_drawdown_percent", value)
        await message.answer(f" Максимальная просадка установлена: {value}%")
        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_limit_candles_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("limit_max_candles", value)
        await message.answer(f" Лимит свечей установлен: {value}")
        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_stop_loss_offset_input(message: Message, state: FSMContext):
    try:
        value_text = message.text.strip().lower()

        if value_text in ("off", "disabled", "выкл", "вимкнено"):
            update_setting("stop_loss_offset", 0)
            await message.answer(
                f" Смещение стоп-лосса ОТКЛЮЧЕНО\n" f"Стоп будет ставиться точно на экстремуме сигнальной свечи"
            )
        else:
            value = float(value_text)
            if value < 0:
                await message.answer(" Значение не может быть отрицательным")
                return

            update_setting("stop_loss_offset", value)

            percentage = value * 100
            await message.answer(
                f" Смещение стоп-лосса установлено: {percentage:.4f}%\n\n"
                f"Примеры:\n"
                f"• 0.0001 = 0.01% (1 пункт на $10000)\n"
                f"• 0.001 = 0.1% (10 пунктов на $10000)\n"
                f"• 0.01 = 1% (100 пунктов на $10000)"
            )

        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число (например, 0.0001 для 0.01%) или 'off' для отключения.")


async def process_entry_offset_input(message: Message, state: FSMContext):
    """Обработка введенного значения смещения входа"""
    try:
        value = float(message.text)
        if value < 0 or value > 1:
            await message.answer("⚠️ Значение должно быть от 0 до 1")
            return

        update_setting("entry_offset_percent", value)
        await message.answer(f"✅ Смещение входа обновлено: {value}%")

        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверное значение. Введите число (например, 0.01)")


async def process_max_losses_in_row_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("max_losses_in_row", value)
        await message.answer(f" Максимальное число подрядных убытков установлено: {value}")
        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите целое число.")


async def process_pause_after_losses_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        update_setting("pause_after_losses", value)
        await message.answer(f" Пауза после убытков установлена: {value} минут")
        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите целое число.")


async def process_max_equity_drawdown_input(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        update_setting("max_equity_drawdown", value)
        await message.answer(f" Максимальная просадка по equity установлена: {value}")
        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_limit_order_lifetime_input(message: Message, state: FSMContext):
    try:
        value = int(message.text)
        if value < 1:
            await message.answer("Значение должно быть больше 0")
            return
        update_setting("limit_order_lifetime", value)
        await message.answer(
            f"Время жизни лимитного ордера установлено: {value} минут\n\n"
            f"Лимитки будут автоматически отменяться через {value} минут, если не исполнятся."
        )
        settings = get_settings()
        text, keyboard = security_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer("Неверное значение. Введите целое число минут (например, 5).")


async def process_position_setting(callback: CallbackQuery, state: FSMContext):
    keyboard = input_keyboard("back_main")
    await callback.message.edit_text("Введите размер позиции в % от баланса (например, 3.0):", reply_markup=keyboard)
    await state.set_state(SettingsStates.waiting_for_position_size)
    await callback.answer()


async def process_position_size_input(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        update_setting("position_size_percent", value)
        await message.answer(f" Размер позиции установлен: {value}%")
        settings = get_settings()
        text, keyboard = position_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer(" Неверное значение. Введите число.")


async def process_other_setting(callback: CallbackQuery, state: FSMContext):
    if callback.data == "other_timeframe":
        keyboard = input_keyboard("back_other")
        await callback.message.edit_text(
            "Введите таймфрейм:\n\n"
            "<b>Допустимые значения:</b>\n"
            "• Минуты: 1m, 3m, 5m, 15m, 30m\n"
            "• Часы: 1h, 2h, 4h, 6h, 12h\n"
            "• Дни и более: 1d, 1w, 1M\n\n"
            "<b>Примеры:</b> 5m, 1h, 1d\n\n"
            "<i>Лимит свечей до отмены лимитки будет считаться на этом таймфрейме</i>",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        await state.set_state(SettingsStates.waiting_for_timeframe)
    await callback.answer()


async def process_timeframe_input(message: Message, state: FSMContext):
    try:
        user_input = message.text.strip()
        normalized = _normalize_timeframe_for_db(user_input)

        if normalized is None:
            await message.answer(
                " Невірний формат таймфрейму!\n\n"
                "<b>Допустимі значення:</b>\n"
                "• Хвилини: 1m, 3m, 5m, 15m, 30m\n"
                "• Години: 1h, 2h, 4h, 6h, 12h\n"
                "• Дні та більше: 1d, 1w, 1M\n\n"
                "Спробуйте ще раз:",
                parse_mode="HTML",
            )
            return

        update_setting("timeframe", normalized)
        await message.answer(f" Таймфрейм встановлено: {normalized}")

        settings = get_settings()
        text, keyboard = other_menu_keyboard(settings)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await state.clear()

    except Exception as e:
        logger.error(f"Error processing timeframe input: {e}")
        await message.answer(" Помилка обробки таймфрейму. Спробуйте ще раз.")


async def process_disable_setting(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    field_map = {
        "SettingsStates:waiting_for_pinbar_tail": ("pinbar_tail_percent", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_body": ("pinbar_body_percent", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_opposite": ("pinbar_opposite_percent", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_min_size": ("pinbar_min_size", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_max_size": ("pinbar_max_size", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_avg_candles": ("pinbar_avg_candles", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_min_avg_percent": ("pinbar_min_avg_percent", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_max_avg_percent": ("pinbar_max_avg_percent", "back_pinbar"),
        "SettingsStates:waiting_for_pinbar_timeout": ("pinbar_timeout", "back_security"),
        "SettingsStates:waiting_for_rsi_high": ("rsi_high", "back_rsi"),
        "SettingsStates:waiting_for_rsi_low": ("rsi_low", "back_rsi"),
        "SettingsStates:waiting_for_max_retries": ("max_retries", "back_trades"),
        "SettingsStates:waiting_for_max_trades": ("max_open_trades", "back_trades"),
        "SettingsStates:waiting_for_max_drawdown": ("max_drawdown_percent", "back_security"),
        "SettingsStates:waiting_for_limit_candles": ("limit_max_candles", "back_security"),
        "SettingsStates:waiting_for_position_size": ("position_size_percent", "back_main"),
        "SettingsStates:waiting_for_timeframe": ("timeframe", "back_other"),
        "SettingsStates:waiting_for_stop_loss_offset": ("stop_loss_offset", "back_security"),
        "SettingsStates:waiting_for_max_losses_in_row": ("max_losses_in_row", "back_security"),
        "SettingsStates:waiting_for_pause_after_losses": ("pause_after_losses", "back_security"),
        "SettingsStates:waiting_for_max_equity_drawdown": ("max_equity_drawdown", "back_security"),
        "SettingsStates:waiting_for_limit_order_lifetime": ("limit_order_lifetime", "back_security"),
    }
    if current_state in field_map:
        field, back_callback = field_map[current_state]
        update_setting(field, None)
        await callback.message.edit_text(f"Настройка '{field}' отключена")
        await state.clear()
        if back_callback == "back_pinbar":
            await process_back_pinbar(callback)
        elif back_callback == "back_rsi":
            await process_back_rsi(callback)
        elif back_callback == "back_trades":
            await process_back_trades(callback)
        elif back_callback == "back_security":
            await process_back_security(callback)
        elif back_callback == "back_other":
            await process_back_other(callback)
        else:
            await process_back_main(callback)
    await callback.answer()


async def process_strategy_setting(callback: CallbackQuery, state: FSMContext = None):
    settings = get_settings()
    if callback.data == "menu_strategy":
        text, keyboard = strategy_menu_keyboard(settings)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()
        return
    if callback.data == "strategy_quantum":
        from utils.settings_manager import get_strategies, toggle_strategy

        new = toggle_strategy("Quantum")
        try:
            setting_changed("strategy", new)
        except Exception:
            pass
        enabled = ", ".join(get_strategies()) or "none"
        await callback.message.edit_text(f" Обновлено стратегії: {enabled}", parse_mode="HTML")
        await callback.answer()
        return
    if callback.data == "strategy_gravity2":
        from utils.settings_manager import get_strategies, toggle_strategy

        new = toggle_strategy("Gravity2")
        try:
            setting_changed("strategy", new)
        except Exception:
            pass
        enabled = ", ".join(get_strategies()) or "none"
        await callback.message.edit_text(f" Обновлено стратегії: {enabled}", parse_mode="HTML")
        await callback.answer()
        return


async def start_bot():
    global bot, dp
    if bot is None or dp is None:
        init_bot()
    if bot and dp:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Aiogram polling error: {e}")
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass
            try:
                await dp.storage.close()
            except Exception:
                pass
            logger.info(" Aiogram bot stopped")


async def stop_bot():
    global bot, dp
    try:
        if bot:
            await bot.session.close()
        if dp and getattr(dp, "storage", None):
            await dp.storage.close()
    except Exception as e:
        logger.warning(f"Error stopping aiogram bot: {e}")
    logger.info(" Aiogram bot stopped")


async def notify_user(message: str):
    global bot
    user_id = 5197139803
    if bot is None:
        init_bot()
    if bot:
        try:
            await bot.send_message(chat_id=user_id, text=message)
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")


if __name__ == "__main__":
    asyncio.run(start_bot())
