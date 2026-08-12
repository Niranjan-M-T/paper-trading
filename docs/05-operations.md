# 05 — Operations, deploy & debugging

## Topology

- **VPS**: `root@srv1501974` (72.60.219.45), code at `/root/paper-trading`.
- **Process manager**: PM2, all processes named `paperaglo-*`. Python venv at `.venv`
  (`.venv/bin/python`); PM2 uses it automatically.
- **Database**: Postgres + TimescaleDB, reached with
  `psql -h 127.0.0.1 -U paper -d paper_trading` (the `-h` is required — peer auth fails
  without it). Password prompt unless `PGPASSWORD` is set.
- **Web**: uvicorn on :8000 behind Caddy on :443 (Let's Encrypt).
- **Git**: local `origin` = github.com/Niranjan-M-T/paper-trading; deploy upstream =
  github.com/a2zvideos1765-tech/paper-trading (auto-merges from origin). A push toward
  origin moves toward production.

**The owner runs every VPS command and pastes the output back.** Claude does not SSH.
Write runbooks as copy-paste `bash` blocks, one command per block.

## The PM2 processes

`paperaglo-{web, poller, trader, real-trader, backfill, backfill-queue, instruments, mcp}`.
See [01-architecture.md](01-architecture.md) for what each does. Common commands:

```
pm2 ls
```
```
pm2 logs paperaglo-trader --lines 80 --nostream
```
```
pm2 restart paperaglo-trader
```

Note the two traders are distinct: `paperaglo-trader` (paper, `live=FALSE`) and
`paperaglo-real-trader` (real money, `live=TRUE`). Restarting one never touches the
other's portfolios.

## The standard deploy runbook (template)

Claude prepares this; the owner runs it. Fill in the specifics per change.

```
cd ~/paper-trading && git pull
```
Apply any new SQL migration **before** restarting (or the tick errors until the table
exists):
```
psql -h 127.0.0.1 -U paper -d paper_trading -f sql/0NN_whatever.sql
```
Restart only the affected process(es):
```
pm2 restart paperaglo-trader
```
Verify from logs / psql, then watch. Example verify:
```
pm2 logs paperaglo-trader --lines 60 --nostream
```

### Portfolio changes (`config/portfolios.yaml`)

`sync_portfolios_from_yaml()` runs on `paperaglo-trader` start and UPSERTs by name. It
**preserves `started_at` on conflict** and disables any paper portfolio absent from YAML.
Two gotchas:

- **A fresh portfolio's `started_at = now()` → cold features → no trades for ~90 trading
  days.** To warm it and get an immediate track record, backdate `started_at`.
- **`trader.tick` loads candles from the EARLIEST `started_at` across ALL portfolios** and
  re-replays each slice every tick. So backdating one portfolio widens the shared load for
  everyone — don't backdate years, or per-tick cost balloons. ~6 months is usually the
  sweet spot (covers the 90-trading-day feature warmup with margin). The universe regime
  index warms independently (always 1100-day lookback), so `started_at` only needs to
  cover *equity-feature* warmup.
- To set a backdated `started_at`, **pre-INSERT the rows via SQL before the restart** (the
  UPSERT preserves it on conflict — one restart), e.g.:

```
psql -h 127.0.0.1 -U paper -d paper_trading <<'SQL'
INSERT INTO portfolios (name, strategy_id, capital, enabled, live, started_at)
VALUES ('MyPf_20k', 'S505_pat_uni_vixpct_crash', 20000, TRUE, FALSE, '2026-01-01 09:15:00+05:30')
ON CONFLICT (name) DO NOTHING;
SQL
```

Times in SQL use IST offset `+05:30`; the DB stores/returns UTC (09:15 IST = 03:45 UTC).

## Reading a debug bundle

The owner periodically shares `paper-trading-debug-YYYYMMDD-HHMM.md` bundles. Structure:
a Summary (error/warning/rejection counts), a **Process heartbeats** table (with a
`stale?` column and a one-line `detail` per runner), then the raw error/warning events
newest-first.

- The `real_trader` heartbeat detail is gold, e.g.
  `bot ON — 1 live pf, 0 new order(s), 5 stale skipped, cash ₹2,383` tells you the switch
  state, order activity, how many scan entries the scan-time gate deferred, and free cash.
- `backfill`/`backfill_queue` showing `stale? YES` is normal off-hours — they're cron
  jobs, not always-on.
- yfinance "possibly delisted" ERROR spam is noise (see [04](04-data-sources.md)).

## Diagnostics on the box

```
# Is the poller writing candles?
psql -h 127.0.0.1 -U paper -d paper_trading -c "SELECT max(ts) FROM candles WHERE interval='5m';"
```
```
# Trades per portfolio
psql -h 127.0.0.1 -U paper -d paper_trading -c "SELECT portfolio_id, count(*) FROM trades GROUP BY portfolio_id ORDER BY portfolio_id;"
```
```
# Health endpoint from anywhere
curl https://paper.studiohappens.tech/health | python3 -m json.tool
```

Read-only tool scripts (run with the venv python, module form
`python -m tools.<name>`): `verify_setup`, `verify_regime`, `verify_data_source`,
`verify_universe_index`, `probe_data_sources`, `probe_yf_fallback`, `compare_sources`,
`compare_db_vs_yf`, `parity_s505`, `test_place_order`.

## Local dev (owner's Windows box)

- Repo also lives at `C:\Users\niran\Documents\Codex\2026-05-08\paper-trading`.
- Windows uses `.venv\Scripts\python.exe`; shell is PowerShell (a Bash tool is also
  available).
- `python -m pytest tests/ -q` should be green (185 tests as of 2026-07-13).
- The parity harness needs the research repo alongside; `PARITY_ALGO_DIR` overrides its
  path (default `../i-want-to-build-an-algo`).
