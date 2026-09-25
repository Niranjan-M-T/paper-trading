-- Shadow buy-points: what the strategy WANTS to buy while the live account is cash-gated.
--
-- Populated each tick by a second, non-persisting engine replay run with unlimited simulated
-- cash (real_trader.emit_shadow_buy_points → replay_one_portfolio(..., persist=False)). Purely
-- informational: NO order is ever placed from this table. It lets you see — when a SIP finally
-- lands — which entries the strategy wanted while there was no money, and whether each is still
-- catchable at that price or lower (the /bot page joins these rows against live candles).
CREATE TABLE IF NOT EXISTS shadow_buy_points (
    id           BIGSERIAL PRIMARY KEY,
    signal_key   TEXT UNIQUE NOT NULL,           -- '<date>|<symbol>|<reason>' — one row per wanted entry
    portfolio_id INTEGER REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    signal_date  DATE NOT NULL,
    signal_time  TEXT,                            -- 'HH:MM' scan bar the entry fired on (nullable)
    price        NUMERIC(18, 4) NOT NULL,         -- the price the strategy would have paid
    reason       TEXT,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS shadow_buy_points_date   ON shadow_buy_points (signal_date DESC);
CREATE INDEX IF NOT EXISTS shadow_buy_points_symbol ON shadow_buy_points (symbol);
