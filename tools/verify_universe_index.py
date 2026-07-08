"""Diagnostic for the universe regime feed (Phase 2 of the S505 engine port). READ-ONLY.

Run ON THE VPS. Answers the one question that decides the feed design: how much 5m equity
history does the DB actually hold? The universe regime source needs trailing warmup —
~50d for the DMA, 60d for the crash overlay, and 252d for the VIX-percentile lever S505 uses.

It:
  1. reports 5m equity history depth (date span, trading days, symbol count) and 1d
     NIFTY/INDIA_VIX depth,
  2. builds the equal-weight universe index + breadth from the DB (src/engine/universe_index),
  3. primes NIFTY/VIX/UNIVERSE and prints the last ~15 days of the S505 regime labels
     (source="universe", crash_overlay=0.08, vix_percentile=0.80) as a sanity check,
  4. verdicts whether there's enough history for each lever.

  python -m tools.verify_universe_index
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.db import close_pool
from src.core.time import IST, now_ist
from src.core.universe import load_universe
from src.engine.replay import load_candles_window, load_index_close
from src.engine.universe_index import build_from_candles
from src.engine.v2_engine import classify_regime_by_date, clear_regime_cache, prime_regime_index


async def _run() -> None:
    equities, _idx = await load_universe()
    symbols = [s.symbol for s in equities]
    since = datetime(2018, 1, 1, tzinfo=IST)
    until = now_ist()

    print("\n=== universe regime feed diagnostic ===")
    print(f"  universe symbols     : {len(symbols)}")

    candles = await load_candles_window(symbols, "5m", since, until)
    if candles.empty:
        print("  5m equity candles    : NONE — cannot build a universe index yet.")
        return
    days = sorted(candles["date"].unique())
    print(f"  5m equity candles    : {len(candles):,} rows, {len(candles['symbol'].unique())} symbols")
    print(f"  5m date span         : {days[0]} → {days[-1]}  ({len(days)} trading days)")

    nifty = await load_index_close("NIFTY_50", interval="1d")
    vix = await load_index_close("INDIA_VIX", interval="1d")
    print(f"  NIFTY_50 1d          : {len(nifty)} days"
          + (f" ({nifty.index[0]} → {nifty.index[-1]})" if not nifty.empty else " — MISSING"))
    print(f"  INDIA_VIX 1d         : {len(vix)} days"
          + (f" ({vix.index[0]} → {vix.index[-1]})" if not vix.empty else " — MISSING"))

    close, breadth = build_from_candles(candles)
    print(f"  universe index built : {len(close)} days"
          + (f" ({close.index[0]} → {close.index[-1]})" if not close.empty else ""))

    # Prime and classify with S505's exact regime params.
    clear_regime_cache()
    if not nifty.empty:
        prime_regime_index("NIFTY_50", nifty)
    if not vix.empty:
        prime_regime_index("INDIA_VIX", vix)
    prime_regime_index("UNIVERSE", close)
    prime_regime_index("UNIVERSE_BREADTH", breadth)

    regime = classify_regime_by_date(source="universe", crash_overlay_pct=0.08, vix_percentile=0.80)
    print("\n  last 15 days of S505 regime (source=universe, crash=0.08, vixpct=0.80):")
    if regime.empty:
        print("    (empty — universe index not primed / no data)")
    else:
        for d, lab in list(regime.items())[-15:]:
            bre = breadth.get(d)
            print(f"    {d}  {lab:9s}  breadth={bre:.2f}" if bre is not None else f"    {d}  {lab}")

    n = len(days)
    print("\n  warmup adequacy (universe index days available):")
    for lever, need in (("DMA bull/bear (50d)", 50), ("crash overlay (60d)", 60),
                        ("VIX percentile (252d)", 252)):
        ok = "OK" if n >= need else f"SHORT ({n}/{need})"
        print(f"    {lever:26s}: {ok}")
    if n < 252:
        print("\n  ⚠ Fewer than 252 universe days: the VIX-percentile lever won't be fully warmed")
        print("    until more history accumulates (or we seed history from the algo CSV).")


async def _amain() -> None:
    try:
        await _run()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_amain())
