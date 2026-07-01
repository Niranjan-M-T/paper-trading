"""Compare Angel One vs yfinance 5-minute bars for the SAME NSE symbols/timestamps.

Answers the question that must be settled before yfinance is allowed to drive live
entries: do the two sources AGREE on each 5m bar — especially VOLUME?

Why volume specifically: the live strategy S404 gates every entry on a volume spike
  scan_volume / scan_volume_avg20  >=  1.1   (1.2 in bear)
i.e. the 11:00/14:00 bar's volume vs its trailing-20-day average (engine_v2.py).
Angel/NSE and Yahoo agree on PRICE to within paise, but Yahoo's NSE VOLUME is often
on a different basis. If a yfinance bar's volume is compared against an Angel-built
20-day average, that gate misfires — firing/suppressing real-money entries the
Angel-tuned backtest never would. This script quantifies that divergence.

Modes:
  (default)  compare today's COMPLETED session, bar-by-bar. Safe to run anytime —
             incl. right now with the market closed; the live poller is asleep so
             there is no Angel-session clash.
  --loop     live mode (market hours): every 60s print each source's LATEST bar and
             its age, to measure yfinance's intraday DELAY vs Angel.

Local setup: put your Angel DATA-account creds in a .env at the repo root:
    ANGEL_API_KEY=...
    ANGEL_CLIENT_CODE=...
    ANGEL_PASSWORD=...
    ANGEL_TOTP_SECRET=...
Other repo env vars are stubbed automatically; this script NEVER touches the DB and
places no orders — it only reads candles.

CAVEAT: during market hours a fresh Angel login here may briefly bump the VPS
poller's Angel session (it self-heals on the poller's next cycle). Prefer running
the completed-session comparison while the market is closed.

Run:
    python -m tools.compare_sources                 # today, default symbols
    python -m tools.compare_sources RELIANCE TCS    # specific symbols
    python -m tools.compare_sources --loop          # live freshness (market hours)
"""

from __future__ import annotations

import os
import sys
import time as _time
from datetime import datetime, time, timedelta, timezone

# Stub the non-Angel required env vars so importing src.core.config doesn't fail —
# this script reads ANGEL_* from .env and touches nothing else.
for _k, _v in {"PG_PASSWORD": "stub", "DASHBOARD_PASSWORD": "stub",
               "SESSION_SECRET": "stub"}.items():
    os.environ.setdefault(_k, _v)

import pandas as pd  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# Standard NSE symboltokens for a handful of liquid names. Override on the CLI if
# you want others (the Angel row count printed will reveal a wrong token instantly).
TOKENS = {
    "RELIANCE": "2885",
    "TCS": "11536",
    "INFY": "1594",
    "HDFCBANK": "1333",
    "SBIN": "3045",
    "ICICIBANK": "4963",
}


def _to_ist_naive(ts) -> datetime:
    """Normalise any timestamp to tz-naive IST floored to the minute for joining."""
    t = pd.Timestamp(ts)
    t = t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)
    return t.tz_localize(None).floor("min").to_pydatetime()


def fetch_angel(symbol: str, day, from_t=time(9, 15), to_t=time(15, 30)) -> pd.DataFrame:
    """Angel 5m bars for `day`. Returns DataFrame[ts(IST-naive), open..close, volume]."""
    from src.core.angel import AngelClient
    token = TOKENS.get(symbol)
    if not token:
        raise ValueError(f"no hardcoded token for {symbol}; add it to TOKENS")
    client = AngelClient.for_data()
    df = client.get_candle(
        symbol=symbol, token=token, exchange="NSE", interval="5m",
        from_dt=datetime.combine(day, from_t), to_dt=datetime.combine(day, to_t),
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.lower).copy()
    df["ts"] = df["timestamp"].map(_to_ist_naive)
    return df[["ts", "open", "high", "low", "close", "volume"]].set_index("ts").sort_index()


def fetch_yf(symbol: str, day) -> pd.DataFrame:
    """yfinance 5m bars for `day` (filtered from a 5-day pull)."""
    import yfinance as yf
    raw = yf.Ticker(f"{symbol}.NS").history(period="5d", interval="5m", auto_adjust=False)
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    raw = raw.rename(columns=str.lower)
    raw.index = [pd.Timestamp(t).tz_convert(IST).tz_localize(None).floor("min") for t in raw.index]
    raw.index.name = "ts"
    same_day = raw[[d.date() == day for d in raw.index]]
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in same_day.columns]
    return same_day[cols].sort_index()


def compare_day(symbol: str, day) -> None:
    print(f"\n{'='*72}\n  {symbol}  —  {day}  (Angel vs yfinance, 5m bars)\n{'='*72}")
    try:
        a = fetch_angel(symbol, day)
    except Exception as exc:  # noqa: BLE001
        print(f"  ANGEL unavailable: {exc!r}")
        print(f"  (set ANGEL_* in a local .env; data fetch needs a valid login)")
        a = pd.DataFrame()
    try:
        y = fetch_yf(symbol, day)
    except Exception as exc:  # noqa: BLE001
        print(f"  yfinance error: {exc!r}")
        y = pd.DataFrame()

    print(f"  rows: angel={len(a)}  yfinance={len(y)}")
    if a.empty or y.empty:
        print("  cannot compare — one source returned nothing.")
        return

    j = a.join(y, how="inner", lsuffix="_a", rsuffix="_y")
    print(f"  aligned bars (same timestamp): {len(j)}")
    if j.empty:
        print("  timestamps did not align — check timezone/bar boundaries.")
        return

    j["close_pctdiff"] = (j["close_y"] - j["close_a"]) / j["close_a"] * 100.0
    # Avoid div-by-zero on zero-volume bars.
    j["vol_ratio"] = j["volume_y"] / j["volume_a"].replace(0, pd.NA)

    close_abs = j["close_pctdiff"].abs()
    vr = j["vol_ratio"].dropna()
    # Bars where volume disagrees by >10% — enough to flip the >=1.1 spike gate.
    flippable = ((vr < 0.9) | (vr > 1.1)).mean() * 100 if len(vr) else float("nan")

    print(f"  PRICE  close % diff:  mean {close_abs.mean():.3f}%   max {close_abs.max():.3f}%")
    print(f"  VOLUME yf/angel ratio: median {vr.median():.2f}   "
          f"p10 {vr.quantile(.1):.2f}   p90 {vr.quantile(.9):.2f}")
    print(f"  VOLUME bars off by >10% (could flip the spike gate): {flippable:.0f}%")
    # Show the scan-window bars the strategy actually keys on.
    for hhmm in ("11:00", "14:00"):
        h, m = map(int, hhmm.split(":"))
        row = j[[ (ts.hour, ts.minute) == (h, m) for ts in j.index]]
        if not row.empty:
            r = row.iloc[0]
            print(f"    scan {hhmm}: close a={r['close_a']:.2f} y={r['close_y']:.2f} | "
                  f"vol a={int(r['volume_a'])} y={int(r['volume_y'])} "
                  f"(ratio {r['vol_ratio']:.2f})" if pd.notna(r['vol_ratio']) else "")


def live_loop(symbols: list[str]) -> None:
    print("LIVE mode — Ctrl-C to stop. Comparing each source's latest bar + age.\n")
    while True:
        now = datetime.now(IST)
        today = now.date()
        print(f"--- {now:%H:%M:%S IST} ---")
        for sym in symbols:
            try:
                a = fetch_angel(sym, today, to_t=now.time())
                y = fetch_yf(sym, today)
                a_last = a.index[-1] if len(a) else None
                y_last = y.index[-1] if len(y) else None
                a_age = (now.replace(tzinfo=None) - a_last).total_seconds()/60 if a_last is not None else None
                y_age = (now.replace(tzinfo=None) - y_last).total_seconds()/60 if y_last is not None else None
                print(f"  {sym:11s} angel last {a_last} ({a_age:.0f}m) | "
                      f"yfinance last {y_last} ({y_age:.0f}m)")
            except Exception as exc:  # noqa: BLE001
                print(f"  {sym:11s} error: {exc!r}")
        _time.sleep(60)


def main(argv: list[str]) -> None:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    symbols = [s.upper() for s in args] or ["RELIANCE", "TCS", "INFY"]

    print(f"Compare Angel vs yfinance 5m bars. Symbols: {', '.join(symbols)}")
    print("Read-only; no DB, no orders.\n")

    if "--loop" in flags:
        live_loop(symbols)
        return

    today = datetime.now(IST).date()
    for sym in symbols:
        compare_day(sym, today)
    print("\nKey: PRICE diff should be tiny. If VOLUME ratio strays far from 1.0 or "
          "many bars are >10% off, yfinance volume is NOT interchangeable with Angel's "
          "for the S404 spike gate — fallback bars would change which entries fire.")


if __name__ == "__main__":
    main(sys.argv)
