-- Міграція для додавання поля limit_order_lifetime
-- Це поле зберігає час життя лімітного ордера в хвилинах

-- Додаємо колонку limit_order_lifetime до таблиці settings
ALTER TABLE settings ADD COLUMN IF NOT EXISTS limit_order_lifetime TEXT;

-- Встановлюємо значення за замовчуванням 5 хвилин для існуючих записів
UPDATE settings 
SET limit_order_lifetime = '5' 
WHERE limit_order_lifetime IS NULL;

-- Коментар для документації
COMMENT ON COLUMN settings.limit_order_lifetime IS 'Час життя лімітного ордера в хвилинах. Якщо ордер не виконується протягом цього часу, він автоматично скасовується.';
