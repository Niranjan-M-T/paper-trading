-- "Why isn't it buying?" diagnostics — one row per live portfolio, overwritten each shadow pass.
--
-- Written by real_trader.write_entry_diagnostics (always-on, ~every 5 min while the trader runs):
-- the regime the engine sees today, that regime's effective entry requirements, free cash vs one
-- position, what the shadow wanted today, what the bot actually bought, the closest near-miss, and
-- the bot's last buy. The /bot page and the daily digest render it via
-- real_executor.entry_verdict. Purely informational — nothing reads it to trade.
CREATE TABLE IF NOT EXISTS entry_diagnostics (
    portfolio_id INTEGER PRIMARY KEY REFERENCES portfolios(id) ON DELETE CASCADE,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload      JSONB NOT NULL
);
