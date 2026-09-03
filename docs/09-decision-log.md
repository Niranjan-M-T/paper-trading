# 09 — Decision log

A narrative of what was built and decided, newest last. This is the "chat history"
distilled into durable form — the reasoning behind choices you'll see in the code. Dates
are approximate where a session spanned several days.

## ~2026-07-01 — yfinance validated as a data source

Angel's historical API couldn't keep up (73 symbols × ~1.25s pacing → 8–17 min cycles,
~95 rate-limit hits/session, dropped bars). Investigated alternatives. **Decision:**
yfinance (`SYMBOL.NS`, 5m) is a faithful drop-in — volume matches Angel to a handful of
shares (critical, since S404 gates on a volume spike), price to ~0, latest bar ~1–2 min
old, works from the VPS IP. Rejected openchart (0 rows) and nsepython (daily-only,
NSE-blocked). Chose a **hybrid**: bulk-poll from yfinance, confirm real entry bars against
Angel before spending money. Shipped behind `DATA_SOURCE`, default `angel`.

## ~2026-07-06 — yfinance hybrid live

`DATA_SOURCE=yfinance` on the VPS cut poll cycles from 8–17 min to ~4–6s, zero rate limits.

## 2026-07 — Angel fallbacks + log-noise fix

Requested: "if yfinance isn't working for a symbol, fall back to Angel." Built two
mechanisms in `poller._poll_once_yf`: **whole-cycle failover** (batch coverage <
`YF_FAILOVER_MIN_COVERAGE` → run the cycle via Angel) and **per-symbol top-up** (symbols
with ZERO yfinance bars pulled from Angel, capped by `YF_TOPUP_MAX_SYMBOLS`). Key insight:
a zero-bar symbol is a fetch failure worth topping up; a partial gap is usually a real
no-trade window where Angel has no bar either — don't top those up. Proven live by
force-failing RELIANCE/TCS (Angel recovered ~75 bars each). Separately, root-caused ~98
debug-bundle "errors" as yfinance's own logger emitting "possibly delisted" for batch
misses; silenced it (`yf_provider` → logger CRITICAL). Added `tools/probe_yf_fallback.py`
and `tests/test_poller.py`.

## 2026-07-09 — S505 engine port begins

Owner: "let's start the engine port." Goal: migrate live money from S404 to S505 (Round-59
champion). Discovered the vendored engine already had the S283/S447 multi-mode chassis,
patience-sell, adaptive depth exits, and NIFTY+VIX regime priming from the DB — the only
gap was the **Round-59 levers**. Chose a phased, parity-gated approach.

- **Phase 1** — ported six fields onto `StrategyV2`
  (`mode_regime_source`, `mode_hysteresis_days`, `mode_crash_overlay_pct`,
  `mode_vix_percentile`, `dd_governor_threshold`, `dd_governor_scale`), extended
  `classify_regime_by_date`, added the drawdown governor. All default to legacy → S404
  byte-identical. The schema drift-guard caught the incomplete port until every field was
  registered in `schema.py` (a feature, not a bug).
- **Universe feed sourcing decision** — the DB has 1282 days of 5m history (back to
  2021-05), so the universe index can be built purely from the DB (no CSV seed). Validated
  the DB-built index vs the research repo's `data/UNIVERSE_INDEX_extended.csv` at ~95%
  label agreement (cold-start artifact; warmed years near-identical). Owner chose "compute
  from DB + validate."
- **Phase 2** — `src/engine/universe_index.py` (equal-weight index + breadth), day-cached
  priming wired into the traders behind the `_any_universe_source` gate.
- **Phase 3** — vendored + registered S455/S505/S525 from a shared `_r59_base.py` chassis
  (S447). Field-by-field diff vs the research repo = zero differences.

## 2026-07 — Phase 4: parity harness, ALL PASS

`tools/parity_s505.py` cross-imports both engines, feeds identical prices + regime series,
runs S455/S505/S525 on each, diffs. **Result: ALL PASS** — trade-for-trade and
equity-to-the-rupee over 2022–2025 (bull/bear/crash/patience/dd-governor). This is the
definitive proof the vendored engine reproduces the research engine for these strategies.

## 2026-07-13 — Phase 5: shadows deployed

Owner chose "paper portfolio, watch ~1–2 weeks." Added six paper shadows to
`config/portfolios.yaml` (S455/S505/S525 × ₹20k + ₹100k, all `live=FALSE`).

**Backdating decision (important):** an earlier note suggested backdating `started_at` to
2024-01-01. Reading the load path changed that — `trader.tick` loads candles from the
*earliest* `started_at` across ALL portfolios and re-replays each slice every tick, so a
2.5-year backdate would mean a ~3.4M-row load + three multi-year replays every 60s
(blowing the tick budget). And it's unnecessary: the universe index warms independently
(always 1100-day lookback), so `started_at` only needs to cover equity-feature warmup
(~90 trading days). **Chose `2026-01-01 09:15 IST` (~6 months)** — warm features now, a real
~2-month backfilled track record, per-tick cost at the engine's reference scale. Deployed
by pre-INSERTing the rows with the backdated `started_at` before the restart (the YAML
UPSERT preserves `started_at` on conflict → one restart). Committed 44ec457 and pushed on
the owner's explicit "commit and push." Owner ran the VPS steps; sync confirmed
(`yaml_count: 24`), but the restart landed after the 15:30 IST close so the shadows first
replayed at the next open.

## 2026-07-14+ — shadows trading; ledger-duplication artifact found

First shadow trades looked right (e.g. `S455_shadow_100k` selling DYCL at `target_+32%`,
the correct `SIDE_4TIER_22` deepest bucket). But a paste showed `S50_...` with ~28 DRREDDY
BUY rows. **Diagnosed as a ledger display artifact, not over-trading:** scan-fires-early +
per-tick replay over a churning forming bar + `price` in the dedupe key = many rows for one
intended trade. The engine can't over-buy (`if symbol in holdings: continue`), and nothing
reads `trades` back for state, so equity is correct. The right guard
(`scan_time_elapsed`) exists but is wired only into real order placement, not the ledger
write. Fix proposed (prevention filter in `replay_one_portfolio` + a cleanup script), not
yet applied. Consequence for the migration: compare shadows on **equity curves, not trade
counts**. See [07-known-issues-and-roadmap.md](07-known-issues-and-roadmap.md).

## 2026-07-16 — DPWIRES noise + this documentation

A debug bundle showed ~87 DPWIRES.NS "possibly delisted" ERRORs despite the silencing
commit — noise (Angel top-up covers DPWIRES), flagged for follow-up. Owner then requested
this documentation pack so another Claude/IDE could pick up the project cold. Wrote the
root `CLAUDE.md` + `docs/` set, grounding every claim in the tree as it stood.

## 2026-07-16 — WhatsApp signal feed, live stats, "Unmanaged" fix

Owner wanted the live bot to forward its BUY/SELL signals to a WhatsApp group so they
can be actioned by hand — specifically to buy AB4036 surveillance stocks the bot can't
place itself. Decisions: source = the live real-money bot (S404 now → S505 after Phase
6; the group is *named* s525 but receives the live strategy's signals, and only while
the bot is ON); scope = every buy & sell, flagging the unplaceable ones; AB4036 = keep
the quarantine's auto-order skip (no reject spam) but ALWAYS send the signal. Found the
group JID live via `GET /group/fetchAllGroups` (`120363411936940548@g.us`, "Stonks S525
trader signals") and seeded it; the target list is editable from /bot (add/remove/toggle,
live group picker, send-test). Built `src/core/whatsapp.py` (defensive Evolution-API
client), `wa_targets` + `real_signals` (sql/012; sends deduped on the price-free logical
key so a churning bar can't spam), and wired `emit_signals` into `real_trader.tick`. Also
fixed the "Unmanaged" manual buy: surveillance scrips trade `-BE`/`-BZ`, so the raw broker
symbol didn't reverse-map — added `real_executor.engine_symbol_root()` suffix-stripping so
adoption catches them. And added a live-account Performance card (`/api/bot/stats`:
realized+unrealized P&L, %, days running, APY) plus a days-running counter on every
dashboard card. All config-gated (WhatsApp default OFF); real S404 path unchanged.

## 2026-08-25 — Real-orders WhatsApp feed, Option B, fixed cost basis

Follow-ups after the feed went live. **(1) Feed source corrected.** The WhatsApp alerts
were coming from the strategy *replay* of a paper shadow (`S404_live_sip_20k`), not the
live bot's real actions. Rewired: `emit_signals` (replay-based) → `emit_order_signals(pf)`
sourced from `real_orders` — "🟢 placed" on `open`, "⚠️ REJECTED … buy it manually" on
`error`/`rejected`, deduped `order:<id>:<status>`. **(2) Option B (owner's choice).** Since
a benched AB4036 symbol produces no more rejections (so the real-orders feed would go silent
after the first), added `emit_quarantine_signals(pf, skips)`: `place_new_orders` now returns
the BUYs it skipped as quarantined, and each distinct signal (dedup `skip:<intent_key>`)
fires a "🚫 skipped — buy it manually" nudge. Net: one REJECTED alert + bench on first hit,
then an ongoing nudge each time the strategy re-signals it — no doomed-order spam. **(3)
Cost basis fixed.** Owner couldn't pull an Angel P&L report, so `invested` is now the fixed
`REAL_OPENING_CAPITAL` (₹18,000) + hand-entered `real_deposits`, replacing the seeded
`capital` + auto-detected deposits. The net-value SIP detector (which booked holdings
rallies / MF inflows as phantom deposits) is **OFF by default** (`DEPOSIT_AUTODETECT`),
gated in `sync_funds`. Engine `capital` is left untouched (changing it would perturb the
stateless replay's sizing and could fire unexpected orders). **(4)** Also shipped the
live manual-trade tagger (`manual_trades`, sql/013 — tags account order-book fills the bot
didn't place) and the `-BE`/`-BZ` adoption fix so INOXGREEN-style surveillance holdings get
managed. Open item: reconstruct the true opening from `real_orders` (bot-only; blind to
pre-tagger manual trades and cash moves) as a sanity check on the ₹18k assumption.

## 2026-09-03 — Corporate-action handling (Track A, increment 1)

Owner flagged that the platform doesn't handle splits / M&A / delisting, and asked whether
the algo's "strategy creator" already covers it. Findings (grounded in the algo tree):
- **Splits/bonuses** are handled in the algo *backtest* at the data layer (`run_scenarios.py`
  `auto_adjust=True`) + a ±50% daily-return clip on the universe index. The live/paper
  platform stores **raw** prices (`yf_provider auto_adjust=False`, drops the Splits/Dividends
  columns) → a split corrupts `volume_avg20` / 90d-high / ATR and a held name's exit basis.
- **Corporate EVENTS** (auditor resignation, insolvency, fraud, SEBI orders, suspension/
  delisting, results blackout) — the algo's **Round 62 "TIER-1 corporate-event features"**
  (`news_data.py` NSE scraper + `engine_v2` event gates) builds strategies **S560–S572** on
  S505/S525. But it's experimental (veto effect "near-zero" until 150 symbols), needs
  `event_features=`, and the live champion is still S404. **Paper-trading has none of it**
  (grep: `event_features`/`news_data`/`classify_event` = 0 hits — ported only through R59).
- **M&A share-conversion** is not handled anywhere.

So the fix splits into **Track A** (price/position integrity — independent) and **Track B**
(port Round 62 event overlay — follows the S505→S525 migration). Shipped Track-A increment 1:
a **held-position staleness alert** — `real_executor.symbol_lag_days` (pure, universe-relative
so outages/weekends never false-trigger) + `real_trader.emit_suspension_alerts` (always-on,
alert-only, deduped per symbol/day in `real_signals`, `SUSPEND_STALE_DAYS` default 3) →
`whatsapp.format_suspension_alert` "you HOLD N×SYM, hasn't priced in ~Kd, handle it manually".
Chose alert-first (zero false-positive trade suppression) over auto-benching. Tests: 19/19.

**Same day, increment 2 — split/bonus back-adjustment (commit pending).** Fixes the false
entries/exits a split causes. `corporate_actions` table (sql/014); a **read-time,
non-destructive** adjustment in `engine/corporate_actions.py` (`adjust_frame` +
`load_active_actions`, pure core `real_executor.cumulative_split_factor`) wired into
`replay.load_candles_window` — divides a bar's price by the compounded ratio of every split
ex-dated after it (volume ×). Chose read-time over mutating stored candles: reversible
(`active` flag), no idempotency/late-backfill hazard, orders still place at the raw current
price, and it's a zero-cost no-op until a split is recorded. `tools/corporate_actions.py`
(`--detect` via yfinance `.splits`, `--add`, `--list`, `--disable`). Mirrors the backtest's
`auto_adjust=True`. Also fixed the **benched-skip WhatsApp spam** (commit 60436e5): the
Option-B nudge deduped on the full `intent_key` (carries live price/time) → re-sent every 60s
tick (owner's INOXGREEN screenshot); now dedups on the price/time-free logical key. Tests: 25/25.

**Still pending:** position-basis re-adoption after a split (`trades`-derived `avg_price`
stays pre-split); auto-scheduling `--detect`; Track B (port Round 62 events).

## Prior context (before this log's window)

Predating the above, the live real-money bot was built on the paper rig: real order
placement verified 2026-06-17; broker-authoritative reconciliation
(`external_positions`/`cash_override`, sql/009) added 2026-06-23; AB4036 quarantine
(sql/010) added 2026-06-24. The hard operational lessons (NSE-not-BSE, paise tick sizes,
token-per-exchange PK, IP whitelist, daily re-auth) were paid for in real rejections. See
[03-live-money-bot.md](03-live-money-bot.md).
