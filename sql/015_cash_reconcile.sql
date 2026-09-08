------------------------------------------------------------------------
-- Cash-flow reconciliation queue (drives the one-tap deposit/withdrawal
-- confirmation on /bot).
--
-- The real trader watches broker FREE CASH netted against trades every tick
-- (real_trader.emit_cash_reconcile_alerts). When cash moves with no bot/manual
-- trade to explain it, it queues a PENDING row here instead of silently letting
-- the move become P&L. The owner then confirms it in one tap on the dashboard:
--   * confirm  → writes a real_deposits row (signed: + deposit, − withdrawal)
--   * dismiss  → it was a dividend / non-capital credit; no cost-basis change
-- Nothing is ever auto-booked: only the human can tell a deposit from a dividend,
-- since Angel's API exposes one blended cash figure, not a labelled ledger.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cash_reconcile (
    id             BIGSERIAL PRIMARY KEY,
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot_id    BIGINT NOT NULL,             -- the real_funds row that triggered it (dedup key)
    amount         NUMERIC(18, 4) NOT NULL,     -- SIGNED unexplained residual: + deposit, − withdrawal
    direction      TEXT NOT NULL,               -- 'deposit' | 'withdrawal' (from the residual sign)
    window_start   TIMESTAMPTZ,                 -- the funds window that was unexplained
    window_end     TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | confirmed | dismissed
    resolved_at    TIMESTAMPTZ,
    resolved_kind  TEXT,                         -- deposit | withdrawal | dividend | other
    deposit_id     BIGINT,                       -- the real_deposits row booked on confirm
    note           TEXT,
    UNIQUE (snapshot_id)                         -- one queued item per detecting snapshot
);
CREATE INDEX IF NOT EXISTS cash_reconcile_pending
    ON cash_reconcile (detected_at DESC) WHERE status = 'pending';
