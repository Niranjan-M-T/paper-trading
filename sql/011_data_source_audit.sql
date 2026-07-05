-- 011_data_source_audit.sql — daily verification that the hybrid (yfinance) data
-- source isn't hurting the live trader. One row per trading day; the /bot page and
-- tools/verify_data_source.py read/write it. Idempotent.

CREATE TABLE IF NOT EXISTS data_source_audit (
    audit_date          DATE PRIMARY KEY,
    data_source         TEXT NOT NULL,            -- 'angel' | 'yfinance' (what the poller used)
    symbols_checked     INTEGER NOT NULL,
    symbols_with_gaps   INTEGER NOT NULL DEFAULT 0,  -- symbols missing >2 bars vs expected
    bars_expected       INTEGER,                  -- per-symbol expected 5m bars for the session
    bars_captured_med   INTEGER,                  -- median bars actually stored per symbol
    close_max_pctdiff   NUMERIC(10,4),            -- worst close disagreement vs Angel (sample)
    vol_median_ratio    NUMERIC(10,4),            -- median yf/angel volume ratio (sample)
    entries_placed      INTEGER NOT NULL DEFAULT 0,
    entries_unconfirmed INTEGER NOT NULL DEFAULT 0,  -- BUYs skipped by the Angel confirm guard
    orders_rejected     INTEGER NOT NULL DEFAULT 0,
    verdict             TEXT NOT NULL DEFAULT 'ok',   -- 'ok' | 'warn' | 'bad'
    detail              JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS data_source_audit_date_idx ON data_source_audit (audit_date DESC);
