"""Standalone probe for FALLBACK market-data sources. Touches nothing in production.

The live poller fetches 5-minute NSE equity candles from Angel One and gets badly
rate-limited on one account. Before we build a multi-source failover, we must prove
— from the SAME IP the poller runs on (i.e. run this on the VPS too) — whether each
candidate library can actually return usable, fresh 5-minute OHLCV for NSE equities.

This script imports NO repo modules and needs no .env / DB / Angel creds, so it runs
anywhere. It only READS public data. It changes nothing.

Run:
    pip install yfinance openchart nsepython
    python -m tools.probe_data_sources               # default: RELIANCE, TCS
    python -m tools.probe_data_sources INFY SBIN

For each provider it reports: installed? ok/fail (+ the real error), row count,
columns, index timezone, first & last bar, the latest bar's AGE vs now (freshness —
critical for a live bot), and elapsed seconds.

What we already expect (to be confirmed empirically):
  - yfinance  : Yahoo, "SYMBOL.NS", 5m intraday capped to last ~60 days. Reachable
                from non-India IPs (not NSE-hosted), but may be delayed ~15 min.
  - openchart : NSE's own charting API, 5m, no API key. Best data fidelity vs Angel,
                BUT hits NSE directly → a datacenter/VPS IP may be blocked.
  - nsepython : equity_history is DAILY-only (no 5m). Probed only to (a) confirm that
                and (b) double-check raw NSE reachability from this IP.
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST)


def _describe(df, *, tz_expected: str = "") -> dict:
    """Summarise a returned OHLCV DataFrame without assuming exact column names."""
    info: dict = {"rows": int(len(df)), "columns": list(df.columns)}
    if len(df) == 0:
        return info
    idx = df.index
    info["index_tz"] = str(getattr(idx, "tz", None))
    try:
        last_ts = idx[-1].to_pydatetime()
        first_ts = idx[0].to_pydatetime()
        if last_ts.tzinfo is None:
            # Treat a naive index as IST for the freshness estimate, but flag it.
            last_ts = last_ts.replace(tzinfo=IST)
            info["index_naive"] = True
        age_min = (_now_ist() - last_ts.astimezone(IST)).total_seconds() / 60
        info["first_bar"] = str(first_ts)
        info["last_bar"] = str(last_ts)
        info["last_bar_age_min"] = round(age_min, 1)
    except Exception as exc:  # noqa: BLE001
        info["describe_err"] = repr(exc)
    return info


def probe_yfinance(symbol: str) -> dict:
    t0 = time.monotonic()
    try:
        import yfinance as yf
    except ImportError:
        return {"provider": "yfinance", "installed": False,
                "hint": "pip install yfinance"}
    try:
        tkr = yf.Ticker(f"{symbol}.NS")
        df = tkr.history(period="5d", interval="5m", auto_adjust=False)
        ok = df is not None and len(df) > 0
        return {"provider": "yfinance", "installed": True, "ok": ok,
                "elapsed_s": round(time.monotonic() - t0, 2),
                **(_describe(df) if ok else {"note": "empty frame returned"})}
    except Exception as exc:  # noqa: BLE001
        return {"provider": "yfinance", "installed": True, "ok": False,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "error": repr(exc), "trace": traceback.format_exc(limit=3)}


def probe_openchart(symbol: str) -> dict:
    t0 = time.monotonic()
    try:
        from openchart import NSEData
    except ImportError:
        return {"provider": "openchart", "installed": False,
                "hint": "pip install openchart"}
    try:
        nse = NSEData()
        end = datetime.now()
        start = end - timedelta(days=5)
        # Confirmed signature (openchart>=…): historical(symbol, segment, start, end, interval).
        # segment must be one of IDX / EQ / FO. search(symbol, 'EQ') loads the master.
        df = nse.historical(symbol, segment="EQ", start=start, end=end, interval="5m")
        if df is None or len(df) == 0:
            # It responds (no exception) but returns nothing — NSE's chart endpoint
            # either rejected this IP or didn't resolve the symbol. This is the
            # known flakiness; the VPS run tells us if it's IP-blocked there too.
            return {"provider": "openchart", "installed": True, "ok": False,
                    "elapsed_s": round(time.monotonic() - t0, 2),
                    "note": "reached NSE but returned 0 rows (IP block or symbol resolution)"}
        return {"provider": "openchart", "installed": True, "ok": True,
                "elapsed_s": round(time.monotonic() - t0, 2), **_describe(df)}
    except Exception as exc:  # noqa: BLE001
        return {"provider": "openchart", "installed": True, "ok": False,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "error": repr(exc), "trace": traceback.format_exc(limit=3)}


def probe_nsepython(symbol: str) -> dict:
    """Confirm daily-only + raw NSE reachability from this IP. NOT a 5m candidate."""
    t0 = time.monotonic()
    try:
        from nsepython import equity_history
    except ImportError:
        return {"provider": "nsepython", "installed": False,
                "hint": "pip install nsepython"}
    try:
        end = datetime.now()
        start = end - timedelta(days=10)
        df = equity_history(symbol, "EQ", start.strftime("%d-%m-%Y"),
                            end.strftime("%d-%m-%Y"))
        ok = df is not None and len(df) > 0
        return {"provider": "nsepython", "installed": True, "ok": ok,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "granularity": "DAILY only (no 5m) — reachability check",
                **({"rows": int(len(df)), "columns": list(df.columns)[:8]} if ok
                   else {"note": "empty/blocked — NSE may be rejecting this IP"})}
    except Exception as exc:  # noqa: BLE001
        return {"provider": "nsepython", "installed": True, "ok": False,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "granularity": "DAILY only (no 5m)",
                "error": repr(exc), "trace": traceback.format_exc(limit=3)}


def _print_report(symbol: str, results: list[dict]) -> None:
    print(f"\n{'='*70}\n  {symbol}.NS  —  probed at {_now_ist():%Y-%m-%d %H:%M:%S IST}\n{'='*70}")
    for r in results:
        head = f"  [{r['provider']}]"
        if not r.get("installed", True):
            print(f"{head} NOT INSTALLED — {r.get('hint','')}")
            continue
        verdict = "OK  " if r.get("ok") else "FAIL"
        print(f"{head} {verdict}  ({r.get('elapsed_s','?')}s)")
        for k, v in r.items():
            if k in ("provider", "installed", "ok", "elapsed_s"):
                continue
            print(f"        {k}: {v}")


def main(argv: list[str]) -> None:
    symbols = [s.upper() for s in argv[1:]] or ["RELIANCE", "TCS"]
    print("Probing fallback data sources (read-only; touches no production code).")
    print(f"Symbols: {', '.join(symbols)}")
    for sym in symbols:
        results = [probe_yfinance(sym), probe_openchart(sym), probe_nsepython(sym)]
        _print_report(sym, results)
    print("\nDone. Run this on the VPS too — NSE/Yahoo may treat the VPS IP differently.")


if __name__ == "__main__":
    main(sys.argv)
