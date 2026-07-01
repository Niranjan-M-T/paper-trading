"""Compare the Angel bars ALREADY STORED IN POSTGRES against yfinance — for the
exact 5-minute bars the live bot trades on.

Why this tool: it makes ZERO Angel API calls (reads the `candles` table the poller
already filled), so it's safe to run on the live VPS during market hours — no second
Angel login, no session clash with the poller. It answers the one open question
before yfinance may drive live entries: does yfinance VOLUME match Angel's?

The live strategy S404 gates every entry on a volume spike
  scan_volume / scan_volume_avg20  >=  1.1   (1.2 in bear)
so if a fallback (yfinance) bar's volume is on a different basis than the Angel bars
that built the 20-day average, the gate misfires and fires/suppresses the wrong
real-money entries. This quantifies the divergence, per bar and at the scan windows.

Run ON THE VPS (where the DB + .env live):
    cd ~/paper-trading && pip install yfinance
    python -m tools.compare_db_vs_yf                     # today, default symbols
    python -m tools.compare_db_vs_yf RELIANCE TCS INFY   # specific symbols
    python -m tools.compare_db_vs_yf --date 2026-07-01 SBIN

Read-only: SELECTs candles, fetches yfinance, prints a report. Changes nothing.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, time, timedelta, timezone

import pandas as pd

from src.core.db import close_pool, fetch

IST = timezone(timedelta(hours=5, minutes=30))
_SUFFIX = re.compile(r"-(EQ|BE|BZ|SM|ST)$")


def _yf_symbol(symbol: str) -> str:
    return _SUFFIX.sub("", symbol)


def _to_ist_min(ts) -> datetime:
    t = pd.Timestamp(ts)
    t = t.tz_localize(timezone.utc) if t.tzinfo is None else t
    return t.tz_convert(IST).tz_localize(None).floor("min").to_pydatetime()


async def _db_bars(symbol: str, day) -> tuple[str, pd.DataFrame]:
    """5m bars for `day` from Postgres. Tries the exact symbol, then a -EQ variant."""
    start = datetime.combine(day, time(0, 0), tzinfo=IST)
    end = start + timedelta(days=1)
    for sym in (symbol, f"{symbol}-EQ"):
        rows = await fetch(
            """
            SELECT ts, open::float8, high::float8, low::float8, close::float8, volume
            FROM candles
            WHERE symbol = $1 AND interval = '5m' AND ts >= $2 AND ts < $3
            ORDER BY ts
            """,
            sym, start, end,
        )
        if rows:
            df = pd.DataFrame([dict(r) for r in rows])
            df["ts"] = df["ts"].map(_to_ist_min)
            return sym, df.set_index("ts")[["open", "high", "low", "close", "volume"]]
    return symbol, pd.DataFrame()


def _yf_bars(symbol: str, day) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.Ticker(f"{_yf_symbol(symbol)}.NS").history(
        period="5d", interval="5m", auto_adjust=False)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    raw = raw.rename(columns=str.lower)
    raw.index = [pd.Timestamp(t).tz_convert(IST).tz_localize(None).floor("min") for t in raw.index]
    same = raw[[d.date() == day for d in raw.index]]
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in same.columns]
    return same[cols].sort_index()


async def compare(symbol: str, day) -> None:
    print(f"\n{'='*74}\n  {symbol}  —  {day}  (Postgres Angel bars vs yfinance, 5m)\n{'='*74}")
    db_sym, a = await _db_bars(symbol, day)
    try:
        y = _yf_bars(symbol, day)
    except Exception as exc:  # noqa: BLE001
        print(f"  yfinance error: {exc!r}"); y = pd.DataFrame()

    print(f"  db symbol matched: {db_sym!r}   rows: db={len(a)}  yfinance={len(y)}")
    if a.empty or y.empty:
        print("  cannot compare — one source returned nothing "
              "(DB may be sparse today due to the rate-limit storm).")
        return

    j = a.join(y, how="inner", lsuffix="_a", rsuffix="_y")
    print(f"  aligned bars (same timestamp): {len(j)}")
    if j.empty:
        print("  timestamps did not align — check tz/bar boundaries."); return

    j["close_pctdiff"] = (j["close_y"] - j["close_a"]) / j["close_a"] * 100.0
    j["vol_ratio"] = j["volume_y"] / j["volume_a"].replace(0, pd.NA)
    close_abs = j["close_pctdiff"].abs()
    vr = j["vol_ratio"].dropna()
    off10 = (((vr < 0.9) | (vr > 1.1)).mean() * 100) if len(vr) else float("nan")

    print(f"  PRICE  |close diff|: mean {close_abs.mean():.3f}%  max {close_abs.max():.3f}%")
    print(f"  VOLUME yf/db ratio: median {vr.median():.2f}  "
          f"p10 {vr.quantile(.1):.2f}  p90 {vr.quantile(.9):.2f}")
    print(f"  VOLUME bars >10% off (could flip the >=1.1 spike gate): {off10:.0f}%")
    for hhmm in ("11:00", "14:00"):
        h, m = map(int, hhmm.split(":"))
        row = j[[(ts.hour, ts.minute) == (h, m) for ts in j.index]]
        if not row.empty:
            r = row.iloc[0]
            ratio = f"{r['vol_ratio']:.2f}" if pd.notna(r["vol_ratio"]) else "n/a"
            print(f"    scan {hhmm}: close db={r['close_a']:.2f} yf={r['close_y']:.2f} | "
                  f"vol db={int(r['volume_a'])} yf={int(r['volume_y'])} (ratio {ratio})")


async def main(argv: list[str]) -> None:
    args = [a for a in argv[1:] if not a.startswith("--")]
    day = datetime.now(IST).date()
    if "--date" in argv:
        day = datetime.strptime(argv[argv.index("--date") + 1], "%Y-%m-%d").date()
        args = [a for a in args if a != argv[argv.index("--date") + 1]]
    symbols = [s.upper() for s in args] or ["RELIANCE", "TCS", "INFY", "SBIN"]

    print(f"Compare stored Angel bars vs yfinance for {day}. Symbols: {', '.join(symbols)}")
    print("Read-only; no Angel login, no orders.\n")
    try:
        for sym in symbols:
            await compare(sym, day)
    finally:
        await close_pool()
    print("\nKey: PRICE diff tiny = good. VOLUME — if the yf/db ratio is near 1.0 with a "
          "tight p10–p90, yfinance volume is safe for the S404 spike gate (calibratable). "
          "If it's far from 1.0 or many bars are >10% off, fallback bars would change which "
          "entries fire, so fallback should keep exits warm but not drive entries.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
