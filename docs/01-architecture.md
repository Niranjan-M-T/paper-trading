# 01 — Architecture

How the whole system fits together. For the setup/install steps see the root
[`README.md`](../README.md); this file is the mental map.

## The two repos

| Repo | Role | Relationship |
|---|---|---|
| `i-want-to-build-an-algo` | Research + backtester. **Authoritative source** of strategy and engine logic. | Strategies are developed and validated here. |
| `paper-trading` (this repo) | Deployable live rig. Runs strategies against live data, persists to Postgres, serves a dashboard, and trades one real account. | The engine (`v2_engine.py`) and strategies are **vendored** — copied in, not imported — so the rig has no dependency on research code. Kept in parity by `tools/parity_s505.py` and `tests/`. |

Why vendored, not imported? Different lifecycles. Research code churns; the rig must
be stable and deployable. The cost is that engine changes upstream must be re-vendored
and re-parity-checked (the root README's "Re-syncing the engine from upstream" section
lists the patch points).

## Process model (PM2)

Eight PM2 processes, defined in `ecosystem.config.js`, all named `paperaglo-*`:

| Process | Job | Cadence |
|---|---|---|
| `paperaglo-web` | FastAPI dashboard (uvicorn), behind Caddy on :443. | always up |
| `paperaglo-poller` | Fetch 5m candles for the universe into `candles`. | every `POLLER_INTERVAL_SECONDS` (default 150s), market hours |
| `paperaglo-trader` | Replay the engine for every **paper** portfolio (`live=FALSE`). | every 60s, market hours |
| `paperaglo-real-trader` | Replay for the **live** portfolio (`live=TRUE`) and place real orders when the bot is ON. | every 60s, market hours |
| `paperaglo-backfill` | Nightly historical top-up (5m + 1m equities, 1d indices). | ~16:30 IST weekdays (cron) |
| `paperaglo-backfill-queue` | Drain the on-demand backfill queue for newly added symbols. | overnight |
| `paperaglo-instruments` | Refresh Angel's instrument master (token↔symbol map). | Sundays 03:00 IST |
| `paperaglo-mcp` | MCP server exposing the bot to Claude/tools over a bearer token. | always up |

The poller and the two traders are the hot path. The poller writes candles; the
traders read them ~5s later (`TRADER_OFFSET_SECONDS`) and act.

## Data flow (one tick)

```
                 ┌─────────────────────────────────────────────┐
   Yahoo/Angel   │  poller: fetch 5m bars for ~73 symbols       │
   ───────────►  │  → INSERT into candles (ON CONFLICT DO NOTH) │
                 └───────────────────────┬─────────────────────┘
                                         ▼
                 ┌─────────────────────────────────────────────┐
   candles,      │  trader / real_trader tick (every 60s):      │
   NIFTY, VIX,   │   1. load rolling candle window from DB      │
   universe idx  │   2. prime regime caches (NIFTY/SENSEX/VIX/  │
   ───────────►  │      UNIVERSE/UNIVERSE_BREADTH)              │
                 │   3. run_backtest_v2(candles, strategy)     │
                 │   4. diff trades vs `trades` table, INSERT  │
                 │   5. DELETE+INSERT `positions` snapshot     │
                 │   6. UPSERT equity_snapshots + equity_intraday
                 │   7. (real only) place/reconcile orders     │
                 └───────────────────────┬─────────────────────┘
                                         ▼
                 ┌─────────────────────────────────────────────┐
                 │  Postgres + TimescaleDB  ◄─── web dashboard   │
                 └─────────────────────────────────────────────┘
```

Key point: **the engine is re-run from scratch every tick** over the rolling window.
There is no incremental live-state machine. See the root CLAUDE.md "one mental model."

## The engine — `src/engine/v2_engine.py`

The heart. Exposes:

- **`StrategyV2`** — a frozen dataclass of every strategy knob (entry threshold,
  allocation mode, exit tiers, pyramid ladder, MACD gate, regime source, mode-specific
  `ModeParams` for bull/bear/sideways, adaptive depth-bucket exits, patience-sell,
  and the Round-59 levers). Adding a field requires registering it in
  `src/strategies/schema.py` (a drift guard raises at import otherwise).
- **`run_backtest_v2(candles, strategy, charges, deposits=, external_positions=,
  cash_override=)`** — the stateless replay. Builds daily features (RSI/ATR/BB, 90-day
  low, volume spike, MACD), classifies the daily regime, and walks day-by-day emitting
  BUY/SELL trades and an equity curve. The three keyword seams (`deposits`,
  `external_positions`, `cash_override`) all default to `None` → **backtest byte-for-byte
  unchanged**; they exist only for the live reconciliation path (see
  [03-live-money-bot.md](03-live-money-bot.md)).
- **`classify_regime_by_date(...)`** — bull/bear/sideways per day, from a regime source
  (NIFTY_50 50-DMA by default; `universe`/`breadth` for Round-59) with a VIX fear
  override and optional crash overlay / VIX-percentile / hysteresis (Round-59 levers).
- **`prime_regime_index(symbol, series)` / `clear_regime_cache()`** — inject the daily
  index series the classifier reads. The rig has no CSVs; it primes these from the DB
  each tick. This is a key vendoring patch point.

## The replay glue — `src/engine/replay.py`

Everything between "candles in the DB" and "engine output persisted":

- `load_candles_window(symbols, interval, since, until)` — pull the rolling window
  as the DataFrame the engine expects (IST-localized, with `date`/`time` columns).
- `load_index_close(symbol, interval)` — NIFTY/SENSEX/VIX daily close, primed into the
  regime cache.
- `universe_index_if_needed(portfolios, symbols, until)` — builds the equal-weight
  universe index + breadth (for S505/S525) **only when a portfolio uses it** (gated by
  `_any_universe_source`), day-cached, 1100-day lookback. Returns `(None, None)`
  otherwise so NIFTY-source setups pay nothing and stay byte-identical.
- `replay_one_portfolio(portfolio, strategy, candles, charges, ...)` — the per-portfolio
  workhorse: clears + primes regime caches, applies per-portfolio dashboard overrides
  (`portfolio_overrides`, validated), binds capital, runs the engine, and persists
  trades/positions/equity. Shared by both traders.

`replay.py` is where the two traders converge — get changes here right and both paper
and live inherit them.

## The database

Postgres + TimescaleDB. Migrations in `sql/`, applied in order:

| Migration | Adds |
|---|---|
| 001_schema | `candles`, `trades`, `positions`, `portfolios`, `equity_snapshots`, `runs`, `signals`. Note `trades_portfolio_dedupe` unique index includes `price` (source of the ledger-dup artifact). |
| 002_timescale | hypertables on time-series tables. |
| 003_universe | editable watch list, Angel instrument master, overrides, backfill queue. |
| 004_forward_trading | `portfolios.started_at` (forward-only anchor). |
| 005_india_vix | INDIA_VIX as an index symbol for the regime classifier. |
| 006_intraday_equity | `equity_intraday` (minute-resolution overlay, pruned to ~3d). |
| 007_real_trading | `portfolios.live` flag; broker bookkeeping tables; seeds the one live portfolio `S404_live_sip_20k`. |
| 008_instruments_token_exchange_pk | fixes the instrument PK to `(token, exchange)` — tokens aren't globally unique. |
| 009_external_positions | `real_external_positions` (adopted broker holdings). |
| 010_quarantine | `real_quarantine` (AB4036 surveillance bench). |
| 011_data_source_audit | `data_source_audit` (daily yfinance-vs-Angel verdict for the `/bot` verify card). |

Core tables to know:

- **`portfolios`** — `(id, name, strategy_id, capital, enabled, created_at, started_at,
  live)`. `live=FALSE` = paper (managed by `trader.py` + `portfolios.yaml`); `live=TRUE`
  = real money (managed by `real_trader.py`, never auto-disabled).
- **`candles`** — 5m bars for equities (the engine's interval), 1d for indices. See the
  root README's "one source of truth" table — everything speaks 5m.
- **`trades`** — append-only ledger. **Write-only audit log**: the engine re-emits the
  full list every tick and nothing reads it back for state. Positions/equity come from
  the engine's in-memory result, not this table.
- **`positions`** — DELETE+INSERT snapshot of the engine's holdings each tick.
- **`portfolio_overrides`** — per-portfolio JSONB knob overrides set from the dashboard.

## The web app — `src/web/`

FastAPI + Jinja templates + a little JS. Routes in `src/web/routes/`:
`dashboard`, `portfolios`, `trades`, `symbols`, `bot`, `alerts`, `diagnose`, `health`,
`login`, `api`. Notable pages:

- `/` dashboard — equity curves, NIFTY/SENSEX overlay, per-portfolio cards.
- `/bot` — the real-money control surface: ON/OFF switch, reconciled holdings (broker
  truth), quarantine card, data-source verify card.
- `/portfolio/{id}` — per-portfolio detail with editable strategy parameters.
- `/diagnose/{portfolio_id}/{symbol}/{ts}` — "why did it trade here?" replay view.
- `/health` — runner heartbeats (stale detection).

Auth: `DASHBOARD_PASSWORD` (full) and optional `VIEWER_PASSWORD` (read-only).

## The MCP server — `src/mcp/server.py`

Exposes the bot over MCP (bearer token `MCP_TOKEN`) so Claude/tools can query state and
drive it. Runs as `paperaglo-mcp`.

## Configuration — `src/core/config.py`

All env-driven via a frozen `Settings` dataclass. Knobs worth knowing:

- `DATA_SOURCE` = `angel` | `yfinance` (live: `yfinance`). See [04](04-data-sources.md).
- `YF_FAILOVER_MIN_COVERAGE` (0.5), `YF_TOPUP_MAX_SYMBOLS` (20) — yfinance resilience.
- `POLLER_INTERVAL_SECONDS` (150), `TRADER_INTERVAL_SECONDS` (60),
  `TRADER_OFFSET_SECONDS` (5).
- `REAL_TRADER_INTENT_MAX_AGE_DAYS` (1) — how stale an engine signal may be and still
  be placed (covers late-arriving candles).
- **Dual Angel account**: `ANGEL2_*` + `ANGEL_DATA_ACCOUNT` (auto|1|2) +
  `ANGEL_TRADING_ACCOUNT` (1|2). When a second account is configured, market-data
  fetching moves to it so account 1 is dedicated to trading. `resolve_angel_account()`
  maps the selector.
