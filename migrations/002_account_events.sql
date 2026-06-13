CREATE TABLE IF NOT EXISTS account_events (
    event_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    symbol TEXT,
    asset TEXT,
    amount NUMERIC NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
