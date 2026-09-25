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

**Same day, increment 3 — position-basis re-adoption after a split (commit pending).**
Closes the gap increment 2 left: an ADOPTED external holding (`real_external_positions`) whose
snapshot predates a split still carried the **old** per-share basis, and the engine judges it
against the now-back-adjusted candles (₹200-space `high_90d`/`atr14`). A ₹1000 basis vs ₹200
candles makes the ATR stop fire the instant it's adopted and the entry-depth read absurd. Fix:
pure `real_executor.split_adjust_position(first_seen, qty, avg_price, actions)` (same
`cumulative_split_factor` divisor as the candles) applied in `real_trader.external_positions_map`
— a pre-split snapshot of 10 @ ₹1000 becomes 50 @ ₹200. Notional (qty×avg_price) is conserved,
so the engine's adoption cash debit (`v2_engine` :973) is unchanged; only the per-share basis and
share count move into today's space — and the adjusted qty now matches the broker's post-split
holding, so a full exit sells exactly what's held. Native (engine-created) positions needed no
change: the stateless replay already rebuilds their basis from the back-adjusted candles. Tests: 29/29.

**Still pending:** auto-scheduling `--detect` (weekly); Track B (port Round 62 events + `news_data`).

## 2026-09-08 — Cash-flow reconciliation (unrecorded deposit/withdrawal guard)

Owner deposited ₹10,000 and it showed up as **phantom P&L** instead of capital. Root cause is
by design: since the net-value SIP auto-detector was disabled (it booked market rallies as
phantom deposits), `invested` only rises when a `real_deposits` row is hand-entered — so a real
cash top-up raises broker free cash and `net_worth` but not `invested`, and the gap becomes P&L.
Nothing watched that gap. Two-part fix, both **detect + alert, never auto-book** (the same
alert-first stance as the corporate-action guard):

**(1) Reconciliation alert.** Pure `real_executor.cash_flow_residual(prev_cash, now_cash,
filled_trades)` = Δfree-cash − Σ(+sell, −buy). The key robustness upgrade over the old detector:
it watches **free cash netted against trades**, not net account value — a market rally never
touches free cash, so it drops out entirely (the confound that made a holdings rally look like a
deposit). `reconcile_kind(residual, threshold)` → 'deposit'/'withdrawal'/None.
`real_trader.emit_cash_reconcile_alerts()` runs always-on in `tick()` (after
`emit_suspension_alerts`): it compares the two most recent **settled** `real_funds` snapshots
(`as_of <= now() − 3min`, so the manual-trade tagger has caught up → no false "withdrawal?" the
instant a manual buy debits cash before its row is tagged), nets `real_orders`
(`avg_fill_price×filled_qty`) + `manual_trades` (`avg_price×qty`) in the window, and WhatsApps
`format_cash_reconcile` when |residual| ≥ `RECONCILE_ALERT_THRESHOLD` (default ₹1,000, above
brokerage/slippage/dividend noise). Deduped on `real_signals` key `reconcile:<snapshot id>` →
exactly one alert per jump (the delta is a one-tick spike; later snapshots are delta≈0). Doesn't
retroactively alert an already-past jump.

**(2) Record UI.** POST `/api/bot/deposit` (`require_admin`) writes `real_deposits`; a withdrawal
is a **negative amount** (`invested = opening + Σ amount`), optional back-date. The `/bot` deposits
card got an inline Deposit/Withdrawal + amount + date + Record form (vanilla `fetch`), replacing
the stale "SIP auto-detected ≥ ₹500" copy. This closes the friction (hand-written SQL) that let
the deposit go unrecorded in the first place. Tests: 35/35. No new SQL; threshold defaults in code.

**(3) One-tap confirmation queue (same day).** Owner asked "why can't the bot do it automatically?"
Checked the Angel SmartAPI: its funds endpoint (`getRMS`) returns an aggregate balance, and there
is **no ledger / transactions endpoint** anywhere in SmartAPI — the labelled ledger (deposit vs
dividend vs charge) exists only in the web/app, not the API. So the bot can see *that* cash moved,
never *what kind* — full auto-booking is impossible without guessing deposit-vs-dividend, the exact
thing that corrupted the numbers before. Built the middle ground instead: detection now queues a
**pending confirmation** (`sql/015 cash_reconcile`, dedup `UNIQUE(snapshot_id)`, replacing the old
`real_signals` reconcile dedup) carrying the signed residual + direction; `/bot` shows an amber card
with one-tap **✓ deposit/withdrawal** / **✕ not capital** buttons (`require_admin`), and the WhatsApp
alert deep-links there (`DASHBOARD_URL`, optional). Confirm books a `real_deposits` row via
`POST /api/bot/reconcile/{id}/resolve`; the pending→confirmed flip is atomic so a double-tap can't
double-book; dismiss records a dividend/other with no cost-basis change. Deliberately NOT a WhatsApp
inbound-webhook / native-button flow: that would be a public endpoint booking real money, so the
tap stays behind the dashboard's admin login. Tests: 35/35. Deploy: run `sql/015` **before** the
`pm2 restart` (the /bot page reads the table).

## 2026-09-25 — false-alarm fixes + shadow buy-points

Owner reported three false alarms from a stretch with no cash (during which they bought SUZLON by
hand), plus asked for a system that shows what the strategy WANTS to buy even when the account is
empty.

**Fix — suspension alert spam.** A by-hand SUZLON buy is outside the bot's universe, so it has zero
candles; `symbol_lag_days` returns its "never priced" sentinel (`10**6`) and the alert printed
"hasn't priced in ~1000000 day(s)" — every day, because the dedup key reset daily. Root cause: the
suspension guard is for universe names that *stop* pricing, not for holdings that were never tracked.
Fixes in `emit_suspension_alerts`: (a) **skip when `symbol_latest is None`** — an untracked holding
is not a suspension; (b) dedup per **ISO week** (`suspend:<sym>:<%G-W%V>`), not per day, so a
persistent lag alerts once a week, not every morning; (c) bumped `SUSPEND_STALE_DAYS` default **3→5**
to clear transient multi-day per-symbol data gaps (AUROPHARMA/HCC/etc.).

**Fix — reconcile false withdrawal/deposit on a manual buy.** The SUZLON buy debited cash; the
reconciliation saw the drop *before* the manual-trade tagger caught the fill → false "₹6,645
withdrawal", then when the fill tagged in a later window it double-counted → false "₹6,637 deposit".
Fix in `emit_cash_reconcile_alerts`: a **guard band** (`RECONCILE_GUARD`, ±30 min) — if any bot fill
or manual trade sits near the cash-move window, treat the move as trade-related and stay silent. Real
deposits land on quiet days; the /bot record form is the backstop for a deposit made mid-trade.

**Feature — shadow buy-points.** Each tick, after the real replay, a **second, non-persisting**
replay runs the live portfolio with unlimited cash so the strategy emits every entry it *wants*, even
cash-gated. New recent BUYs are logged to `shadow_buy_points` (`sql/016`), and `/bot` joins them
against live candles to show, per name: wanted @ ₹P, price now, and whether it's still catchable at
≤ P. Key mechanics: `replay_one_portfolio(..., persist=False)` (new flag — the fantasy trade list
must never overwrite the live portfolio's trades/positions/equity); `cash_override` set huge for
every trading day; and, crucially, the shadow strategy is forced to **fixed-rupee sizing**
(`allocation_mode="fixed"`, `SHADOW_ALLOC=₹10L`) because under native `pct_equity` the huge cash
inflates equity → inflates position size → a heavy day could still exhaust cash and drop entries.
Candidate selection is upstream of sizing, so fixed sizing changes only funding, not which names fire.
Purely informational — never places an order; throttled to once per 5 min; fully guarded so a shadow
bug can't break real trading; default on (`SHADOW_BUY_POINTS`), lookback `SHADOW_LOOKBACK_DAYS=45`.
Tests: 41/41. Deploy: run `sql/016` **before** the `pm2 restart` (the /bot page reads the table).

## Prior context (before this log's window)

Predating the above, the live real-money bot was built on the paper rig: real order
placement verified 2026-06-17; broker-authoritative reconciliation
(`external_positions`/`cash_override`, sql/009) added 2026-06-23; AB4036 quarantine
(sql/010) added 2026-06-24. The hard operational lessons (NSE-not-BSE, paise tick sizes,
token-per-exchange PK, IP whitelist, daily re-auth) were paid for in real rejections. See
[03-live-money-bot.md](03-live-money-bot.md).
