# 04 — Data sources & the poller

## The problem yfinance solved

Angel's historical API rate-limits hard. Polling ~73 symbols every cycle on ONE account
can't finish: 73 × ~1.25s pacing = a ~91s floor before any network time, so cycles blew
out to 8–17 minutes, threw ~95 "Too many requests" per session, and Angel even dropped
1–2 bars/day. That's fatal for a rig that needs a fresh forming bar every minute.

## The decision: yfinance hybrid (LIVE since ~2026-07-06)

**Bulk-poll 5m bars from yfinance** (`SYMBOL.NS`) to kill the rate-limit storm, but
**confirm the actual entry bars against Angel** (authoritative) before spending real
money. Shipped behind `DATA_SOURCE`, default `angel`; the VPS runs `yfinance`.

Why yfinance was safe to adopt — validated 2026-07-01:
- **Volume** matches Angel to a handful of shares (ratio ~1.00 at median/p10/p90). This
  mattered most: S404 gates entries on a volume spike
  (`scan_volume/scan_volume_avg20 >= 1.1`), so volume fidelity was the risk. Cleared.
- **Price** matches to ~0; the latest bar is ~1–2 min old; works from the VPS IP.
- Rejected alternatives: **openchart** returns 0 rows; **nsepython** is daily-only and
  NSE-blocks the VPS.

Result in production: `DATA_SOURCE=yfinance` cut poll cycles from 8–17 min to ~4–6s with
zero rate limits (a debug bundle shows `6s/cycle · src=yfinance`).

## Angel fallbacks (in `poller._poll_once_yf`)

yfinance is primary, Angel is the safety net — two independent mechanisms:

1. **Whole-cycle failover** — if a batch yfinance download covers less than
   `YF_FAILOVER_MIN_COVERAGE` (default 0.5) of the universe, treat Yahoo as down for that
   cycle and run the entire poll via Angel.
2. **Per-symbol top-up** — for symbols yfinance returns **zero** bars for (a genuine
   fetch/resolve failure), pull just those from Angel — capped at `YF_TOPUP_MAX_SYMBOLS`
   (default 20) so a partial Yahoo outage can't re-create the 73-call storm.

Key distinction, encoded in `poller.py`'s helpers (`_covered_symbols`, `_coverage`,
`_symbols_missing`): a **zero-bar** symbol is a fetch failure worth topping up; a
**partial gap** (some bars, then nothing) is usually a real no-trade window where Angel
has no bar either — those are NOT topped up. Proven live: force-failing RELIANCE/TCS made
Angel recover ~75 bars each.

Read-only probes: `tools/probe_yf_fallback.py` (with `--force-fail SYM,SYM` and
`--angel`), `tools/probe_data_sources.py`, `tools/compare_sources.py`,
`tools/compare_db_vs_yf.py`.

## Log-noise handling

`src/core/yf_provider.py` sets the `yfinance` logger to `CRITICAL`. yfinance emits its own
`ERROR`-level "possibly delisted; no price data found" lines for batch misses (e.g. a
symbol with no bar yet at the 09:15 open before Yahoo publishes). Those were ~100% of a
debug bundle's "errors" — pure noise, since the fallbacks handle the data. Silencing the
logger keeps bundles readable.

> **Known live discrepancy (2026-07-16):** a debug bundle still showed ~87 DPWIRES.NS
> "possibly delisted" ERROR lines despite the silencing commit — either the deployed
> poller predates that commit or yfinance emits these outside the muted logger. It's noise
> (DPWIRES is a known-gapped symbol the Angel top-up covers), not data loss, but worth
> confirming. See [07-known-issues-and-roadmap.md](07-known-issues-and-roadmap.md).

## Dual Angel account (optional)

`config.py` supports a **second Angel account** (`ANGEL2_*`). When fully configured,
market-data fetching (poller/backfill) moves to account 2 by default
(`ANGEL_DATA_ACCOUNT=auto`), leaving account 1 dedicated to real trading
(`ANGEL_TRADING_ACCOUNT=1`). `resolve_angel_account('auto'|'1'|'2', has_account2)` maps
the selector. This separates the data rate-limit budget from the trading session.

## Cadence & the "one interval" rule

- Poller default is `POLLER_INTERVAL_SECONDS=150` (was 60). At ~73 symbols a 60s Angel
  sweep is physically impossible; 150s still refreshes a 5-min forming bar 2–3× before it
  finalises. With yfinance the batch download is seconds, so cadence is comfortable.
- **Everything speaks 5-minute bars for equities** — the engine was tuned on 5m, CSVs are
  5m, the poller writes 5m, the trader reads 5m. The nightly backfill also keeps `1m`
  (free side effect, for finer charts) and `1d` for indices, but the engine never reads
  those. Don't wire 1m into the engine without re-tuning every strategy. (Root README has
  the full "one source of truth" table.)

## Daily verification

`tools/verify_data_source.py` runs at 16:05 IST (cron), compares the day's data against
Angel, writes a verdict to `data_source_audit` (sql/011), and surfaces it on the `/bot`
verify card. A healthy verdict looks like "OK, small number of gaps, median bars ≈ full
session."
