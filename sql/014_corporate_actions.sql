-- 014_corporate_actions.sql
-- Split / bonus back-adjustment for the live + paper candle series.
--
-- The poller stores RAW, unadjusted OHLCV (src/core/yf_provider.py auto_adjust=False),
-- so a stock split or bonus issue leaves a discontinuity in the stored history: the raw
-- close drops (e.g. /5 on a 5:1 split) and raw volume jumps (×5) on the ex-date. That
-- corrupts every rolling feature that spans the boundary — the 90-day high (→ false deep-
-- dip entries), volume_avg20 (→ phantom volume spike), ATR/SMAs — for up to ~90 days, and
-- a held position's exit basis. The algo BACKTEST avoids this with yfinance auto_adjust=True;
-- this table lets the live/paper engine reproduce the same adjustment.
--
-- Applied at candle READ time (src/engine/corporate_actions.py, wired into
-- replay.load_candles_window) — NON-DESTRUCTIVE: the stored candles stay raw, so orders
-- still place at the real current price and a wrong row can be reversed by flipping `active`.
-- A bar strictly before ex_date is divided by `ratio` (volume multiplied); on/after, untouched.

CREATE TABLE IF NOT EXISTS corporate_actions (
    id          SERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,                       -- engine symbol (e.g. 'RELIANCE')
    ex_date     DATE NOT NULL,                       -- first trading day in post-action price space
    action_type TEXT NOT NULL DEFAULT 'split',       -- 'split' | 'bonus' (both are a ratio adjust)
    ratio       NUMERIC(12,6) NOT NULL,              -- new shares per old share; price ÷ ratio, vol × ratio
    source      TEXT,                                -- 'yfinance' | 'manual'
    note        TEXT,
    active      BOOLEAN NOT NULL DEFAULT TRUE,        -- flip FALSE to reverse a wrong entry (non-destructive)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, ex_date, action_type)
);

-- Read-time adjustment filters on the active rows for a set of symbols.
CREATE INDEX IF NOT EXISTS corporate_actions_active_symbol
    ON corporate_actions (symbol) WHERE active;
