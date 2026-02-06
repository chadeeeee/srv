CREATE TABLE IF NOT EXISTS detected_pinbars (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    open_price DECIMAL(20, 8) NOT NULL,
    high_price DECIMAL(20, 8) NOT NULL,
    low_price DECIMAL(20, 8) NOT NULL,
    close_price DECIMAL(20, 8) NOT NULL,
    entry_price DECIMAL(20, 8) NOT NULL,
    stop_price DECIMAL(20, 8) NOT NULL,
    invalidate_price DECIMAL(20, 8) NOT NULL,
    body_size DECIMAL(20, 8),
    total_range DECIMAL(20, 8),
    wick_size DECIMAL(20, 8),
    is_extremum BOOLEAN DEFAULT FALSE,
    detected_at TIMESTAMP DEFAULT NOW(),
    order_placed BOOLEAN DEFAULT FALSE,
    order_id VARCHAR(50),
    UNIQUE(symbol, direction, detected_at)
);

CREATE INDEX idx_pinbars_symbol ON detected_pinbars(symbol);
CREATE INDEX idx_pinbars_detected_at ON detected_pinbars(detected_at DESC);
