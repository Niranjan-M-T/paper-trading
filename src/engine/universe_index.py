"""Equal-weight universe index + breadth, built from the traded universe's own candles.

This is the paper-trading port of the algo project's build_universe_index.py. It feeds
classify_regime_by_date(source="universe"|"breadth") — the Round-59 regime source S505/S525
use instead of large-cap NIFTY-50 (which mislabels the mid/small-cap universe's bear years).

Construction (IDENTICAL to the algo, so the two agree where their history overlaps):
  close   = equal-weight index from the MEAN daily return across all symbols trading that
            day, cumulated from 1000 (composition-neutral: a late-listed symbol contributes
            only its own returns from its first day, no join/rebase bias). Daily returns are
            clipped to +/-50% so an unadjusted split / bad tick can't corrupt the regime signal.
  breadth = fraction of symbols (that have a 50-DMA that day) whose close > their own 50-DMA.

The algo reads real daily bars; the paper trader has only 5m equity bars, so we take each
symbol's LAST 5m close per day as its daily close (≈ the official close for liquid names).
Only RELATIVE structure matters to the classifier — close vs its own DMAs, and breadth as a
fraction — so the absolute index level (and thus a different history start) is irrelevant to
the regime LABELS. tools/verify_universe_index.py checks the labels against the algo CSV.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Columns the builder emits (mirrors the algo CSV, plus nothing else).
INDEX_START = 1000.0
RET_CLIP = 0.5          # clip |daily return| to this before averaging (split/bad-tick guard)
BREADTH_DMA = 50        # DMA window for the breadth count
BREADTH_MIN_PERIODS = 30  # a symbol needs this many closes before it has a 50-DMA
BREADTH_NEUTRAL = 0.5   # breadth before any symbol has 50d of history


def daily_close_matrix(candles: pd.DataFrame) -> pd.DataFrame:
    """Long candle frame (needs columns: symbol, date, close, timestamp) → a wide daily
    last-close matrix (index=date, columns=symbol). Empty frame → empty matrix.

    `date` is the trading date; the LAST close of the day (by timestamp) is that symbol's
    daily close, matching how a daily bar's close is the session's final print.
    """
    need = {"symbol", "date", "close", "timestamp"}
    if candles is None or candles.empty or not need.issubset(candles.columns):
        return pd.DataFrame()
    d = candles.sort_values("timestamp")
    daily = d.groupby(["date", "symbol"], sort=True)["close"].last().reset_index()
    wide = daily.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    return wide.sort_index()


def build_universe_index(daily_close: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Wide daily-close matrix (index=date, cols=symbol) → (index_close, breadth), each a
    Series indexed by date. Matches algo build_universe_index.build_universe_index().

    Empty input → two empty Series (callers degrade to no universe regime).
    """
    if daily_close is None or daily_close.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    # --- Equal-weight index via mean daily return (composition-neutral) ---
    rets = daily_close.pct_change(fill_method=None).clip(lower=-RET_CLIP, upper=RET_CLIP)
    mean_ret = rets.mean(axis=1, skipna=True).fillna(0.0)
    close = INDEX_START * (1.0 + mean_ret).cumprod()

    # --- Breadth: fraction of symbols above their own 50-DMA ---
    dma50 = daily_close.rolling(BREADTH_DMA, min_periods=BREADTH_MIN_PERIODS).mean()
    above = daily_close > dma50
    valid = dma50.notna()  # only count symbols that HAVE a 50-DMA that day
    breadth = (above & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    breadth = breadth.fillna(BREADTH_NEUTRAL)

    close.name = "close"
    breadth.name = "breadth"
    return close, breadth


def build_from_candles(candles: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Convenience: long 5m candle frame → (index_close, breadth). Used by the live trader
    (prime the regime cache) and the validation tool."""
    return build_universe_index(daily_close_matrix(candles))
