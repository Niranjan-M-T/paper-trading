# 07 — Known issues & roadmap

## Open issue 1 — ledger duplication (cosmetic, not a trading bug)

**Symptom:** the `trades` table shows the same intended trade many times. Observed
example: `S50_drop3_e15_alloc10_vol11_50k` had ~28 BUY rows for DRREDDY, 3 shares each,
all `entry_scan_11:00_drop_-3%`, spread across `ts` 10:40/10:45/10:50/10:55/11:00 at
slightly different prices.

**Root cause (three verified facts compose):**

1. **The engine fires a scan early on the live day.** It reads a scan as
   `time <= scan_time`, so during the current session the "11:00 scan" evaluates against
   whatever bar is latest *now* (10:40 bar at 10:40, etc.). Hence `entry_scan_11:00_*`
   rows stamped at earlier timestamps.
2. **The trader re-runs the full replay every 60s** while the poller keeps rewriting the
   still-forming 5-minute bar. Within one bar, the close churns (e.g. DRREDDY walked
   1290.99 → 1288.39 → 1290.09 → 1290.69 → 1287.29), so each tick re-emits the same
   intended trade at a different price.
3. **`price` is in the dedupe key** — `trades_portfolio_dedupe (portfolio_id, ts, symbol,
   side, qty, price, reason)` in `sql/001_schema.sql`. So `ON CONFLICT DO NOTHING` never
   fires for a re-priced re-emission; every price variant inserts a new row.

~5 timestamps × ~5 price variants ≈ the ~28 rows.

**Why it's harmless:** the engine can't over-buy — `if symbol in holdings: continue`
(v2_engine) means each fresh replay buys the symbol once. `positions` is DELETE+INSERT
from the engine's holdings each tick, and `equity_curve` is engine-derived. **Nothing
reads the `trades` table back for state** — it's a write-only audit log. Proof: the
affected portfolio sat at `final_equity ≈ 51,127` on ₹50k; 84 real shares of DRREDDY
(~₹108k) would have made the equity nonsense. It's a **ledger display artifact**, not
over-trading. It predates the S505 shadow work (rows seen dated 2026-07-09).

**Why only some portfolios show it:** only a *scan entry on a live, still-forming day*
triggers it. Exits (`target_+..._tierN`) show single clean rows because they evaluate on
settled bars.

**The gap:** the right guard already exists — `real_executor.scan_time_elapsed()`, whose
docstring describes this exact failure — but it's wired only into *real order placement*
(`real_trader.py`), not into the *ledger write*. Both traders write trades through the
shared `replay.upsert_trades`, so the live `S404_live_sip_20k` ledger has the same
pollution, just less of it.

**Proposed fix (not yet applied):**
- **Prevent:** in `replay_one_portfolio`, filter today's not-yet-final scan entries before
  `upsert_trades`, reusing `scan_time_elapsed` — the same rule the real bot already applies
  at placement, moved down to the ledger layer so both traders inherit it. Once the scan
  bar finalises (e.g. 11:05) the engine emits one stable trade at the final close, which
  inserts once and never churns.
- **Clean up:** a `tools/` script that reconciles each portfolio's `trades` against the
  engine's authoritative re-emission and deletes the orphans (safer than a
  "keep MAX(id) per group" heuristic).

**Impact on the S505 migration:** trade *counts* are inflated for every paper portfolio,
so during shadow validation compare S505 vs S404 on **equity curves**, not trade counts.

**Sizing query:**
```
psql -h 127.0.0.1 -U paper -d paper_trading -c "
SELECT p.name, t.symbol, t.ts, t.side, t.qty, t.reason, COUNT(*) AS dup_rows
  FROM trades t JOIN portfolios p ON p.id = t.portfolio_id
 GROUP BY p.name, t.symbol, t.ts, t.side, t.qty, t.reason
HAVING COUNT(*) > 1
 ORDER BY dup_rows DESC LIMIT 25;"
```

## Open issue 2 — DPWIRES yfinance log spam

A 2026-07-16 debug bundle showed ~87 `['DPWIRES.NS']: possibly delisted; no price data
found` ERROR lines, one per poll cycle, despite the `yf_provider` commit that sets the
`yfinance` logger to `CRITICAL`. Either the deployed poller predates that commit or
yfinance emits these outside the muted logger. **It's noise, not data loss** — DPWIRES is
a known-gapped symbol (one of DPWIRES/PLAZACABLE/SURANAT&P/BFUTILITIE) that the Angel
per-symbol top-up covers. Worth confirming the deployed code includes the silencing commit,
and if so, tracking down the second emission path (e.g. `yfinance`'s `logging` vs a
`print`/`warnings` channel, or a child logger name).

## Roadmap / future plans

- **Phase 6 of the S505 migration** — flip live money S404 → S505 after shadow validation.
  The headline goal. See [06-s505-migration.md](06-s505-migration.md). Gated on ~1–2 weeks
  of clean shadow behavior and the owner's explicit go-ahead.
- **Fix the ledger duplication** (issue 1) — prevention filter + cleanup script. Worth
  doing before Phase 6 so shadow trade counts are trustworthy.
- **Resolve the DPWIRES noise** (issue 2).
- **Ongoing engine re-vendoring discipline** — when the research engine evolves, re-vendor
  `v2_engine.py`, re-apply the patch points (regime priming from DB, ATR pandas≥2.2 fix,
  cache-based index loads), and re-run `pytest` + `tools/parity_s505.py`.

## How to keep this documentation alive

These docs are a point-in-time snapshot (2026-07-16). When you make a substantive change:
- update the relevant `docs/0N-*.md`,
- if it changes a golden rule or the mental model, update the root `CLAUDE.md`,
- append a dated entry to [09-decision-log.md](09-decision-log.md).

The owner also keeps a private cross-session memory for this project (outside the repo)
that mirrors much of this; the durable home for teammates is these files.
