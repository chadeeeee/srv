import os
from dotenv import load_dotenv

load_dotenv()

CONFIG = {
    # Database
    "DB_HOST": os.getenv("DB_HOST", "localhost"),
    "DB_PORT": os.getenv("DB_PORT", "5432"),
    "DB_NAME": os.getenv("DB_NAME", "trading"),
    "DB_USER": os.getenv("DB_USER", "postgres"),
    "DB_PASS": os.getenv("DB_PASS", ""),
    "DB_SSL": os.getenv("DB_SSL", "false").lower() == "true",
    
    # Bybit API
    "BYBIT_API_KEY": os.getenv("BYBIT_API_KEY", ""),
    "BYBIT_API_SECRET": os.getenv("BYBIT_API_SECRET", ""),
    "USE_TESTNET": os.getenv("USE_TESTNET", "true").lower() == "true",
    "RECV_WINDOW": os.getenv("RECV_WINDOW", "20000"),
    
    # Telegram
    "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID", ""),
    "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH", ""),
    "TELEGRAM_CHANNEL": os.getenv("TELEGRAM_CHANNEL", ""),
    "TELEGRAM_CHANNELS": os.getenv("TELEGRAM_CHANNELS", ""),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    
    # Channel IDs for strategies
    "CHANNEL_PREMIUM2_ID": int(os.getenv("CHANNEL_PREMIUM2_ID", "-1002428177498")),
    "CHANNEL_GRAVITY2_ID": int(os.getenv("CHANNEL_GRAVITY2_ID", "-1002260822616")),
    
    # Trading defaults
    "POSITION_SIZE_PERCENT": float(os.getenv("POSITION_SIZE_PERCENT", "1.5")),
    "DEFAULT_ORDER_QTY": float(os.getenv("DEFAULT_ORDER_QTY", "10")),
    "MAX_OPEN_TRADES": int(os.getenv("MAX_OPEN_TRADES", "3")),
    "MAX_RETRIES": int(os.getenv("MAX_RETRIES", "2")),
    "STOP_LOSS_OFFSET": float(os.getenv("STOP_LOSS_OFFSET", "0.007")),
    "STOP_BEHIND_EXTREMUM_OFFSET": float(os.getenv("STOP_BEHIND_EXTREMUM_OFFSET", "0.002")),
    
    # RSI
    "RSI_PERIOD": int(os.getenv("RSI_PERIOD", "14")),
    "RSI_OVERSOLD": int(os.getenv("RSI_OVERSOLD", "30")),
    "RSI_OVERBOUGHT": int(os.getenv("RSI_OVERBOUGHT", "70")),
    
    # Pinbar
    "PINBAR_MIN_RATIO": float(os.getenv("PINBAR_MIN_RATIO", "2.5")),
    
    # Timeframe
    "TIMEFRAME": os.getenv("TIMEFRAME", "1"),
    "LIMIT_MAX_CANDLES": int(os.getenv("LIMIT_MAX_CANDLES", "3")),
    "LIMIT_ORDER_LIFETIME": int(os.getenv("LIMIT_ORDER_LIFETIME", "5")),
    
    # Risk management
    "MAX_DRAWDOWN_PERCENT": float(os.getenv("MAX_DRAWDOWN_PERCENT", "0")),
    "MAX_LOSSES_IN_ROW": int(os.getenv("MAX_LOSSES_IN_ROW", "3")),
    "PAUSE_AFTER_LOSSES": int(os.getenv("PAUSE_AFTER_LOSSES", "10")),
    "MAX_EQUITY_DRAWDOWN": float(os.getenv("MAX_EQUITY_DRAWDOWN", "0")),
    
    # Strategy
    "STRATEGY": os.getenv("STRATEGY", "Quantum"),
    
    # Balance logging
    "BALANCE_LOG_INTERVAL": float(os.getenv("BALANCE_LOG_INTERVAL", "300")),
}
