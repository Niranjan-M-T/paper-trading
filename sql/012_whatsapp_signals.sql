-- 012_whatsapp_signals.sql
-- WhatsApp signal fan-out for the live real-money bot.
--
-- The live bot forwards every BUY/SELL signal to one or more WhatsApp groups via
-- the Evolution API gateway, so signals can be actioned by hand — the point is to
-- catch what the bot itself can't execute (e.g. AB4036 surveillance stocks that the
-- exchange hard-blocks). See src/core/whatsapp.py + src/runners/real_trader.py.
--
-- Two tables:
--   wa_targets   — editable list of destination groups (managed from /bot).
--   real_signals — send-once ledger keyed by the PRICE-FREE logical key so a
--                  still-forming bar (or a quarantined-every-tick BUY) can't
--                  re-notify the group on every 60s tick.

CREATE TABLE IF NOT EXISTS wa_targets (
    id         SERIAL PRIMARY KEY,
    jid        TEXT UNIQUE NOT NULL,          -- WhatsApp JID: '…@g.us' (group) or '…@s.whatsapp.net'
    label      TEXT,                          -- human name shown in the /bot UI
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the default group ("Stonks S525 trader signals"). ON CONFLICT so re-running
-- the migration is safe and never clobbers a label/enabled state edited from /bot.
INSERT INTO wa_targets (jid, label, enabled)
VALUES ('120363411936940548@g.us', 'Stonks S525 trader signals', TRUE)
ON CONFLICT (jid) DO NOTHING;

CREATE TABLE IF NOT EXISTS real_signals (
    signal_key   TEXT PRIMARY KEY,            -- date|symbol|side|reason (real_executor._logical_key_from_trade)
    portfolio_id INTEGER REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    price        NUMERIC(18,4) NOT NULL,
    reason       TEXT,
    placeable    BOOLEAN,                     -- did the bot attempt it, or is it manual-only (quarantine / no cash)?
    note         TEXT,                        -- e.g. 'surveillance/cautionary block (e.g. AB4036)'
    sent_ok      BOOLEAN NOT NULL DEFAULT FALSE,
    targets      INTEGER NOT NULL DEFAULT 0,  -- how many groups it was delivered to
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS real_signals_created_desc ON real_signals (created_at DESC);
