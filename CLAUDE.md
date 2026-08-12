# CLAUDE.md — operating manual for this repo

This file is auto-loaded by Claude Code. Read it first, every session. It is the
short version; the long version lives in [`docs/`](docs/README.md).

This is a **live, real-money trading system.** A real Angel One account places
real orders from this code. Treat every change as production. When in doubt, stop
and ask.

---

## The five golden rules

1. **Never guess about strategies. Read the source, always.**
   The authoritative strategy definitions live in the companion research repo
   `i-want-to-build-an-algo` (path is machine-specific; on the owner's box it is
   `C:\Users\niran\Documents\Codex\2026-05-08\i-want-to-build-an-algo`). Before you
   answer *anything* about how a strategy behaves, read that project's
   `strategies_v2.py` and `engine_v2.py`. The strategies in this repo
   (`src/strategies/*.py`) are **vendored copies** — byte-for-byte ports validated
   by a parity harness. If you cannot find the research repo, say so; do not
   reconstruct strategy behavior from memory.

2. **Only commit or push when the user explicitly asks.** Not after "finishing" a
   change, not "to be safe." The local `origin`
   (github.com/Niranjan-M-T/paper-trading) auto-merges into the deploy upstream
   (github.com/a2zvideos1765-tech/paper-trading), so a push moves toward
   production. Stage the work, show the diff, wait for the word.

3. **Real money is gated behind a master switch and a migration discipline.** The
   live bot is ON/OFF from `/bot`. Any migration of the live strategy is
   parity-gated: prove equivalence, shadow it in paper, *then* flip. Never change
   what real money trades without the user's explicit go-ahead and passing
   validation. See [docs/06-s505-migration.md](docs/06-s505-migration.md).

4. **The user runs all VPS commands.** Claude does not SSH. Hand the user
   copy-paste `bash` blocks (one command per fenced block) and read the output
   they paste back. Deploy = give a runbook, not run it.

5. **Parity is the whole design.** The live trader and the backtester run the
   *same* engine (`src/engine/v2_engine.py`, vendored from the research repo). Any
   engine change must keep `external_positions=None` / default-off behavior
   byte-identical, verified by `tests/` and `tools/parity_s505.py`. Breaking
   parity silently corrupts live trading.

---

## The one mental model that explains everything

**The engine is a stateless replay.** `run_backtest_v2` does not hold live state.
Every 60 seconds the trader loads a rolling window of candles from the DB and
*re-derives the entire trade history from scratch*, as if backtesting up to "now."
It then diffs that against what's already stored and inserts the new rows.

This single fact explains almost every non-obvious behavior in the system:

- **Why there's no "live position" logic** — positions are just the tail of a
  fresh replay, DELETE+INSERT into the `positions` table each tick.
- **Why the `trades` table can show duplicate rows** — a still-forming 5-minute
  bar re-prices each tick, and `price` is in the dedupe key, so the same intended
  trade re-inserts at each price variant. It is a *ledger display artifact*, not
  real over-trading. (See [docs/07](docs/07-known-issues-and-roadmap.md).)
- **Why real trading needs "broker-authoritative reconciliation"** — a stateless
  replay assumes every signal filled perfectly; the real broker rejects, partial-
  fills, and holds manual buys. The `external_positions` / `cash_override` seams
  reconcile the replay to broker truth. (See [docs/03](docs/03-live-money-bot.md).)
- **Why forward-only works** — each portfolio slices candles from its `started_at`,
  so a fresh portfolio has cold features until indicators warm (~90 trading days).

If you internalize "stateless replay, re-run every minute," the codebase stops
being surprising.

---

## Where things are

| Path | What |
|---|---|
| `src/engine/v2_engine.py` | The vendored engine (`run_backtest_v2`, `StrategyV2`, regime classifier). The brain. |
| `src/engine/replay.py` | Glue: load candles, prime regime caches, run the engine, persist trades/positions/equity. Shared by paper + live traders. |
| `src/runners/trader.py` | Paper trader (all paper portfolios, `live=FALSE`). |
| `src/runners/real_trader.py` | Real-money trader (the one live portfolio, `live=TRUE`). Order placement, reconciliation, quarantine. |
| `src/runners/poller.py` | Fetches 5m candles into the DB every cycle (yfinance or Angel). |
| `src/strategies/*.py` | One file per strategy, `STRATEGY = StrategyV2(...)`. Auto-discovered by `registry.py`. |
| `src/strategies/_r59_base.py` | Shared S447 chassis for S455/S505/S525 (not itself registered). |
| `config/portfolios.yaml` | Which strategies run at which capital. UPSERT-synced on trader start. |
| `sql/0NN_*.sql` | Migrations, applied in order. Newest: 011. |
| `tools/*.py` | Diagnostics, parity harness, data probes, order test. |
| `README.md` | Setup/ops guide (predates the real-money bot & yfinance work). |
| `docs/` | The deep context — read the index. |

---

## Before you start working

- Read [`docs/README.md`](docs/README.md) and the file for the area you're touching.
- If it's a strategy question: read the research repo's `strategies_v2.py` first
  (rule 1).
- If it's the live bot: read [docs/03](docs/03-live-money-bot.md) — the gotchas
  there (NSE-not-BSE, paise tick sizes, IP whitelist, daily re-auth) are hard-won
  and not obvious from code.
- Run `python -m pytest tests/ -q` to confirm the tree is green before and after.
- Don't commit. Show the diff and the runbook.
