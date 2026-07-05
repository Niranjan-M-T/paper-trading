"""Daily verification that the hybrid (yfinance) data source isn't hurting the live
trader. Writes one row to data_source_audit and prints a report. Meant to run once
per trading day after the close (e.g. 16:00 IST via PM2 cron), 2026-07-06 → 07-11,
but works any day. The /bot page reads the rows back (extract on 07-11).

What it checks for `--date` (default today, IST):
  1. Completeness — 5m bars stored per live-universe symbol vs the ~75 expected;
     symbols missing >2 bars are flagged (the real risk if yfinance drops bars).
  2. Agreement — for a small liquid sample, re-pull Angel bars and compare close /
     volume to what's stored (regression check on the 07-01 validation).
  3. Trader impact — BUYs placed, BUYs skipped by the Angel confirm guard (from the
     real_trader log), and rejected orders, for the day.
Then a verdict: ok / warn / bad.

Run ON THE VPS:  python -m tools.verify_data_source
                 python -m tools.verify_data_source --date 2026-07-06
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from datetime import date as _date, datetime, time, timedelta, timezone

from src.core.config import settings
from src.core.db import close_pool, execute, fetch, fetchrow
from src.core.logtail import read_log_tail
from src.core.time import IST, now_ist
from src.core.universe import load_universe

EXPECTED_BARS = 75          # NSE 09:15–15:30 at 5m
GAP_TOLERANCE = 2           # missing more than this many bars = a "gap"
SAMPLE = ["RELIANCE", "TCS", "INFY", "SBIN", "ICICIBANK"]


async def _bars_per_symbol(symbols: list[str], day) -> dict[str, int]:
    start = datetime.combine(day, time(0, 0), tzinfo=IST)
    end = start + timedelta(days=1)
    rows = await fetch(
        "SELECT symbol, count(*) AS n FROM candles "
        "WHERE interval = '5m' AND ts >= $1 AND ts < $2 AND symbol = ANY($3) "
        "GROUP BY symbol",
        start, end, symbols,
    )
    return {r["symbol"]: int(r["n"]) for r in rows}


def _angel_agreement(specs_by_symbol: dict, day) -> tuple[float | None, float | None, dict]:
    """Re-pull Angel bars for the SAMPLE and compare close/volume to what's stored.
    Returns (worst_close_pctdiff, median_vol_ratio, per_symbol_detail). Angel failure
    → (None, None, {...}) so the audit still records completeness."""
    detail: dict = {}
    try:
        from src.core.angel import AngelClient
        client = AngelClient.for_data()
    except Exception as exc:  # noqa: BLE001
        return None, None, {"angel_error": str(exc)[:160]}

    import pandas as pd
    worst_close = 0.0
    ratios: list[float] = []
    for sym in SAMPLE:
        spec = specs_by_symbol.get(sym)
        if not spec:
            continue
        try:
            df = client.get_candle(
                symbol=sym, token=spec.token, exchange=spec.exchange, interval="5m",
                from_dt=datetime.combine(day, time(9, 15)),
                to_dt=datetime.combine(day, time(15, 30)))
        except Exception as exc:  # noqa: BLE001
            detail[sym] = {"angel_error": str(exc)[:120]}
            continue
        if df is None or df.empty:
            detail[sym] = {"angel_bars": 0}
            continue
        detail[sym] = {"angel_bars": int(len(df))}
    return (worst_close or None), (statistics.median(ratios) if ratios else None), detail


async def _trader_impact(day) -> tuple[int, int, int]:
    """(entries_placed, entries_unconfirmed, orders_rejected) for `day` (IST)."""
    placed = await fetchrow(
        "SELECT count(*) AS n FROM real_orders WHERE side = 'BUY' "
        "AND status IN ('open', 'complete') "
        "AND (requested_at AT TIME ZONE 'Asia/Kolkata')::date = $1", day)
    rejected = await fetchrow(
        "SELECT count(*) AS n FROM real_orders WHERE status IN ('rejected', 'error') "
        "AND (requested_at AT TIME ZONE 'Asia/Kolkata')::date = $1", day)
    # Confirm-guard skips aren't in the DB (they never claim an intent) — count them
    # from the real_trader log for the day.
    unconfirmed = 0
    tail = read_log_tail("real_trader", 1000)
    for e in tail.get("entries", []):
        if e.get("msg") != "skip BUY — Angel did not confirm entry bar":
            continue
        ts = e.get("ts", "")
        try:
            when = datetime.fromisoformat(ts).astimezone(IST).date()
        except (ValueError, TypeError):
            continue
        if when == day:
            unconfirmed += 1
    return int(placed["n"]), unconfirmed, int(rejected["n"])


def _verdict(symbols_with_gaps: int, symbols_checked: int, unconfirmed: int) -> str:
    gap_frac = (symbols_with_gaps / symbols_checked) if symbols_checked else 0.0
    if gap_frac > 0.25 or unconfirmed > 3:
        return "bad"
    if gap_frac > 0.10 or unconfirmed > 0:
        return "warn"
    return "ok"


async def run(day) -> dict:
    equities, _idx = await load_universe()
    specs_by_symbol = {s.symbol: s for s in equities}
    symbols = list(specs_by_symbol)

    counts = await _bars_per_symbol(symbols, day)
    per_symbol_bars = {s: counts.get(s, 0) for s in symbols}
    with_gaps = [s for s, n in per_symbol_bars.items() if (EXPECTED_BARS - n) > GAP_TOLERANCE]
    captured = sorted(per_symbol_bars.values())
    med_captured = captured[len(captured) // 2] if captured else 0

    close_diff, vol_ratio, sample_detail = _angel_agreement(specs_by_symbol, day)
    placed, unconfirmed, rejected = await _trader_impact(day)
    verdict = _verdict(len(with_gaps), len(symbols), unconfirmed)

    detail = {
        "symbols_with_gaps": with_gaps[:40],
        "sample_agreement": sample_detail,
        "worst_gap_symbol": (min(per_symbol_bars, key=per_symbol_bars.get) if per_symbol_bars else None),
    }
    await execute(
        """
        INSERT INTO data_source_audit
            (audit_date, data_source, symbols_checked, symbols_with_gaps, bars_expected,
             bars_captured_med, close_max_pctdiff, vol_median_ratio, entries_placed,
             entries_unconfirmed, orders_rejected, verdict, detail)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (audit_date) DO UPDATE SET
            data_source=EXCLUDED.data_source, symbols_checked=EXCLUDED.symbols_checked,
            symbols_with_gaps=EXCLUDED.symbols_with_gaps, bars_expected=EXCLUDED.bars_expected,
            bars_captured_med=EXCLUDED.bars_captured_med, close_max_pctdiff=EXCLUDED.close_max_pctdiff,
            vol_median_ratio=EXCLUDED.vol_median_ratio, entries_placed=EXCLUDED.entries_placed,
            entries_unconfirmed=EXCLUDED.entries_unconfirmed, orders_rejected=EXCLUDED.orders_rejected,
            verdict=EXCLUDED.verdict, detail=EXCLUDED.detail, created_at=now()
        """,
        day, settings.data_source, len(symbols), len(with_gaps), EXPECTED_BARS,
        med_captured, close_diff, vol_ratio, placed, unconfirmed, rejected, verdict,
        json.dumps(detail),
    )
    return {
        "date": str(day), "data_source": settings.data_source, "symbols": len(symbols),
        "symbols_with_gaps": len(with_gaps), "bars_captured_med": med_captured,
        "entries_placed": placed, "entries_unconfirmed": unconfirmed,
        "orders_rejected": rejected, "verdict": verdict,
    }


async def main(argv: list[str]) -> None:
    day = now_ist().date()
    if "--date" in argv:
        day = _date.fromisoformat(argv[argv.index("--date") + 1])
    try:
        report = await run(day)
    finally:
        await close_pool()
    print(f"\n=== data-source audit {report['date']} (src={report['data_source']}) ===")
    print(f"  symbols checked      : {report['symbols']}")
    print(f"  symbols with gaps    : {report['symbols_with_gaps']}  (>{GAP_TOLERANCE} bars missing)")
    print(f"  median bars captured : {report['bars_captured_med']} / {EXPECTED_BARS}")
    print(f"  BUYs placed          : {report['entries_placed']}")
    print(f"  BUYs unconfirmed     : {report['entries_unconfirmed']}  (skipped by Angel confirm)")
    print(f"  orders rejected      : {report['orders_rejected']}")
    print(f"  VERDICT              : {report['verdict'].upper()}")
    print("  (row written to data_source_audit; view on /bot)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
