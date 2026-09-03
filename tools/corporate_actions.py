"""Manage the corporate_actions table (sql/014) that drives split/bonus back-adjustment.

The live/paper engine stores RAW prices and back-adjusts at read time from this table, so
recording a split here makes the rolling features (90d high, volume_avg20, ATR) continuous
across the ex-date — killing the false deep-dip entries / phantom volume spikes a split
otherwise causes. Adjustment is non-destructive (stored candles stay raw), so a wrong row
is reversed with `--disable`.

Usage (run on the VPS, in the venv):

  # Pull splits/bonuses from yfinance for the whole enabled equity universe (last N years).
  # Records them; the engine picks them up on the next tick. Review with --list first.
  python -m tools.corporate_actions --detect --years 3

  python -m tools.corporate_actions --list
  python -m tools.corporate_actions --add RELIANCE 2024-10-28 5 --type split --note "5:1"
  python -m tools.corporate_actions --disable 7        # reverse a wrong row (keeps the record)

yfinance's `.splits` reports both splits and bonus issues as a ratio (5.0 = 5:1 split,
2.0 = 1:1 bonus), which is exactly the back-adjust factor we need. Detection is UPSERT with
ON CONFLICT DO NOTHING, so re-running never clobbers a manual edit.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta

from src.core.db import close_pool, fetch, get_pool
from src.core.yf_provider import yf_ticker


async def _universe_symbols() -> list[str]:
    rows = await fetch(
        "SELECT symbol FROM universe_symbols WHERE enabled AND kind = 'equity' ORDER BY symbol"
    )
    return [r["symbol"] for r in rows]


async def _upsert(symbol: str, ex_date: date, ratio: float, action_type: str,
                  source: str, note: str | None) -> bool:
    row = await fetch(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, ratio, source, note)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (symbol, ex_date, action_type) DO NOTHING
        RETURNING id
        """,
        symbol, ex_date, action_type, float(ratio), source, note,
    )
    return bool(row)


async def cmd_detect(years: int) -> None:
    import yfinance as yf  # local import: only --detect needs the network dep

    symbols = await _universe_symbols()
    cutoff = date.today() - timedelta(days=365 * years)
    print(f"Scanning {len(symbols)} symbols for splits/bonuses since {cutoff} …")
    added = 0
    for sym in symbols:
        try:
            splits = yf.Ticker(yf_ticker(sym)).splits  # pd.Series: ex-date -> ratio
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {sym}: fetch failed ({str(exc)[:80]})")
            continue
        if splits is None or len(splits) == 0:
            continue
        for ts, ratio in splits.items():
            ex = ts.date() if hasattr(ts, "date") else ts
            if ex < cutoff or not ratio or float(ratio) <= 0:
                continue
            if await _upsert(sym, ex, float(ratio), "split", "yfinance",
                             f"yfinance split {float(ratio):g}"):
                added += 1
                print(f"  + {sym}  {ex}  ratio {float(ratio):g}")
    print(f"Done. {added} new corporate action(s) recorded. Review with --list.")


async def cmd_list() -> None:
    rows = await fetch(
        "SELECT id, symbol, ex_date, action_type, ratio::float8 AS ratio, source, active, note "
        "FROM corporate_actions ORDER BY ex_date DESC, symbol"
    )
    if not rows:
        print("(no corporate actions recorded)")
        return
    print(f"{'id':>4}  {'symbol':<12} {'ex_date':<11} {'type':<6} {'ratio':>7}  {'act':<3} source/note")
    for r in rows:
        flag = "on" if r["active"] else "OFF"
        print(f"{r['id']:>4}  {r['symbol']:<12} {str(r['ex_date']):<11} {r['action_type']:<6} "
              f"{r['ratio']:>7.4g}  {flag:<3} {r['source'] or ''} {r['note'] or ''}")


async def cmd_add(symbol: str, ex_date: str, ratio: float, action_type: str, note: str | None) -> None:
    ex = datetime.strptime(ex_date, "%Y-%m-%d").date()
    ok = await _upsert(symbol.upper(), ex, ratio, action_type, "manual", note)
    print(f"{'added' if ok else 'already present'}: {symbol.upper()} {ex} {action_type} ratio {ratio:g}")


async def cmd_disable(action_id: int) -> None:
    row = await fetch("UPDATE corporate_actions SET active = FALSE WHERE id = $1 RETURNING symbol, ex_date",
                      action_id)
    print(f"disabled id {action_id}" if row else f"no row with id {action_id}")


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manage split/bonus back-adjustment rows.")
    p.add_argument("--detect", action="store_true", help="pull splits/bonuses from yfinance")
    p.add_argument("--years", type=int, default=3, help="how far back --detect looks (default 3)")
    p.add_argument("--list", action="store_true", help="print recorded corporate actions")
    p.add_argument("--add", nargs=3, metavar=("SYMBOL", "EX_DATE", "RATIO"),
                   help="manually add one action, e.g. --add RELIANCE 2024-10-28 5")
    p.add_argument("--type", default="split", choices=("split", "bonus"), help="action type for --add")
    p.add_argument("--note", default=None, help="note for --add")
    p.add_argument("--disable", type=int, metavar="ID", help="deactivate a row (reverse a wrong entry)")
    return p.parse_args(argv)


async def main(argv: list[str]) -> None:
    args = _parse(argv)
    await get_pool()
    try:
        if args.detect:
            await cmd_detect(args.years)
        if args.add:
            sym, ex, ratio = args.add
            await cmd_add(sym, ex, float(ratio), args.type, args.note)
        if args.disable is not None:
            await cmd_disable(args.disable)
        if args.list or not (args.detect or args.add or args.disable is not None):
            await cmd_list()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
