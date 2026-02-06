import threading
import time

import psycopg2
from psycopg2 import sql

from config import CONFIG
from utils.logger import setting_changed

DB_HOST = CONFIG["DB_HOST"]
DB_PORT = CONFIG["DB_PORT"]
DB_NAME = CONFIG["DB_NAME"]
DB_USER = CONFIG["DB_USER"]
DB_PASS = CONFIG["DB_PASS"]
DB_SSL = CONFIG.get("DB_SSL", False)

SETTING_COLUMNS = (
    "id",
    "body_tail_ratio",
    "pinbar_size",
    "rsi_low",
    "rsi_high",
    "rsi_period",
    "rsi_interval",
    "max_retries",
    "max_open_trades",
    "stop_loss_offset",
    "timestamp_offset",
    "pinbar_timeout",
    "strategy",
    "bot_was_active_before_panika",
    "pinbar_tail_percent",
    "pinbar_body_percent",
    "pinbar_opposite_percent",
    "position_size_percent",
    "limit_timeout",
    "max_drawdown_percent",
    "dd_stop_active",
    "equity_peak",
    "limit_max_candles",
    "timeframe",
    "max_losses_in_row",
    "pause_after_losses",
    "max_equity_drawdown",
    "losses_in_row",
    "pause_until",
    "limit_order_lifetime",
    "trigger_timeout",
    "pinbar_min_size",
    "pinbar_max_size",
    "pinbar_avg_candles",
    "pinbar_min_avg_percent",
    "pinbar_max_avg_percent",
)
ALLOWED_FIELDS = set(SETTING_COLUMNS) - {"id"}

_lock = threading.Lock()
_settings_store = {
    "body_tail_ratio": CONFIG.get("PINBAR_MIN_RATIO", 2.5),
    "pinbar_size": CONFIG.get("PINBAR_MIN_RATIO", 0.5),
    "rsi_low": CONFIG.get("RSI_OVERSOLD", 30),
    "rsi_high": CONFIG.get("RSI_OVERBOUGHT", 70),
    "rsi_period": CONFIG.get("RSI_PERIOD", 14),
    "rsi_interval": CONFIG.get("TIMEFRAME", "1"),
    "max_retries": CONFIG.get("MAX_RETRIES", 2),
    "max_open_trades": CONFIG.get("MAX_OPEN_TRADES", 3),
    "stop_loss_offset": CONFIG.get("STOP_LOSS_OFFSET", 0.007),
    "timestamp_offset": 0,
    "pinbar_timeout": 100,
    "strategy": "Quantum",
    "pinbar_tail_percent": 0,
    "pinbar_body_percent": 100,
    "pinbar_opposite_percent": 100,
    "position_size_percent": CONFIG.get("POSITION_SIZE_PERCENT", 1.5),
    "limit_timeout": CONFIG.get("LIMIT_MAX_CANDLES", 3),
    "max_drawdown_percent": CONFIG.get("MAX_DRAWDOWN_PERCENT", 0),
    "limit_max_candles": CONFIG.get("LIMIT_MAX_CANDLES", 3),
    "timeframe": CONFIG.get("TIMEFRAME", "1"),
    "max_losses_in_row": CONFIG.get("MAX_LOSSES_IN_ROW", 3),
    "pause_after_losses": CONFIG.get("PAUSE_AFTER_LOSSES", 10),
    "max_equity_drawdown": CONFIG.get("MAX_EQUITY_DRAWDOWN", 0),
    "losses_in_row": 0,
    "pause_until": None,
    "bot_was_active_before_panika": True,
    "blacklist": set(),
    "strategy_list": set(s.strip() for s in (CONFIG.get("STRATEGY", "Quantum") or "Quantum").split(",") if s.strip()),
    "equity_peak": 0.0,
    "dd_stop_active": False,
    "limit_order_lifetime": CONFIG.get("LIMIT_ORDER_LIFETIME", 5),
    "trigger_timeout": 100,
    "pinbar_min_size": 0.05,
    "pinbar_max_size": 2.0,
    "pinbar_avg_candles": 10,
    "pinbar_min_avg_percent": 50,
    "pinbar_max_avg_percent": 200,
}
_db_ready = False


def get_connection():
    ssl_mode = "require" if DB_SSL else "prefer"
    return psycopg2.connect(
        host=DB_HOST, 
        port=DB_PORT, 
        dbname=DB_NAME, 
        user=DB_USER, 
        password=DB_PASS,
        sslmode=ssl_mode,
        connect_timeout=10
    )


def _normalize_timeframe_for_db(value):
    if not value:
        return "1m"

    value = str(value).strip().upper()

    internal_to_display = {
        "1": "1m",
        "3": "3m",
        "5": "5m",
        "15": "15m",
        "30": "30m",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "360": "6h",
        "720": "12h",
        "D": "1d",
        "W": "1w",
        "M": "1M",
    }

    if value in ("1M", "3M", "5M", "15M", "30M", "1H", "2H", "4H", "6H", "12H", "1D", "1W", "1MON"):
        return value.lower() if value != "1MON" else "1M"

    if value in internal_to_display:
        return internal_to_display[value]

    return value.lower()


def _format_timeframe_for_display(internal_value):
    return _normalize_timeframe_for_db(internal_value)


def _parse_timeframe_to_internal(value):
    """Parse timeframe from any format to internal API format (1, 5, 60, D, W, M)"""
    if not value:
        return "1"

    value = str(value).strip().upper()

    display_to_internal = {
        "1M": "1",
        "3M": "3",
        "5M": "5",
        "15M": "15",
        "30M": "30",
        "1H": "60",
        "2H": "120",
        "4H": "240",
        "6H": "360",
        "12H": "720",
        "1D": "D",
        "1W": "W",
        "1MON": "M",
    }

    if value in display_to_internal:
        return display_to_internal[value]

    valid_internal = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}
    if value in valid_internal:
        return value

    return "1"


def init_db():
    global _db_ready
    if _db_ready:
        return True
    try:
        from utils.logger import logger
        conn = get_connection()
        cur = conn.cursor()
        logger.info("SettingsManager: Підключення до бази даних (Aiven Cloud/SSL) встановлено")
        cur.execute("CREATE TABLE IF NOT EXISTS settings (id SERIAL PRIMARY KEY)")

        for col in [
            "strategy",
            "rsi_low",
            "rsi_high",
            "rsi_period",
            "rsi_interval",
            "max_retries",
            "max_open_trades",
            "stop_loss_offset",
            "pinbar_timeout",
            "limit_max_candles",
            "timeframe",
            "max_drawdown_percent",
            "equity_peak",
            "dd_stop_active",
            "pinbar_tail_percent",
            "pinbar_body_percent",
            "pinbar_opposite_percent",
            "position_size_percent",
            "limit_timeout",
            "max_losses_in_row",
            "pause_after_losses",
            "max_equity_drawdown",
            "losses_in_row",
            "pause_until",
            "bot_was_active_before_panika",
            "timestamp_offset",
            "body_tail_ratio",
            "pinbar_size",
            "limit_order_lifetime",
        ]:
            cur.execute(f"ALTER TABLE settings ADD COLUMN IF NOT EXISTS {col} TEXT")

        cur.execute("CREATE TABLE IF NOT EXISTS blacklist (symbol TEXT PRIMARY KEY)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                id SERIAL PRIMARY KEY,
                is_active BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        conn.commit()

        # Get existing columns to avoid unnecessary ALTER TABLE calls which lock the table
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'settings'"
        )
        existing_columns = {row[0] for row in cur.fetchall()}

        # Додаємо всі відсутні колонки з SETTING_COLUMNS
        for col in SETTING_COLUMNS:
            if col == "id":
                continue
            
            if col not in existing_columns:
                try:
                    logger.info(f"Adding missing column: {col}")
                    cur.execute(
                        sql.SQL("ALTER TABLE settings ADD COLUMN IF NOT EXISTS {col} TEXT").format(col=sql.Identifier(col))
                    )
                    conn.commit()
                except Exception as e:
                    logger.error(f"Failed to add column {col}: {e}")
                    conn.rollback()
        
        # Don't commit here again as we commit per column or rollback
        # conn.commit()

        cur.execute("SELECT count(*) FROM settings")
        count = cur.fetchone()[0]
        if count == 0:
            # Створюємо початковий запис з усіма поточними налаштуваннями
            keys = [k for k in SETTING_COLUMNS if k != "id"]
            cols_str = ", ".join(keys)
            placeholders = ", ".join(["%s"] * len(keys))
            values = []
            for k in keys:
                val = _settings_store.get(k)
                if isinstance(val, (set, list, dict)):
                    values.append(str(val))
                else:
                    values.append(str(val) if val is not None else "None")
            
            query = f"INSERT INTO settings ({cols_str}) VALUES ({placeholders})"
            cur.execute(query, tuple(values))
            conn.commit()
            print("Створено початковий запис налаштувань у БД")

        cur.close()
        conn.close()
        _db_ready = True
        return True
    except Exception as e:
        print(f"DB init error: {e}")
        return False


def _persist_setting_db(key, value):
    if key not in ALLOWED_FIELDS:
        return
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM settings ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
        if not r:
            cur.execute(
                "INSERT INTO settings (strategy) VALUES (%s) RETURNING id", (str(_settings_store.get("strategy")),)
            )
            conn.commit()
        try:
            cur.execute(
                sql.SQL("ALTER TABLE settings ADD COLUMN IF NOT EXISTS {col} TEXT").format(col=sql.Identifier(key))
            )
            cur.execute(
                sql.SQL(
                    "UPDATE settings SET {col} = %s WHERE id = (SELECT id FROM settings ORDER BY id DESC LIMIT 1)"
                ).format(col=sql.Identifier(key)),
                (str(value),),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        cur.close()
        conn.close()
    except Exception:
        pass


def get_bybit_base_url():
    return "https://api-demo.bybit.com" if CONFIG.get("USE_TESTNET", False) else "https://api.bybit.com"


def get_settings():
    global _db_ready
    with _lock:
        if not _db_ready:
            init_db()
            
        try:
            conn = get_connection()
            cur = conn.cursor()
            cols = ", ".join(SETTING_COLUMNS)
            cur.execute(f"SELECT {cols} FROM settings ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                settings_dict = dict(_settings_store)
                for k, v in zip(SETTING_COLUMNS, row):
                    if v is None or str(v) == "None" or v == "":
                        continue
                    
                    # Автоматичне перетворення типів
                    try:
                        if k in ["rsi_low", "rsi_high", "position_size_percent", "stop_loss_offset", "max_drawdown_percent", "pinbar_tail_percent", "pinbar_body_percent", "pinbar_opposite_percent", "body_tail_ratio", "pinbar_size"]:
                            settings_dict[k] = float(v)
                        elif k in ["rsi_period", "max_retries", "max_open_trades", "pinbar_timeout", "trigger_timeout", "limit_max_candles", "limit_timeout", "max_losses_in_row", "pause_after_losses", "losses_in_row", "limit_order_lifetime"]:
                            settings_dict[k] = int(v)
                        elif k == "id":
                            continue
                        else:
                            settings_dict[k] = v
                    except:
                        settings_dict[k] = v

                return settings_dict
        except Exception as e:
            print(f"Get settings error: {e}")
        return dict(_settings_store)


def get_setting(key, default=None):
    with _lock:
        s = _settings_store.get(key, default)
    return s


def update_setting(key, value):

    if key == "timeframe":
        value = _normalize_timeframe_for_db(value)

    with _lock:
        _settings_store[key] = value
    try:
        _persist_setting_db(key, value)
    except Exception as e:
        print(f"Persist setting error for {key}: {e}")
    return True


def get_strategies():
    with _lock:
        return list(_settings_store.get("strategy_list", set()))


def toggle_strategy(name):
    with _lock:
        current = set(_settings_store.get("strategy_list", set()))
        if name in current:
            current.discard(name)
        else:
            current.add(name)
        val = ",".join(sorted(current))
        _settings_store["strategy"] = val
        _settings_store["strategy_list"] = current
    update_setting("strategy", val)
    return val


def is_blacklisted(symbol):
    with _lock:
        bl = set(_settings_store.get("blacklist", set()))
        return symbol.upper() in bl


def add_to_blacklist(symbol):
    with _lock:
        _settings_store.setdefault("blacklist", set()).add(symbol.upper())
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO blacklist (symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING", (symbol.upper(),))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass
    return True


def remove_from_blacklist(symbol):
    with _lock:
        _settings_store.setdefault("blacklist", set()).discard(symbol.upper())
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM blacklist WHERE symbol = %s", (symbol.upper(),))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass
    return True


def get_blacklist():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT symbol FROM blacklist ORDER BY symbol")
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception:
        with _lock:
            return list(_settings_store.get("blacklist", set()))


def is_bot_active():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT is_active FROM bot_state ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
        cur.close()
        conn.close()
        if r is None:
            return True
        return bool(r[0])
    except Exception:
        return bool(_settings_store.get("bot_was_active_before_panika", True))


def set_bot_active(state: bool):
    with _lock:
        _settings_store["bot_was_active_before_panika"] = bool(state)
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO bot_state (is_active, updated_at) VALUES (%s, CURRENT_TIMESTAMP)", (bool(state),))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass
    return True


def is_trading_paused():
    with _lock:
        pause_until = _settings_store.get("pause_until")
        if not pause_until:
            return False
        return time.time() < float(pause_until)


def set_trading_paused(minutes):
    with _lock:
        _settings_store["pause_until"] = time.time() + max(0, int(minutes) * 60)
    return True


def increment_losses_in_row():
    with _lock:
        _settings_store["losses_in_row"] = int(_settings_store.get("losses_in_row", 0)) + 1
        val = _settings_store["losses_in_row"]
    try:
        setting_changed("losses_in_row", val)
    except Exception:
        pass
    return val


def reset_losses_in_row():
    with _lock:
        _settings_store["losses_in_row"] = 0
    try:
        setting_changed("losses_in_row", 0)
    except Exception:
        pass
    return True


def get_losses_in_row():
    with _lock:
        return int(_settings_store.get("losses_in_row", 0))


def get_max_losses_in_row():
    with _lock:
        val = _settings_store.get("max_losses_in_row")
        if val is None:
            return 999999
        return int(val)


def get_pause_after_losses():
    with _lock:
        val = _settings_store.get("pause_after_losses")
        if val is None:
            return 0
        return int(val)


def is_drawdown_protection_active():
    with _lock:
        return bool(_settings_store.get("dd_stop_active", False))


def check_and_update_drawdown(current_equity):
    with _lock:
        peak = float(_settings_store.get("equity_peak", 0.0))
        max_dd_pct_val = _settings_store.get("max_drawdown_percent")

        if max_dd_pct_val is None:
            return False

        max_dd_pct = float(max_dd_pct_val)

        if peak <= 0:
            _settings_store["equity_peak"] = float(current_equity)
            return False
        if current_equity > peak:
            _settings_store["equity_peak"] = float(current_equity)
            return False
        if max_dd_pct <= 0:
            return False
        drawdown = (peak - current_equity) / peak * 100.0
        if drawdown >= max_dd_pct:
            _settings_store["dd_stop_active"] = True
            try:
                setting_changed("dd_stop_active", True)
            except Exception:
                pass
            return True
    return False


def reset_drawdown_protection():
    with _lock:
        _settings_store["equity_peak"] = float(_settings_store.get("equity_peak", 0.0))
        _settings_store["dd_stop_active"] = False
    try:
        setting_changed("dd_stop_active", False)
    except Exception:
        pass
    return True


def is_equity_drawdown_triggered():
    return is_drawdown_protection_active()


def set_trading_stopped():
    set_bot_active(False)
    return True


_bot_active = True
