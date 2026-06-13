CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    binding TEXT NOT NULL,
    symbol TEXT NOT NULL,
    candle_close_time TIMESTAMPTZ NOT NULL,
    side TEXT,
    reason TEXT NOT NULL,
    confidence NUMERIC NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(binding, candle_close_time)
);

CREATE TABLE IF NOT EXISTS order_intents (
    id BIGSERIAL PRIMARY KEY,
    binding TEXT NOT NULL,
    symbol TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id BIGINT,
    purpose TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    reduce_only BOOLEAN NOT NULL DEFAULT false,
    stop_price NUMERIC,
    status TEXT NOT NULL,
    executed_quantity NUMERIC NOT NULL DEFAULT 0,
    average_price NUMERIC NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fills (
    exchange_trade_id BIGINT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    commission NUMERIC NOT NULL,
    realized_pnl NUMERIC NOT NULL,
    event_time TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS risk_state (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    daily_realized_pnl NUMERIC NOT NULL DEFAULT 0,
    realized_day DATE NOT NULL DEFAULT CURRENT_DATE,
    peak_equity NUMERIC NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    halted_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO risk_state(id) VALUES (1) ON CONFLICT DO NOTHING;
