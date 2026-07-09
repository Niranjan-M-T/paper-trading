"""Phase 4 parity harness: does the VENDORED engine reproduce the ALGO engine's
S455 / S505 / S525?  LOCAL DEV TOOL — needs the algo project checked out alongside
this repo (set PARITY_ALGO_DIR, default ../i-want-to-build-an-algo). Not run on the VPS.

Feed BOTH engines the identical 5m prices (the algo's angel_symbols) and the identical
regime series — the algo reads its CSVs from disk; we prime the vendored engine from the
SAME CSVs — run each strategy on both, and diff the trade lists. Exact-sequence-identical
trades + equity-to-the-rupee is the green light before shadow/live.

  python -m tools.parity_s505
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

for _k, _v in {"PG_PASSWORD": "t", "ANGEL_API_KEY": "t", "ANGEL_CLIENT_CODE": "t",
               "ANGEL_PASSWORD": "t", "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
               "DASHBOARD_PASSWORD": "t", "SESSION_SECRET": "t"}.items():
    os.environ.setdefault(_k, _v)

import pandas as pd

PAPER = Path(__file__).resolve().parents[1]
ALGO = Path(os.getenv("PARITY_ALGO_DIR") or PAPER.parent / "i-want-to-build-an-algo")
SYMBOL_DIR = ALGO / "data" / "angel_symbols"
ALGO_DATA = ALGO / "data"
EXCLUDE = {"NIFTY_50", "SENSEX", "INDIA_VIX"}
STRATEGIES = ["S455_s447_pat250_12", "S505_pat_uni_vixpct_crash", "S525_s505_ddgov"]
LOAD_START = "2021-06-01"   # price warmup for features
RUN_START = "2022-01-01"    # first trading day compared (covers 2022 bear → 2025 bear)

sys.path.insert(0, str(PAPER))
sys.path.insert(0, str(ALGO))

from src.engine.v2_engine import (  # noqa: E402
    ChargeConfigV2 as VCharges, clear_regime_cache, prime_regime_index,
    run_backtest_v2 as v_run,
)
from src.strategies.registry import get as vget  # noqa: E402
import engine_v2 as algo_engine  # noqa: E402
from strategies_v2 import build_strategies  # noqa: E402


def _load_symbol(sym: str) -> pd.DataFrame | None:
    csv = SYMBOL_DIR / f"{sym}.csv"
    if not csv.exists():
        return None
    cache = SYMBOL_DIR / "__parity_cache__" / f"{sym}.parquet"
    if cache.exists() and cache.stat().st_mtime >= csv.stat().st_mtime:
        return pd.read_parquet(cache)
    f = pd.read_csv(csv, parse_dates=["timestamp"])
    f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
    f["symbol"] = f["symbol"].astype(str).str.upper()
    f["date"] = f["timestamp"].dt.date
    f["time"] = f["timestamp"].dt.strftime("%H:%M")
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        f.to_parquet(cache, index=False)
    except Exception:
        pass
    return f


def load_prices(symbols, start):
    frames = [x for s in symbols if (x := _load_symbol(s)) is not None]
    prices = pd.concat(frames, ignore_index=True)
    start_d = pd.to_datetime(start).date()
    prev = sorted(d for d in prices["date"].unique() if d < start_d)
    keep = prev[-1] if prev else start_d
    prices = prices[prices["date"] >= keep]
    return prices.sort_values(["date", "symbol", "timestamp"]).reset_index(drop=True)


def prime_vendored_regime():
    """Prime the vendored regime cache from the SAME CSVs the algo engine reads."""
    clear_regime_cache()
    nif = pd.read_csv(ALGO_DATA / "NIFTY_50_extended.csv")
    prime_regime_index("NIFTY_50", pd.Series(nif["close"].astype(float).values,
                                             index=[d.date() for d in pd.to_datetime(nif["date"])]))
    v = pd.read_csv(ALGO_DATA / "INDIA_VIX_extended.csv", skiprows=[1, 2])
    v.columns = ["date", "close"]
    dts = pd.to_datetime(v["date"], errors="coerce")
    m = dts.notna()
    prime_regime_index("INDIA_VIX", pd.Series(v["close"].astype(float).values[m.values],
                                              index=[d.date() for d in dts[m]]))
    u = pd.read_csv(ALGO_DATA / "UNIVERSE_INDEX_extended.csv")
    uidx = [d.date() for d in pd.to_datetime(u["date"])]
    prime_regime_index("UNIVERSE", pd.Series(u["close"].astype(float).values, index=uidx))
    prime_regime_index("UNIVERSE_BREADTH", pd.Series(u["breadth"].astype(float).values, index=uidx))


def norm(trades):
    return [(str(t["date"]), t["time"], t["symbol"], t["side"], int(t["qty"]), round(float(t["price"]), 2))
            for t in trades]


def compare(name, prices, algo_strats):
    golden = algo_engine.run_backtest_v2(prices, algo_strats[name], algo_engine.ChargeConfigV2(), RUN_START)
    prime_vendored_regime()
    test = v_run(prices, vget(name), VCharges(), RUN_START)
    gt, tt = norm(golden["trades"]), norm(test["trades"])
    gset, tset = set(gt), set(tt)
    only_algo = [x for x in gt if x not in tset]
    only_vend = [x for x in tt if x not in gset]
    ge = round(golden["summary"]["final_equity"], 2)
    te = round(test["summary"]["final_equity"], 2)
    identical = gt == tt
    verdict = "PASS" if identical else ("SET-MATCH/ORDER-DIFF" if not only_algo and not only_vend else "MISMATCH")
    print(f"\n{name}")
    print(f"  trades     algo/vendored : {len(gt)} / {len(tt)}")
    print(f"  final eq   algo/vendored : {ge:,.0f} / {te:,.0f}  (diff {te-ge:+,.2f})")
    print(f"  only-algo / only-vendored: {len(only_algo)} / {len(only_vend)}")
    print(f"  VERDICT                  : {verdict}")
    for label, rows in (("only-algo", only_algo), ("only-vendored", only_vend)):
        for r in rows[:8]:
            print(f"    [{label}] {r}")
    return identical


def main():
    symbols = sorted(p.stem.upper() for p in SYMBOL_DIR.glob("*.csv") if p.stem.upper() not in EXCLUDE)
    print(f"symbols: {len(symbols)} | load {LOAD_START} | run {RUN_START} | algo={ALGO}")
    t0 = time.monotonic()
    prices = load_prices(symbols, LOAD_START)
    print(f"loaded {len(prices):,} rows in {time.monotonic()-t0:.0f}s")
    algo_strats = build_strategies()
    results = {name: compare(name, prices, algo_strats) for name in STRATEGIES}
    print("\n=== SUMMARY ===")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("\nALL PASS" if all(results.values()) else "\nSOME FAILED — investigate")


if __name__ == "__main__":
    main()
