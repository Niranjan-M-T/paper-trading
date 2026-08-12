# 02 — Strategies

## Rule zero (repeat of golden rule 1)

**Never describe or reason about a strategy's behavior from memory.** The
authoritative definitions live in the research repo `i-want-to-build-an-algo` —
read its `strategies_v2.py` and `engine_v2.py` before you answer. The files in
`src/strategies/` here are *vendored ports*, kept byte-for-byte identical and proven
so by `tools/parity_s505.py`. If the research repo is not available on the machine
you're on, say so rather than guessing.

## How a strategy is defined

One file per strategy in `src/strategies/`, exporting a module-level constant:

```python
from src.engine.v2_engine import StrategyV2
STRATEGY = StrategyV2(name="S99_my_idea", fall_threshold=-0.06, ...)
DESCRIPTION = "..."   # optional
```

`registry.py` auto-discovers every module that exports `STRATEGY`. A file with no
`STRATEGY` (like `_r59_base.py`) is imported and skipped — that's how shared chassis
code lives alongside registered strategies.

`config/portfolios.yaml` references a strategy by `name` and pairs it with a capital.
On `paperaglo-trader` startup, `sync_portfolios_from_yaml()` UPSERTs each row into
`portfolios` (preserving `started_at` on conflict); anything in the DB but absent from
YAML and not `live` is flipped `enabled=FALSE`.

Adding a `StrategyV2` field requires also registering it in `src/strategies/schema.py`
— a drift guard raises `RuntimeError` at import if the schema and dataclass disagree.
This guard is good; it has caught incomplete ports.

## The registered strategies (as of 2026-07)

Simple → complex. Names encode lineage (SNNN = iteration number).

| Name | One-liner |
|---|---|
| `S6_tiered_exit` | Single buy on −5%, tiered exits at +15/30/50%. |
| `S14_concentrated` | Max 1 buy/day on the deepest drop, ₹25k, +35%. |
| `S23_s20_equity8` | % -of-equity sizing, volume-confirmed pyramid. |
| `S29_s23_sensex` | S23 + a SENSEX regime gate. |
| `S31_s24_persist` | Trigger-mode entry + 3-candle persistence filter. |
| `S50_drop3_e15_alloc10_vol11` | −3% drop, +15% exit, 10% alloc, 1.1× volume. |
| `S228_multiregime_bear_pyr_small` | Multi-regime; switches the whole param set on NIFTY bull/bear/sideways; needs INDIA_VIX. |
| `S283_mm_dma_classic` | The multi-mode "adaptive money-management" chassis (50-DMA regimes + VIX fear). |
| `S404_s392_side_only` | **The live real-money strategy today.** S283 chassis + mode-aware *adaptive depth-bucket exits* (`adaptive_exit_by_depth`). |
| `S455_s447_pat250_12` | Round-59: S447 chassis + patience-sell (250 days / 12% min profit). |
| `S505_pat_uni_vixpct_crash` | **The live-migration target.** S455 + Round-59 regime levers (universe regime source, VIX-percentile, crash overlay). |
| `S525_s505_ddgov` | S505 + a drawdown governor (halve allocation when equity is 15% below its high-water mark). |

## The Round-59 family: S447 → S455 → S505 → S525

These four share the `_r59_base.py` chassis and mirror the research repo's
`dataclasses.replace` chain exactly. `S447` itself is **not registered** — it's the
base the others derive from.

### S447 chassis (`S447_BASE` in `_r59_base.py`)

The S283 multi-mode adaptive-exit engine (same family as the live S404), with these
salient values (read the file for the full list — do not trust this table alone for
anything that matters):

- `fall_threshold=-0.030`, `allocation_mode="pct_equity"`, `allocation_pct=0.16`
- `scan_times=("11:00","14:00")`, `macd_filter="positive"`,
  `macd_filter_in_bear_market=True`
- `pyramid_levels=((-0.08,0.06),(-0.16,0.05),(-0.25,0.04))`
- Per-regime exit ladders (`adaptive_exit_by_depth`, keyed by how far below the 90-day
  high the entry was):
  - **bull** → `_BULL_XWIDE` (wide — lets deep bull entries run)
  - **bear** → `_S311_BUCKETS` (also the global default)
  - **sideways** → `_SIDE_4TIER_22` (4 tiers; entries >22% below the 90d high get the
    widest tier — S447's defining change)

### The derivation chain

- **S455** = S447 + `PATIENCE_250_12` = `patience_sell_after_days=250,
  patience_sell_min_profit=0.12`. Keeps the legacy `mode_regime_source="NIFTY_50"`.
- **S505** = S455 + `R59_UNI_VIXPCT_CRASH` = `mode_regime_source="universe",
  mode_crash_overlay_pct=0.08, mode_vix_percentile=0.80`. **No drawdown governor.**
- **S525** = S505 + `dd_governor_threshold=0.15, dd_governor_scale=0.5`.

So each successive strategy isolates one lever, which is exactly why all three run as
paper shadows during validation (see [06-s505-migration.md](06-s505-migration.md)) —
to attribute any behavior difference to a specific lever.

## The Round-59 regime machinery (ported in the S505 project)

The vendored engine already had the S283/S447 multi-mode chassis, patience-sell,
adaptive depth exits, and NIFTY+VIX regime priming. The S505 port added the
**Round-59 levers** onto `StrategyV2` and `classify_regime_by_date`, all defaulting to
legacy behavior so S404 stays byte-identical:

| Field | Meaning |
|---|---|
| `mode_regime_source` | `"NIFTY_50"` (default) \| `"universe"` \| `"breadth"`. Which series drives bull/bear/sideways. |
| `mode_hysteresis_days` | Require N consecutive days before switching regime (anti-whipsaw). |
| `mode_crash_overlay_pct` | Force bear when close < 60-day-high × (1 − pct). |
| `mode_vix_percentile` | Use a rolling-252d VIX percentile (not a fixed threshold) for the fear override. |
| `dd_governor_threshold` / `dd_governor_scale` | When equity < HWM × (1 − threshold), multiply new-entry allocation by `scale`. |

The **universe index** (`src/engine/universe_index.py`) is an equal-weight,
composition-neutral index built from the DB's 5m equity candles resampled to daily:
mean daily return clipped ±50%, cumulated from 1000; **breadth** = fraction of symbols
above their own 50-DMA. It's built once per trading day (1100-day lookback), and only
when a live/paper portfolio actually uses a universe/breadth regime source
(`_any_universe_source` gate). Validated against the research repo's
`data/UNIVERSE_INDEX_extended.csv` at ~95% label agreement (cold-start artifact; warmed
years near-identical).

## Parity — the guarantee

`tools/parity_s505.py` cross-imports both engines (research + vendored), feeds them
identical 5m prices and identical regime series, runs S455/S505/S525 on each, and diffs
trade lists and equity. Result at port time: **ALL PASS** — trade-for-trade and
equity-to-the-rupee over 2022–2025 (covering bull, bear, crash, patience, and the
drawdown governor). Config parity is separately locked by `tests/test_r59_strategies.py`.

Run `python -m pytest tests/ -q` after any engine or strategy change. Re-run
`tools/parity_s505.py` (needs the research repo; `PARITY_ALGO_DIR` env overrides its
path) after any engine change that could affect the Round-59 strategies.
