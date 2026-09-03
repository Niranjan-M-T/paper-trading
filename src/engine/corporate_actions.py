"""Read-time split / bonus back-adjustment for the candle series.

The poller stores RAW OHLCV, so a split/bonus leaves a discontinuity that corrupts every
rolling feature spanning it (90-day high → false deep-dip entries, volume_avg20 → phantom
volume spike, ATR/SMAs) and a held position's exit basis. This module back-adjusts the
loaded window at READ time from the `corporate_actions` table (sql/014), so it is:

  * non-destructive — the stored candles stay raw, orders still place at the real current
    price, and a wrong `corporate_actions` row is reversed by flipping its `active` flag;
  * a no-op with zero cost when the table has nothing for the loaded symbols; and
  * consistent for live AND paper (both go through replay.load_candles_window).

The adjustment rule (see real_executor.cumulative_split_factor, the pure core) mirrors
yfinance auto_adjust=True — the same thing the algo backtest uses — so the live/paper series
lines up with what the strategies were validated on. Wired into replay.load_candles_window.
"""

from __future__ import annotations

import pandas as pd

from src.core.db import fetch
from src.engine.real_executor import cumulative_split_factor


async def load_active_actions(symbols: list[str]) -> dict[str, list[tuple]]:
    """Active corporate actions for `symbols`, as {symbol: [(ex_date, ratio), …]}.

    Empty dict when the table has nothing for these symbols — the caller then skips the
    adjustment entirely, so there is no cost until a real split/bonus is recorded."""
    if not symbols:
        return {}
    rows = await fetch(
        "SELECT symbol, ex_date, ratio::float8 AS ratio FROM corporate_actions "
        "WHERE active AND symbol = ANY($1::text[])",
        list(symbols),
    )
    out: dict[str, list[tuple]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append((r["ex_date"], float(r["ratio"])))
    return out


def adjust_frame(df: pd.DataFrame, actions_by_symbol: dict[str, list[tuple]]) -> pd.DataFrame:
    """Back-adjust an OHLCV frame in place for splits/bonuses (non-destructive to the DB).

    `df` has columns symbol, date, open, high, low, close, volume (the shape
    replay.load_candles_window builds). For each symbol with actions, a bar strictly before
    an ex-date has its prices divided by the compounded ratio and its volume multiplied — so
    the pre- and post-action bars sit in the same (current) price/volume space. Bars on/after
    every ex-date are untouched. Returns the (possibly adjusted) frame."""
    if df.empty or not actions_by_symbol:
        return df
    factor = pd.Series(1.0, index=df.index)
    for sym, acts in actions_by_symbol.items():
        sym_mask = df["symbol"] == sym
        if not sym_mask.any():
            continue
        # One divisor per row = product of the ratios whose ex-date is after that bar.
        factor.loc[sym_mask] = df.loc[sym_mask, "date"].map(
            lambda d, a=acts: cumulative_split_factor(d, a)
        )
    if bool((factor == 1.0).all()):
        return df  # every loaded bar is on/after its actions — nothing to adjust
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] / factor
    df["volume"] = (df["volume"] * factor).round().astype("int64")
    return df
