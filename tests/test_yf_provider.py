"""Tests for the yfinance bulk provider's pure normaliser (src/core/yf_provider).

We don't hit the network here — the live batch fetch is covered by
tools/probe_data_sources.py. These lock the schema contract the poller relies on:
the normalised frame must be a drop-in for upsert_bars().
"""

from __future__ import annotations

import os

os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use")

import pandas as pd  # noqa: E402

from src.core.time import IST  # noqa: E402
from src.core.yf_provider import normalize_yf_frame, yf_ticker  # noqa: E402


# ---- yf_ticker ----

def test_yf_ticker_plain_and_suffixed():
    assert yf_ticker("RELIANCE") == "RELIANCE.NS"
    assert yf_ticker("JASH-EQ") == "JASH.NS"
    assert yf_ticker("EQUITASBNK-EQ") == "EQUITASBNK.NS"
    assert yf_ticker("SOMENAME-BE") == "SOMENAME.NS"


# ---- normalize_yf_frame ----

def _raw(tz="Asia/Kolkata"):
    """A yfinance-shaped frame: capitalised cols, tz-aware index, extra columns."""
    idx = pd.to_datetime(["2026-07-01 09:15", "2026-07-01 09:20", "2026-07-01 09:25"])
    idx = idx.tz_localize(tz)
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [100.5, 101.5, 102.5],
            "Low": [99.5, 100.5, 101.5],
            "Close": [100.2, 101.2, 102.2],
            "Adj Close": [100.2, 101.2, 102.2],
            "Volume": [1000, 2000, 3000],
            "Dividends": [0, 0, 0],
            "Stock Splits": [0, 0, 0],
        },
        index=idx,
    )


def test_normalize_maps_schema_and_symbol():
    out = normalize_yf_frame(_raw(), "RELIANCE")
    assert list(out.columns) == ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
    assert (out["symbol"] == "RELIANCE").all()
    assert len(out) == 3
    assert out["close"].tolist() == [100.2, 101.2, 102.2]


def test_normalize_index_is_ist_tzaware():
    out = normalize_yf_frame(_raw(), "TCS")
    ts = out["timestamp"].iloc[0]
    assert ts.tzinfo is not None
    assert ts.utcoffset() == IST.utcoffset(None)  # +05:30
    assert (ts.hour, ts.minute) == (9, 15)


def test_normalize_converts_utc_index_to_ist():
    # A UTC-indexed frame at 03:45 UTC must land on the 09:15 IST bar (+05:30).
    idx = pd.to_datetime(["2026-07-01 03:45", "2026-07-01 03:50"]).tz_localize("UTC")
    raw = pd.DataFrame(
        {"Open": [1.0, 1.0], "High": [1.0, 1.0], "Low": [1.0, 1.0],
         "Close": [1.0, 1.0], "Volume": [10, 20]}, index=idx,
    )
    out = normalize_yf_frame(raw, "INFY")
    ts = out["timestamp"].iloc[0]
    assert (ts.hour, ts.minute) == (9, 15)


def test_normalize_volume_is_int():
    out = normalize_yf_frame(_raw(), "SBIN")
    assert out["volume"].dtype == "int64"
    assert out["volume"].tolist() == [1000, 2000, 3000]


def test_normalize_drops_nan_close_rows():
    raw = _raw()
    raw.loc[raw.index[1], "Close"] = float("nan")  # a gap bar
    out = normalize_yf_frame(raw, "RELIANCE")
    assert len(out) == 2  # the NaN-close bar is dropped


def test_normalize_empty_or_missing_columns_returns_empty_schema():
    empty = normalize_yf_frame(pd.DataFrame(), "X")
    assert list(empty.columns) == ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
    assert len(empty) == 0
    # A frame missing OHLCV columns is treated as unusable, not a crash.
    junk = normalize_yf_frame(pd.DataFrame({"foo": [1]}, index=pd.to_datetime(["2026-07-01"])), "X")
    assert len(junk) == 0
