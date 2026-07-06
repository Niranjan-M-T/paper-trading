"""Dry-run probe for the poller's yfinance -> Angel fallbacks. READ-ONLY: never
writes candles, never places orders. Run ON THE VPS (ideally during market hours)
to see, on live data, exactly what the hybrid poller would do this cycle:

  * COVERAGE vs the failover threshold — would the whole cycle fail over to Angel?
  * MISSING set — symbols yfinance returned ZERO bars for (the per-symbol Angel
    top-up targets). Partial no-trade gaps are NOT missing (Angel can't fill those).

Options:
  --force-fail SYM[,SYM...]  pretend yfinance returned nothing for these symbols,
                             to exercise the top-up path on names that are actually
                             fine (proves the classification + Angel recovery work).
  --angel                    also log into Angel and re-pull the top-up targets
                             (read-only) to PROVE Angel has bars where yfinance did
                             not. No upsert — just prints the bar count Angel returns.

  python -m tools.probe_yf_fallback
  python -m tools.probe_yf_fallback --force-fail RELIANCE,TCS --angel
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time

from src.core.config import settings
from src.core.db import close_pool
from src.core.time import IST, is_market_open, now_ist
from src.core.universe import load_universe
from src.core.yf_provider import fetch_5m
from src.runners.poller import _coverage, _symbols_missing


def _arg_list(argv: list[str], flag: str) -> list[str]:
    if flag not in argv:
        return []
    raw = argv[argv.index(flag) + 1]
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


async def main(argv: list[str]) -> None:
    forced = set(_arg_list(argv, "--force-fail"))
    check_angel = "--angel" in argv

    equities, _idx = await load_universe()
    specs_by_symbol = {s.symbol: s for s in equities}
    symbols = list(specs_by_symbol)

    print(f"\n=== yfinance -> Angel fallback probe ===")
    print(f"  universe            : {len(symbols)} symbols")
    print(f"  market open now     : {is_market_open()}")
    print(f"  failover threshold  : {settings.yf_failover_min_coverage:.0%} coverage")
    print(f"  top-up cap          : {settings.yf_topup_max_symbols} symbols/cycle")
    if forced:
        print(f"  --force-fail        : {sorted(forced)} (simulated yfinance miss)")

    print("\n  fetching live yfinance batch ...")
    long_df = fetch_5m(symbols, "1d")
    if forced and long_df is not None and not long_df.empty:
        long_df = long_df[~long_df["symbol"].isin(forced)]

    coverage = _coverage(symbols, long_df)
    missing = _symbols_missing(symbols, long_df)
    would_failover = long_df is None or long_df.empty or coverage < settings.yf_failover_min_coverage

    print(f"\n  coverage            : {coverage:.1%}")
    print(f"  WOULD FAILOVER      : {would_failover}  "
          f"({'whole cycle runs via Angel' if would_failover else 'yfinance drives, per-symbol top-up only'})")
    if not would_failover:
        cap = settings.yf_topup_max_symbols
        targets = missing[:cap]
        print(f"  zero-bar symbols    : {len(missing)}  -> top-up targets: {targets}"
              f"{' (capped)' if len(missing) > cap else ''}")

    if check_angel and not would_failover and missing:
        cap = settings.yf_topup_max_symbols
        targets = [s for s in missing[:cap] if s in specs_by_symbol]
        print(f"\n  --angel: re-pulling {len(targets)} top-up target(s) from Angel (read-only) ...")
        from src.core.angel import AngelClient
        client = AngelClient.for_data()
        now = now_ist()
        open_dt = datetime.combine(now.date(), time(9, 15))
        for sym in targets:
            spec = specs_by_symbol[sym]
            try:
                df = client.get_candle(symbol=sym, token=spec.token, exchange=spec.exchange,
                                       interval="5m", from_dt=open_dt, to_dt=now.replace(tzinfo=None))
                n = 0 if df is None else len(df)
                verdict = "Angel HAS bars (top-up would recover)" if n else "Angel also empty (genuine no-trade)"
                print(f"    {sym:14s} angel_bars={n:3d}  -> {verdict}")
            except Exception as exc:  # noqa: BLE001
                print(f"    {sym:14s} angel_error: {str(exc)[:100]}")
    elif check_angel:
        print("\n  --angel: nothing to re-pull (no zero-bar symbols this cycle).")

    print()


async def _amain(argv: list[str]) -> None:
    # Close the pool on the SAME loop its connections were created on — running
    # close_pool() in a second asyncio.run() hits "Event loop is closed".
    try:
        await main(argv)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_amain(sys.argv))
