-- Dodanie ustawień dla logiki trigger (Gravity2 SHORT)
-- Trigger pozwala na obserwację ceny i opóźnione otwieranie pozycji

-- Włączenie/wyłączenie logiki trigger
ALTER TABLE settings ADD COLUMN IF NOT EXISTS trigger_wait_enabled BOOLEAN DEFAULT FALSE;

-- Ile sekund czekać po aktywacji triggera przed otwarciem zlecenia
ALTER TABLE settings ADD COLUMN IF NOT EXISTS trigger_wait_seconds INTEGER DEFAULT 3;

-- Czy sprawdzać, że cena pozostaje poza zakresem świecy sygnałowej
ALTER TABLE settings ADD COLUMN IF NOT EXISTS trigger_candle_check_enabled BOOLEAN DEFAULT TRUE;

-- Timeout dla oczekiwania na aktywację triggera (w minutach)
ALTER TABLE settings ADD COLUMN IF NOT EXISTS trigger_timeout_minutes INTEGER DEFAULT 60;

-- Ustaw domyślne wartości dla istniejących rekordów
UPDATE settings 
SET 
    trigger_wait_enabled = FALSE,
    trigger_wait_seconds = 3,
    trigger_candle_check_enabled = TRUE,
    trigger_timeout_minutes = 60
WHERE trigger_wait_enabled IS NULL;
