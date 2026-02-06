-- Add entry_offset_percent column to settings table
-- This parameter controls how far beyond the candle HIGH/LOW to place entry orders
-- Default: 0.01 = 0.01% beyond the extremum
-- LONG: entry ABOVE candle HIGH by this percentage
-- SHORT: entry BELOW candle LOW by this percentage

ALTER TABLE settings 
ADD COLUMN IF NOT EXISTS entry_offset_percent DECIMAL(10, 4) DEFAULT 0.01;
