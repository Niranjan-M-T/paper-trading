"""Diagnostic + validation for the universe regime feed (Phase 2 of the S505 engine port).
READ-ONLY. Run ON THE VPS.

Two jobs:
  1. DEPTH — report how much 5m equity history the DB holds and whether it covers each
     regime lever's warmup (50d DMA, 60d crash overlay, 252d VIX percentile).
  2. VALIDATE — build the equal-weight universe index from the DB, then compare its S505
     regime labels (source=universe, crash=0.08, vixpct=0.80) against the algo's
     data/UNIVERSE_INDEX_extended.csv over the overlap. High agreement (~99%) confirms the
     DB-computed feed reproduces the series S505 was validated on. This is the "compute
     from DB + validate against the algo CSV" gate.

  python -m tools.verify_universe_index
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd

from src.core.config import REPO_ROOT
from src.core.db import close_pool
from src.core.time import IST, now_ist
from src.core.universe import load_universe
from src.engine.replay import load_candles_window, load_index_close
from src.engine.universe_index import build_from_candles
from src.engine.v2_engine import classify_regime_by_date, clear_regime_cache, prime_regime_index

_REF_CSV = REPO_ROOT / "data" / "UNIVERSE_INDEX_extended.csv"
# S505's exact regime params — the labels we compare must be produced the same way live.
_S505_REGIME = dict(source="universe", crash_overlay_pct=0.08, vix_percentile=0.80)


def _labels(close: pd.Series, breadth: pd.Series, nifty: pd.Series, vix: pd.Series) -> pd.Series:
    """Prime NIFTY/VIX + a given universe close/breadth and classify with S505's params.
    Clears the cache first so a prior run's memo can't leak."""
    clear_regime_cache()
    if not nifty.empty:
        prime_regime_index("NIFTY_50", nifty)
    if not vix.empty:
        prime_regime_index("INDIA_VIX", vix)
    prime_regime_index("UNIVERSE", close)
    prime_regime_index("UNIVERSE_BREADTH", breadth)
    return classify_regime_by_date(**_S505_REGIME)


def _yearly(close: pd.Series, breadth: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"close": close, "breadth": breadth})
    df["year"] = [d.year for d in df.index]
    rows = []
    for y, g in df.groupby("year"):
        c0, c1 = g["close"].iloc[0], g["close"].iloc[-1]
        rows.append((y, (c1 / c0 - 1) * 100, g["breadth"].mean(), len(g)))
    return pd.DataFrame(rows, columns=["year", "idx_ret_pct", "mean_breadth", "n"])


async def _run() -> None:
    equities, _idx = await load_universe()
    symbols = [s.symbol for s in equities]

    print("\n=== universe regime feed: depth + validation ===")
    print(f"  universe symbols     : {len(symbols)}")

    candles = await load_candles_window(symbols, "5m", datetime(2018, 1, 1, tzinfo=IST), now_ist())
    if candles.empty:
        print("  5m equity candles    : NONE — cannot build a universe index yet.")
        return
    days = sorted(candles["date"].unique())
    print(f"  5m equity candles    : {len(candles):,} rows, {len(candles['symbol'].unique())} symbols")
    print(f"  5m date span         : {days[0]} → {days[-1]}  ({len(days)} trading days)")

    nifty = await load_index_close("NIFTY_50", interval="1d")
    vix = await load_index_close("INDIA_VIX", interval="1d")
    print(f"  NIFTY_50 / INDIA_VIX : {len(nifty)} / {len(vix)} daily closes")

    close_db, breadth_db = build_from_candles(candles)
    labels_db = _labels(close_db, breadth_db, nifty, vix)

    n = len(days)
    print("\n  warmup adequacy:")
    for lever, need in (("DMA bull/bear (50d)", 50), ("crash overlay (60d)", 60),
                        ("VIX percentile (252d)", 252)):
        print(f"    {lever:26s}: {'OK' if n >= need else f'SHORT ({n}/{need})'}")

    # ---- Validation vs the algo CSV ----
    if not _REF_CSV.exists():
        print(f"\n  (reference {_REF_CSV.name} not found — skipping label-agreement check)")
        return
    ref = pd.read_csv(_REF_CSV)
    ref.index = [d.date() for d in pd.to_datetime(ref["date"])]
    close_algo = ref["close"].astype(float)
    breadth_algo = ref["breadth"].astype(float)
    labels_algo = _labels(close_algo, breadth_algo, nifty, vix)

    overlap = sorted(set(labels_db.index) & set(labels_algo.index))
    if not overlap:
        print("\n  no overlapping dates between DB index and algo CSV — cannot validate.")
        return
    ldb = labels_db.reindex(overlap)
    lalgo = labels_algo.reindex(overlap)
    agree = (ldb.values == lalgo.values)
    pct = 100.0 * agree.mean()
    print(f"\n  === regime-label agreement (DB vs algo CSV) ===")
    print(f"    overlap window   : {overlap[0]} → {overlap[-1]}  ({len(overlap)} days)")
    print(f"    labels agree     : {int(agree.sum())}/{len(overlap)}  ({pct:.1f}%)")
    if pct < 100.0:
        disagree = [(d, str(ldb[d]), str(lalgo[d])) for d in overlap if ldb[d] != lalgo[d]]
        print(f"    disagreements    : {len(disagree)} (first 10: DB / algo)")
        for d, a, b in disagree[:10]:
            print(f"      {d}  {a:9s} / {b}")

    print("\n  yearly index return % / mean breadth (DB-built | algo CSV):")
    yd = _yearly(close_db, breadth_db).set_index("year")
    ya = _yearly(close_algo, breadth_algo).set_index("year")
    print(f"    {'year':6s}{'ret% DB':>10s}{'ret% algo':>11s}{'brdth DB':>10s}{'brdth algo':>11s}")
    for y in sorted(set(yd.index) & set(ya.index)):
        print(f"    {y:<6d}{yd.loc[y,'idx_ret_pct']:>10.1f}{ya.loc[y,'idx_ret_pct']:>11.1f}"
              f"{yd.loc[y,'mean_breadth']:>10.3f}{ya.loc[y,'mean_breadth']:>11.3f}")


async def _amain() -> None:
    try:
        await _run()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_amain())
