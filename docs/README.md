# Paper-Trading — deep documentation index

You are looking at the onboarding pack for the `paper-trading` codebase: a live,
real-money algorithmic trading system for Indian equities (NSE), built on top of a
paper-trading rig that runs many strategies in parallel.

**Start with the root [`CLAUDE.md`](../CLAUDE.md)** — it has the five golden rules
and the one mental model ("stateless replay") you need before anything else. Then
come here for depth.

These docs were written to hand the project to a fresh Claude (or a human) working
in a different IDE, with no access to the prior chat history. They capture what the
code cannot tell you on its own: the *why*, the decision history, the operator's
preferences, and the live-fire gotchas.

## The files

| File | Read it when… |
|---|---|
| [01-architecture.md](01-architecture.md) | You need the system map: processes, data flow, the DB, the engine, the replay model, the web app, the MCP server. |
| [02-strategies.md](02-strategies.md) | You're touching strategies, regime logic, or need the S-lineage (S6 → S404 → S505). Includes the "never guess" rule and the parity chain. |
| [03-live-money-bot.md](03-live-money-bot.md) | You're near real money: the master switch, SIP, broker-authoritative reconciliation, quarantine, and the operational gotchas that cost real rejections to learn. |
| [04-data-sources.md](04-data-sources.md) | Anything about candles: the yfinance hybrid, Angel fallbacks, the dual-account split, poller/backfill cadence. |
| [05-operations.md](05-operations.md) | You're deploying, debugging on the VPS, or reading a debug bundle. PM2 processes, psql access, the deploy runbook template. |
| [06-s505-migration.md](06-s505-migration.md) | The current active project: migrating live money from S404 to S505. Phases, status, the shadow-validation gate. |
| [07-known-issues-and-roadmap.md](07-known-issues-and-roadmap.md) | Open bugs (the ledger-duplication artifact, DPWIRES log noise) and what's planned next. |
| [08-user-preferences.md](08-user-preferences.md) | How the owner works and wants to be worked with. Read this early — it changes how you should respond. |
| [09-decision-log.md](09-decision-log.md) | The narrative history: what was built, in what order, and why each decision was made. The "chat history" distilled. |

## The 30-second orientation

- **Two repos.** This one (`paper-trading`) is the deployable rig. The companion
  `i-want-to-build-an-algo` is the research/backtester and the **authoritative
  source of strategy definitions**. The engine and strategies here are *vendored*
  (byte-for-byte ports), kept in parity by a harness.
- **One engine, two callers.** `run_backtest_v2` powers both the backtester and the
  live trader. "Parity by construction" is the core design principle.
- **Stateless replay.** Every minute, the trader re-derives all trades from candles
  and reconciles. There is no separate live-tick code path to drift.
- **Live money is real and gated.** One Angel One account, ₹20k, running S404 today,
  behind an ON/OFF switch, with a disciplined migration toward S505 underway.
- **The owner runs the VPS.** Claude writes runbooks; the owner executes and pastes
  output.

## Provenance

Written 2026-07-16, distilled from an extended working session that covered: the
yfinance data-source hardening, the full S404→S505 engine port (Phases 1–6), the
paper-shadow deployment, and diagnosis of a ledger-duplication artifact. See
[09-decision-log.md](09-decision-log.md) for the blow-by-blow. Facts about code were
verified against the tree at the time of writing; `file:line` references drift — grep
to confirm before relying on a citation.
