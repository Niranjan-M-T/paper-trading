"""Live data poller: once per minute during market hours, fetch the most recent
5-minute candles for each symbol in config/universe.yaml and upsert to `candles`.

Why 5-minute, not 1-minute? The engine (and the entire backtest grid) was tuned
on 5-min bars; the historical CSVs in `data/angel_symbols/` are 5-min bars. The
trader queries `interval='5m'` for replay (see runners/trader.py CANDLE_INTERVAL).
Writing 1-min bars from the poller would leave the trader blind during market
hours — it would only see the previous night's backfill. Keep one interval
end-to-end.

The nightly backfill still pulls 1-min bars too (see runners/backfill.py
EQUITY_INTERVALS) so the dashboard can offer a finer-grained chart later if
needed; live polling sticks to 5m.

We poll every minute even though the bar is 5 minutes long: each poll fetches a
trailing 5-minute window, so the still-forming current bar gets refreshed via
ON CONFLICT DO UPDATE until it finalises. This keeps trader latency low without
extra Angel calls per symbol.

Run via PM2:  pm2 start ecosystem.config.js --only paperaglo-poller
Run manually: python -m src.runners.poller
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from src.core.angel import AngelClient, AngelSessionError
from src.core.config import settings
from src.core.db import close_pool, conn, get_pool, heartbeat
from src.core.logging import setup_logging
from src.core.time import IST, is_market_open, now_ist, seconds_until_market_open
from src.core.universe import load_universe
from src.core.yf_provider import fetch_5m


log = setup_logging("poller")
INTERVAL = "5m"  # MUST match runners/trader.py CANDLE_INTERVAL and load_history.py default.


# Angel session, re-authenticated daily. The SmartConnect JWT expires at midnight
# IST; a process that logs in once and never refreshes will, after expiry, get
# auth-failure responses that look like "no data" — which is exactly how the poller
# silently wrote 0 rows all day. We track the login date and re-login on rollover.
_client: AngelClient | None = None
_session_date = None


def _get_client() -> AngelClient:
    global _client, _session_date
    today = now_ist().date()
    if _client is None or _session_date != today:
        _client = AngelClient.for_data()
        _session_date = today
        log.info("angel login ok", extra={"account": _client.account, "session_date": str(today)})
    return _client


def _reset_client() -> None:
    global _client, _session_date
    _client = None
    _session_date = None


async def upsert_bars(symbol: str, df) -> int:
    if df.empty:
        return 0
    rows = []
    for ts, _, o, h, l, c, v in df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        ts = ts.tz_convert(IST) if hasattr(ts, "tz_convert") and ts.tzinfo else ts.tz_localize(IST) if ts.tzinfo is None else ts
        rows.append((symbol, INTERVAL, ts.to_pydatetime(), float(o), float(h), float(l), float(c), int(v)))
    async with conn() as c:
        await c.executemany(
            """
            INSERT INTO candles (symbol, interval, ts, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, interval, ts) DO UPDATE
                SET open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume
            """,
            rows,
        )
    return len(rows)


# ---- yfinance coverage helpers (pure — unit-tested in tests/test_poller.py) ----

def _covered_symbols(long_df) -> set:
    """Symbols the yfinance batch actually returned at least one bar for."""
    if long_df is None or long_df.empty:
        return set()
    return {str(s) for s in long_df["symbol"].unique()}


def _coverage(symbols: list[str], long_df) -> float:
    """Fraction of the universe yfinance returned any data for (1.0 if empty universe)."""
    if not symbols:
        return 1.0
    covered = _covered_symbols(long_df)
    return sum(1 for s in symbols if s in covered) / len(symbols)


def _symbols_missing(symbols: list[str], long_df) -> list[str]:
    """Universe symbols yfinance returned ZERO bars for this cycle (fetch/resolve
    failures) — NOT partial no-trade gaps, whose symbols still appear in `covered`
    (Angel can't fill a no-trade window either, so those are deliberately skipped)."""
    covered = _covered_symbols(long_df)
    return [s for s in symbols if s not in covered]


async def _poll_angel_symbols(client: AngelClient, specs, since, until) -> int:
    """Fetch each spec's 5m bars over [since, until] from Angel and upsert. Returns
    rows upserted. AngelSessionError bubbles (token expiry must re-login in main())."""
    total = 0
    for spec in specs:
        try:
            df = client.get_candle(
                symbol=spec.symbol, token=spec.token, exchange=spec.exchange,
                interval=INTERVAL,
                from_dt=since.replace(tzinfo=None), to_dt=until.replace(tzinfo=None),
            )
        except AngelSessionError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("poll failed", extra={"symbol": spec.symbol, "err": str(exc)})
            continue
        total += await upsert_bars(spec.symbol, df)
        # 1.25s pause between symbols mirrors algo project's rate-limit headroom.
        await asyncio.sleep(1.25)
    return total


async def _poll_once_yf(client: AngelClient, specs, since, until) -> tuple[int, str]:
    """Hybrid yfinance poll with Angel fallbacks. Returns (rows_upserted, source_tag).

    1. One batched yfinance download for the whole universe (runs the blocking
       call in a thread so the event loop isn't stalled).
    2. Whole-poller FAILOVER: if the batch is empty or covers less than
       settings.yf_failover_min_coverage of the universe, Yahoo is effectively
       down this cycle — run the entire poll through Angel instead, so the bot
       never goes blind.
    3. Per-symbol TOP-UP: symbols yfinance returned zero bars for (a resolve/fetch
       failure — distinct from a quiet no-trade window, which Angel can't fill
       either) are re-pulled from Angel for the full session so far, capped at
       settings.yf_topup_max_symbols so a partial Yahoo outage can't reintroduce
       the rate-limit storm."""
    symbols = [s.symbol for s in specs]
    long_df = await asyncio.to_thread(fetch_5m, symbols, "1d")
    coverage = _coverage(symbols, long_df)

    if long_df is None or long_df.empty or coverage < settings.yf_failover_min_coverage:
        log.warning("yfinance coverage low — failing over to Angel this cycle",
                    extra={"coverage": round(coverage, 3),
                           "min": settings.yf_failover_min_coverage,
                           "symbols": len(symbols)})
        total = await _poll_angel_symbols(client, specs, since, until)
        return total, f"angel (yfinance failover · cov={coverage:.0%})"

    total = 0
    for sym, group in long_df.groupby("symbol"):
        total += await upsert_bars(str(sym), group)

    missing = _symbols_missing(symbols, long_df)
    src = "yfinance"
    if missing:
        cap = settings.yf_topup_max_symbols
        missing_set = set(missing)
        targets = [s for s in specs if s.symbol in missing_set][:cap]
        # Heal the whole session for a failed symbol, not just the trailing window
        # — it may have been missing from Yahoo since the open.
        session_open = until.replace(hour=9, minute=15, second=0, microsecond=0)
        topped = await _poll_angel_symbols(client, targets, session_open, until)
        total += topped
        src = f"yfinance (+{len(targets)} angel top-up)"
        log.info("yfinance per-symbol Angel top-up",
                 extra={"missing": len(missing), "attempted": len(targets),
                        "capped": len(missing) > cap, "rows": topped})
    return total, src


async def poll_once(client: AngelClient) -> None:
    """One pass over the universe — fetch the trailing few 5-min bars and upsert.

    Universe is re-read every cycle so symbol additions/removals from /symbols
    take effect within ~60s with no restart needed."""
    t0 = time.monotonic()
    equities, _indices = await load_universe()
    specs = equities  # poller only fetches equities at 5m; indices are 1d via backfill
    now = now_ist()
    # 15-minute trailing window: covers the in-progress 5-min bar plus the two
    # prior bars, so a single missed cycle self-heals on the next poll. UPSERTs
    # are cheap; over-fetching by a couple of bars is the right trade-off.
    since = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    until = now.replace(second=0, microsecond=0)

    # Hybrid data source (DATA_SOURCE=yfinance): one batched Yahoo download for the
    # whole universe instead of 73 sequential Angel calls — this removes the Angel
    # historical-API rate-limit storm — with Angel fallbacks (whole-cycle failover
    # + per-symbol top-up) inside _poll_once_yf. Angel also stays authoritative for
    # entry bars via the real_trader scan-confirm. Default 'angel' keeps the
    # unchanged per-symbol Angel sweep.
    if settings.data_source == "yfinance":
        total, src = await _poll_once_yf(client, specs, since, until)
    else:
        total, src = await _poll_angel_symbols(client, specs, since, until), "angel"

    elapsed = time.monotonic() - t0
    interval = settings.poller_interval_seconds
    # If a full sweep takes longer than the tick interval, live candles arrive
    # late — the cause of "stale" signals. Surface it loudly so it's visible on
    # /health and in the bot log panel instead of silently lagging.
    slow = elapsed > interval
    detail = f"{total} rows · {len(specs)} symbols · {elapsed:.0f}s/cycle · {src}"
    if slow:
        detail += f" · SLOW (> {interval}s tick — candles lagging real-time)"
        log.warning("poll cycle slower than tick interval — candles lag real-time",
                    extra={"elapsed_s": round(elapsed, 1), "interval_s": interval,
                           "symbols": len(specs), "source": src})
    await heartbeat("poller", "ok", detail=detail)
    log.info("poll cycle done",
             extra={"upserted": total, "symbols": len(specs),
                    "elapsed_s": round(elapsed, 1), "source": src})


async def main() -> None:
    await get_pool()
    log.info("poller starting", extra={"interval": INTERVAL,
                                       "tick_seconds": settings.poller_interval_seconds})

    # Log in eagerly so credential problems surface immediately (PM2 restarts on
    # a hard exit). Subsequent ticks re-auth automatically at the IST date rollover.
    _get_client()

    try:
        while True:
            if not is_market_open():
                wait = max(60.0, min(seconds_until_market_open(), 1800.0))
                await heartbeat("poller", "sleeping", detail="market closed")
                log.info("market closed, sleeping", extra={"wait_seconds": wait})
                await asyncio.sleep(wait)
                continue

            cycle_start = time.monotonic()
            try:
                # _get_client() re-logs in if the daily token rolled over.
                await poll_once(_get_client())
            except AngelSessionError as exc:
                # Token expired mid-session — drop the client so the next cycle
                # re-authenticates, and make the failure loud (not a silent 0 rows).
                log.warning("angel session expired — re-login queued for next cycle",
                            extra={"err": str(exc)[:200]})
                _reset_client()
                await heartbeat("poller", "error", detail="angel session expired; re-login queued")
            except Exception as exc:  # noqa: BLE001
                log.exception("poll cycle errored")
                await heartbeat("poller", "error", detail=str(exc)[:200])

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, settings.poller_interval_seconds - elapsed)
            await asyncio.sleep(sleep_for)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
