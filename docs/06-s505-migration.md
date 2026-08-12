# 06 — The S404 → S505 live migration (current active project)

**Goal:** migrate the live real-money account from `S404_s392_side_only` to
`S505_pat_uni_vixpct_crash` (the research repo's Round-59 champion) — safely, without ever
switching real money to an unproven strategy. Started 2026-07-09.

## Why S505, and why carefully

S505 is the Round-59 winner in the backtester. But the vendored engine lacked the Round-59
regime machinery, and the live account is real money. So the migration is **parity-gated
and shadow-validated**: prove the vendored engine reproduces the research engine exactly,
run S505 in paper alongside the live S404 to watch real forward behavior, and only then
flip. See [02-strategies.md](02-strategies.md) for what S505/S525 actually are.

## The phase plan and status

| Phase | What | Status |
|---|---|---|
| **1** | Port the Round-59 levers into the vendored `StrategyV2` + `classify_regime_by_date` + a drawdown governor. All default to legacy → S404 byte-identical. | ✅ committed |
| **2** | Build the equal-weight universe index + breadth from DB 5m candles (`src/engine/universe_index.py`); wire day-cached priming into the traders (gated by `_any_universe_source`). | ✅ committed |
| **3** | Vendor + register S455/S505/S525 from the shared `_r59_base.py` chassis. Field-by-field diff vs the research repo = zero differences. | ✅ committed |
| **4** | Parity harness `tools/parity_s505.py`: run all three on both engines over identical data. | ✅ **ALL PASS** — trade-for-trade + equity-to-the-rupee, 2022–2025 |
| **5** | Shadow-run S505 (and S455/S525) as **paper** portfolios alongside the live S404. | ✅ deployed 2026-07-13 (commit 44ec457) — validation window in progress |
| **6** | Flip live `portfolios.strategy_id` S404 → S505. | ⛔ **NOT until Phase 5 validates** (~1–2 weeks of clean shadow behavior) |

## What "parity ALL PASS" means (Phase 4)

`tools/parity_s505.py` cross-imports the research engine (`engine_v2`, `strategies_v2`) and
the vendored engine, feeds both identical 5m prices and identical regime series (algo reads
CSVs; vendored is primed from the same files), runs S455/S505/S525 on each, and diffs. It
matched trade-for-trade and equity-to-the-rupee across bull, bear, crash, patience, and the
drawdown governor. Config parity is separately frozen by `tests/test_r59_strategies.py`.
This is the definitive evidence that the vendored engine *is* the research engine for these
strategies.

## Phase 5 — the shadow deployment (as deployed)

Six paper portfolios in `config/portfolios.yaml`, all `live=FALSE`:

```
S455_shadow_20k  / S455_shadow_100k   → S455_s447_pat250_12
S505_shadow_20k  / S505_shadow_100k   → S505_pat_uni_vixpct_crash
S525_shadow_20k  / S525_shadow_100k   → S525_s505_ddgov
```

- **Backdated `started_at = 2026-01-01 09:15 IST`** (~6 months). Rationale in
  [05-operations.md](05-operations.md): `started_at` only needs to cover equity-feature
  warmup (~90 trading days; the universe index warms independently), and the shared
  candle-load window is driven by the earliest `started_at` across all portfolios, so a
  deeper backdate would balloon per-tick cost. 6 months gives warm features + a real
  ~2-month backfilled track record while staying inside the engine's reference cost.
- Deployed by **pre-INSERTing the six rows with the backdated `started_at`** before the
  restart (the YAML UPSERT preserves `started_at` on conflict).
- The universe regime feed switches on automatically because S505/S525 use
  `mode_regime_source="universe"` (the `_any_universe_source` gate) — so the trader now
  builds/primes the DB universe index once per day. S404-only setups still pay nothing.

## How to evaluate the shadows

Watch `/dashboard` and `pm2 logs paperaglo-trader`:

1. A `universe regime index built` log line (proves S505/S525 priming fired).
2. Six `replay completed` lines for the `*_shadow_*` portfolios with **nonzero trades**
   and sane `final_equity` near their ₹20k/₹100k base. Zero trades = features didn't warm.
3. Tick timing stays under 60s (the widened load window is the thing to watch).

**Compare on EQUITY CURVES, not trade counts.** The `trades` table is inflated for *every*
paper portfolio by the ledger-duplication artifact
([07-known-issues-and-roadmap.md](07-known-issues-and-roadmap.md)), so trade counts are not
trustworthy until that's fixed. Equity curves are engine-derived and trustworthy.

What to look for over ~1–2 weeks:
- **S505 vs live S404** over the overlapping window — the apples-to-apples read on what
  migrating would do (same infra, same data).
- **S455 vs S505 vs S525** — isolates each Round-59 lever (patience → +universe/VIX-pct/
  crash → +drawdown governor).
- Any wild divergence from the Phase-4 parity expectation signals a regime-priming issue,
  not a strategy issue.

## Phase 6 — the flip (future, gated)

Only after the shadow validates: change the live portfolio's `strategy_id` from
`S404_s392_side_only` to `S505_pat_uni_vixpct_crash`. This is a real-money change — it
needs the owner's explicit go-ahead and a deliberate, in-market moment. Do not propose
doing it early. NIFTY + India VIX are already primed in the DB; the universe feed is proven
by the shadows.

## Safety invariants maintained throughout

- Every engine change defaults OFF (`external_positions=None`, Round-59 fields = legacy),
  so the live S404 path is byte-identical at every commit.
- The universe feed is gated by `_any_universe_source`, so it only runs when a
  universe-source portfolio exists.
- Nothing in Phases 1–5 changes what real money trades. Real money stays on S404 until
  Phase 6.
