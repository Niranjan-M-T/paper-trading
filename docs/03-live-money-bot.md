# 03 — The live real-money bot

**This is the part that spends real money.** One Angel One account, ₹20,000, running
`S404_s392_side_only`, forward-only with SIP top-ups, managed from `/bot`, driven by
`paperaglo-real-trader`. As of 2026-06-17 it can place real orders end to end
(verified: a test order was ACCEPTED, went open, and was cancelled in the real account).

Everything here is layered on top of the paper rig. The real trader reuses the same
engine and the same `replay.py` glue; the differences are the reconciliation seams and
the order-placement path.

## The master switch

`/bot` has one ON/OFF toggle: `real_bot_state.enabled`. It **defaults OFF on deploy** —
pushing code never starts live trading. When OFF, the real trader still replays and
tracks state; it just doesn't place orders. Turning it ON is a deliberate, in-market act
the owner performs.

## The core problem: stateless replay vs. broker truth

The engine is a stateless replay (see root CLAUDE.md). It assumes every signal filled
perfectly at the engine's price. Reality doesn't:

- orders get **rejected** (surveillance, insufficient margin, bad tick),
- fills are **partial**,
- the same logical order can **duplicate**,
- the owner may **manually buy** something the engine never chose.

So `positions` ("what the engine thinks") drifts from `real_holdings` (broker truth) on
every reject/partial/duplicate/manual trade. The fix (built 2026-06-23): **the broker is
the single source of truth**, and the engine is *reconciled* to it.

## Broker-authoritative reconciliation

Three seams on `run_backtest_v2`, all date-keyed, all defaulting to `None` so every
backtest/paper path is byte-identical:

1. **`external_positions={date: {symbol: {qty, avg_price}}}`** — inject broker positions
   the engine didn't create (manual buys, orphaned fills) into `holdings` so the S404
   exit ladder *manages* them. `entry_depth_pct`/`entry_atr` are snapshotted from that
   day's features so the adaptive ladder picks the right bucket; cash is debited to
   conserve equity. The engine already refuses to re-buy a held symbol
   (`if symbol in holdings: continue`), so an injected position is managed (exited),
   never re-entered. The engine also now returns `holdings_state` (the full dict) so
   adopted positions persist to `positions` — they have no BUY trade, so the old
   trade-replay reconstruction dropped them.

2. **`cash_override={date: cash}`** — SET the engine's simulated cash to the broker's
   real free cash on that day (after deposit/adoption injections — final word), so entry
   sizing (`pct_equity` alloc + `min_entry_cash` gate) reflects the real account and
   absorbs manual sells/withdrawals the stateless replay can't see. The real trader
   passes `{today: funds.available_cash}`.

3. **`deposits={date: amount}`** — the SIP variable-deposit map. First deposit is
   `starting_cash`; later ones are injected mid-run. Detection is net-value-vs-baseline
   (capital + prior deposits), not raw cash deltas, so initial funding isn't miscounted
   as a phantom deposit.

Detection/persistence lives in `real_trader.py`: it diffs `real_holdings` against the
engine's `open_positions`, writes unmanaged holdings to `real_external_positions`
(sql/009), builds the map, and passes it through `replay_one_portfolio`.

## Broker-bound SELLs (`real_executor` / `real_trader.place_new_orders`)

SELLs are reconciled against actual broker quantity so the bot never phantom-sells or
strands shares:

- **Phantom guard** — broker holds 0 of the symbol → skip the SELL entirely. This killed
  the AB4036 PARACABLES/PRECWIRE/UNIVCABLES rejection loop (the engine "sold" positions
  the broker never actually held).
- **Orphan sweep on full close** — engine fully closed the symbol but the broker still
  holds extra (e.g. duplicate-fill orphans: broker 6 vs engine 3) → sell the full broker
  qty.
- **Clamp** — otherwise sell `min(engine_qty, broker_qty)` so partial tiers never
  over-sell.
- BUYs are unchanged (already cash-gated and logically deduped).

The unified `/bot` holdings view (`/api/bot/holdings` + `bot.html`) shows ONE reconciled
table with a **managed-by badge**: `engine` / `adopted` / `orphan`.

## AB4036 quarantine + surveillance handling

AB4036 (exchange surveillance / cautionary listing) is a **hard broker block** — no buy
override exists. The phantom guard already stops the doomed *sells*; the quarantine stops
the doomed *buys*:

- `real_quarantine` table (sql/010). On a BUY rejection whose bracketed code is in
  `_SURVEILLANCE_CODES` (`{"AB4036"}`), `quarantine_symbol(sym, code, err, months=3)`
  benches it. `active_quarantine_symbols()` deletes expired rows and returns the active
  set, passed into `place_new_orders(..., quarantined=)` which skips BUYs for benched
  symbols (SELLs unaffected, so a real held position can still exit). Auto-clears after
  3 months in case the scrip leaves surveillance.
- `real_executor.surveillance_reject_code(err)` extracts a bracketed `[CODE]` but returns
  it **only** for permanent per-symbol blocks. Transient infra errors (AG7002 IP
  whitelist, rate limits, expired session) return `None` → keep retrying, don't bench a
  tradeable symbol.
- `/api/bot/quarantine` + a hidden-until-nonempty `bot.html` card show benched symbols.

## The scan-time gate (why real orders wait)

The engine reads a scan as `time <= scan_time`, so on the *current* day a "11:00 scan"
evaluates against whatever bar is latest right now — a still-forming bar whose close
churns every poll. `real_executor.scan_time_elapsed(reason, now_hhmm)` gates real order
placement: a scan-mode entry is only placed once its scan **bar** is complete
(scan_time + one bar interval, e.g. 11:05 for the 11:00 scan). Non-scan actions (pyramid
adds, tiered exits, stops) act on the current bar by design and are always allowed. In a
debug bundle this shows up as `N stale skipped`.

> This gate protects **real money** but is **not** applied to the ledger write, which is
> the root of the ledger-duplication artifact. See
> [07-known-issues-and-roadmap.md](07-known-issues-and-roadmap.md).

## The hybrid-data money guard

Because live candles come from yfinance (see [04](04-data-sources.md)), before a real
BUY the engine's entry bar (yfinance) must agree with Angel's authoritative bar:
`real_executor.entry_bars_agree(...)` confirms close within 0.5% and, when both volumes
are known, volume within 20% (volume is the S404 entry gate). A missing Angel bar → do
NOT confirm (fail safe).

## Hard-won operational gotchas (not obvious from code)

These each cost a real rejection or a silent data gap to learn. Respect them.

- **The account trades NSE, not BSE.** Symbols added via dashboard search defaulted to
  BSE → real orders rejected. `tools/migrate_universe_to_nse.py --all --backfill`
  re-points them to NSE.
- **`instruments.token` is unique only per exchange segment**, not globally. The PK was
  `(token)` → refresh silently overwrote NSE equities with colliding derivative rows.
  Fixed to `(token, exchange)` (sql/008); joins must match on token AND exchange.
- **`instruments.tick_size` is in PAISE** → rupees = value/100 (tick 10 → ₹0.10, 5 →
  ₹0.05, 1 → ₹0.01). Orders must snap the limit price to the scrip's real tick or the
  exchange rejects with a generic "5 paise" message.
- **Order placement requires the VPS IP whitelisted** on the Angel SmartAPI app whose key
  is in `.env` (error AG7002 = IP not registered). Use `placeOrderFullResponse` to see
  real errors; plain `placeOrder` returns `None` and hides the reason.
- **Runners must re-auth daily** — the Angel JWT expires at midnight IST. The poller once
  silently wrote 0 candles for a day before this was fixed.
- **Deploy order matters for migrations** — apply the SQL (`sql/009`, `sql/010`, …)
  BEFORE restarting the runners. Until the table exists, the real-trader tick errors and
  `/bot` holdings 500s.

## WhatsApp signal fan-out (Evolution API)

The live bot forwards **every** ready BUY/SELL signal to one or more WhatsApp groups so
they can be actioned by hand — the point is to catch what the bot can't place itself
(AB4036 surveillance stocks). `src/core/whatsapp.py` is a defensive Evolution-API client
(`send_text`/`broadcast`/`fetch_groups`/`format_signal`); a gateway outage logs and
returns falsy, never breaking the tick. Wired via `real_trader.emit_signals()` after
`place_new_orders`, so it fires only when the bot is **ON**.

- **Config** (`.env`, default OFF): `WA_ENABLED`, `WA_GATEWAY_URL`
  (`https://wa.hosting.studiohappens.tech`), `WA_API_KEY` (controls the whole gateway —
  never hard-code it), `WA_INSTANCE` (`taskflow`). The gateway sends to a **group JID**
  (`…@g.us`), not a name.
- **Targets** are the `wa_targets` table (sql/012), editable from /bot (add/remove/toggle,
  a live group picker via `GET /group/fetchAllGroups`, and a send-test). Seeded with
  "Stonks S525 trader signals" = `120363411936940548@g.us`.
- **Dedup**: `real_signals` (sql/012) keyed on the **price-free** logical key
  (`date|symbol|side|reason`) so a churning forming bar — or a quarantined-every-tick BUY
  — can't re-notify; unsent rows retry next tick.
- **Scope/behavior** (owner's decisions): source = the live real-money portfolio (S404
  now → S505 after Phase 6; note the group is *named* s525 but receives the live
  strategy's signals); **every** buy & sell is sent, with the ones the bot can't place
  (quarantine / no cash / phantom sell) flagged "act manually". The AB4036 **quarantine is
  unchanged** — it still skips the doomed auto-order (no reject spam); "always signal" is
  satisfied by the feed.

## "Unmanaged" manual buys (fixed 2026-07-16)

A hand-bought position showing `managed_by = orphan` ("Unmanaged") means the engine can't
adopt it. Two causes: (1) adoption only runs when the bot is **ON** (the tick returns at
the master switch before `reconcile_external_positions`); (2) the holding didn't
reverse-map to a universe symbol. Surveillance/T2T scrips trade in the `-BE`/`-BZ` series,
so a hand-bought AB4036 holding was `XYZ-BE` while the universe maps `XYZ`/`XYZ-EQ` — no
match. Fixed with `real_executor.engine_symbol_root()` (strips `-(EQ|BE|BZ|SM|ST|IL|T)$`);
`broker_holdings_by_engine` now falls back to the stripped root when it's a known universe
symbol. A holding whose root isn't in the universe still can't be managed (no candles) —
add it to the universe first.

## Live-account performance (/bot)

`/api/bot/stats` + the Performance card show total P&L split into **realized + unrealized**,
% return on capital deployed, **days running**, and an extrapolated APY. Math in
`metrics.split_pnl`: `net_worth = cash + Σ(qty·ltp)`, `invested = Σ real_deposits` (SIP cost
basis; falls back to `capital`), `unrealized = Σ real_holdings.pnl`, `realized = total −
unrealized`. `metrics.days_live()` also drives a "Nd running" counter on every dashboard
card.

## Testing the live path

- `python -m tools.test_place_order` — places and immediately cancels a tiny,
  un-fillable order to confirm live placement works without real exposure.
- The daily audit `tools/verify_data_source.py` (16:05 cron) writes `data_source_audit`
  → the `/bot` verify card.
