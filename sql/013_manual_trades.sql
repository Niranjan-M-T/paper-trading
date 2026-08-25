-- 013_manual_trades.sql
-- Live manual-trade tagger.
--
-- Angel's order book is account-wide — it includes orders you place by hand in the
-- Angel app/web, not just the ones the bot placed via the API. Every tick the real
-- trader fetches the order book and records any FILLED order whose Angel orderid is
-- NOT in real_orders (i.e. the bot didn't place it) here. That gives /bot a clean
-- bot-vs-manual split and lets the accounting separate the two.
--
-- Scope note: Angel's order-book API is essentially current-day, so this captures
-- manual trades from the moment it's deployed forward. Full history (older closed
-- manual trades) still needs Angel's downloadable Trade Book / Tax P&L report.

CREATE TABLE IF NOT EXISTS manual_trades (
    order_id      TEXT PRIMARY KEY,          -- Angel orderid (account-wide, unique)
    symbol        TEXT NOT NULL,             -- engine symbol (series suffix stripped, e.g. INOXGREEN)
    tradingsymbol TEXT NOT NULL,             -- broker tradingsymbol as held (e.g. INOXGREEN-BE)
    side          TEXT NOT NULL,             -- BUY / SELL
    qty           INTEGER NOT NULL,          -- filled shares
    avg_price     NUMERIC(18,4),             -- average fill price
    status        TEXT,                      -- broker status (complete / open / ...)
    exchange      TEXT,
    product       TEXT,
    order_ts      TIMESTAMPTZ,               -- broker order time (IST)
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw           JSONB
);
CREATE INDEX IF NOT EXISTS manual_trades_ts_desc ON manual_trades (order_ts DESC NULLS LAST);
