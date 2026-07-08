"""Batch 5-minute NSE candle fetch from Yahoo Finance (yfinance).

The bulk data source for the hybrid poller. Validated 2026-07-01 to match Angel's
bars to within a handful of shares (see tools/compare_db_vs_yf.py) — so it drops
into the engine unchanged. One batched download replaces the poller's 73 sequential
Angel calls, which is what removes the rate-limit storm.

Output schema is exactly what the poller's upsert_bars() and the engine expect:
    columns: timestamp (tz-aware IST), symbol, open, high, low, close, volume
`symbol` is the ORIGINAL universe symbol (e.g. "JASH-EQ"), not the ".NS" ticker.

The pure normaliser (`normalize_yf_frame`) is unit-tested; the thin live fetch
(`fetch_5m`) is exercised by tools/probe_data_sources.py against the real API.
"""

from __future__ import annotations

import logging
import re

import pandas as pd

from src.core.time import IST

# yfinance logs EVERY unresolved ticker in a batch at ERROR level ("possibly
# delisted; no price data found") plus an "N Failed downloads:" summary. At the
# 09:15 open Yahoo often hasn't published the first 5m bar yet, so a batch
# legitimately misses a chunk of (even liquid) symbols for a cycle or two before
# it self-heals — and thin names miss quiet windows all day. The poller's Angel
# top-up/failover heals the DATA; real coverage problems surface via the poller's
# own coverage warning + the daily audit. So silence yfinance's redundant
# per-symbol chatter to keep the debug bundle actionable (was ~98 events/48h).
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Strip NSE series suffixes to get the Yahoo base symbol; ".NS" = Yahoo's NSE suffix.
_SUFFIX = re.compile(r"-(EQ|BE|BZ|SM|ST)$")

_OUT_COLS = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]


def yf_ticker(symbol: str) -> str:
    """Universe symbol -> Yahoo ticker. 'RELIANCE' -> 'RELIANCE.NS', 'JASH-EQ' -> 'JASH.NS'."""
    return f"{_SUFFIX.sub('', symbol)}.NS"


def normalize_yf_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalise a yfinance OHLCV frame to the engine schema for `symbol`.

    Accepts any column casing (Open/open), a tz-aware or tz-naive DatetimeIndex,
    and yfinance's extra columns (Adj Close / Dividends / Stock Splits). Drops rows
    with no close (Yahoo emits NaN rows for gaps) and coerces volume to int.
    Returns an empty, correctly-columned frame if there's nothing usable.
    """
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=_OUT_COLS)

    df = raw.rename(columns=str.lower)
    needed = {"open", "high", "low", "close", "volume"}
    if not needed.issubset(df.columns):
        return pd.DataFrame(columns=_OUT_COLS)

    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)

    out = pd.DataFrame({
        "timestamp": idx,
        "symbol": symbol,
        "open": pd.to_numeric(df["open"].values, errors="coerce"),
        "high": pd.to_numeric(df["high"].values, errors="coerce"),
        "low": pd.to_numeric(df["low"].values, errors="coerce"),
        "close": pd.to_numeric(df["close"].values, errors="coerce"),
        "volume": pd.to_numeric(df["volume"].values, errors="coerce"),
    })
    out = out.dropna(subset=["close"])
    out["volume"] = out["volume"].fillna(0).astype("int64")
    return out[_OUT_COLS].reset_index(drop=True)


def _slice_ticker(batch: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Pull one ticker's OHLCV frame out of a yf.download() result.

    yf.download(group_by='ticker') returns MultiIndex columns (ticker, field) for
    multiple tickers, or flat columns for a single ticker."""
    if isinstance(batch.columns, pd.MultiIndex):
        if ticker not in batch.columns.get_level_values(0):
            return pd.DataFrame()
        return batch[ticker]
    return batch  # single-ticker download → already flat


def fetch_5m(symbols: list[str], period: str = "1d") -> pd.DataFrame:
    """Batch-fetch 5m bars for many symbols in one download. Returns a single long
    frame (all symbols concatenated) in the engine schema. Network call — not used
    in unit tests. Symbols that Yahoo can't resolve are simply absent from output."""
    import yfinance as yf

    if not symbols:
        return pd.DataFrame(columns=_OUT_COLS)

    ticker_to_symbol = {yf_ticker(s): s for s in symbols}
    tickers = list(ticker_to_symbol)
    batch = yf.download(
        tickers, period=period, interval="5m",
        group_by="ticker", auto_adjust=False, progress=False, threads=True,
    )
    if batch is None or len(batch) == 0:
        return pd.DataFrame(columns=_OUT_COLS)

    frames = []
    for ticker, symbol in ticker_to_symbol.items():
        one = _slice_ticker(batch, ticker)
        norm = normalize_yf_frame(one, symbol)
        if not norm.empty:
            frames.append(norm)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=_OUT_COLS)
