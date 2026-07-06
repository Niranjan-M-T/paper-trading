"""Tests for the poller's yfinance-coverage decision helpers — the pure logic that
drives the two Angel fallbacks:

  * whole-cycle FAILOVER  (coverage below threshold -> run the poll via Angel)
  * per-symbol TOP-UP     (symbols with ZERO bars -> re-pull just those from Angel)

No network / DB here; the live fetch + upsert are covered by tools/probe_data_sources.py
and exercised end-to-end on the VPS. These lock the classification: a symbol with a
partial no-trade gap is COVERED (Angel can't fill it either); only a symbol yfinance
returned nothing for is MISSING (a real fetch/resolve failure worth an Angel call).
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

from src.runners.poller import _coverage, _covered_symbols, _symbols_missing  # noqa: E402


def _long(bar_counts: dict) -> pd.DataFrame:
    """Build a long yfinance-shaped frame: `bar_counts` maps symbol -> #bars stored."""
    rows = []
    for sym, n in bar_counts.items():
        for i in range(n):
            rows.append({"timestamp": pd.Timestamp("2026-07-06 09:15") + pd.Timedelta(minutes=5 * i),
                         "symbol": sym, "open": 1.0, "high": 1.0, "low": 1.0,
                         "close": 1.0, "volume": 10})
    return pd.DataFrame(rows, columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])


# ---- _covered_symbols ----

def test_covered_symbols_from_batch():
    df = _long({"RELIANCE": 75, "TCS": 75, "DPWIRES": 71})
    assert _covered_symbols(df) == {"RELIANCE", "TCS", "DPWIRES"}


def test_covered_symbols_empty_batch():
    assert _covered_symbols(pd.DataFrame()) == set()
    assert _covered_symbols(None) == set()


# ---- _coverage ----

def test_coverage_full():
    symbols = ["RELIANCE", "TCS", "INFY"]
    assert _coverage(symbols, _long({s: 75 for s in symbols})) == 1.0


def test_coverage_partial_fraction():
    symbols = ["RELIANCE", "TCS", "INFY", "SBIN"]  # 4 asked, 2 returned
    assert _coverage(symbols, _long({"RELIANCE": 75, "TCS": 75})) == 0.5


def test_coverage_empty_batch_is_zero():
    assert _coverage(["RELIANCE", "TCS"], pd.DataFrame()) == 0.0


def test_coverage_empty_universe_is_one():
    # No symbols asked -> nothing to fail over about.
    assert _coverage([], _long({})) == 1.0


# ---- _symbols_missing ----

def test_missing_only_zero_bar_symbols():
    # DPWIRES has a partial gap (71 bars) — it's COVERED, not missing.
    # NOTATICKER returned nothing — that's the real fetch failure to top up.
    symbols = ["RELIANCE", "DPWIRES", "NOTATICKER"]
    df = _long({"RELIANCE": 75, "DPWIRES": 71})
    assert _symbols_missing(symbols, df) == ["NOTATICKER"]


def test_missing_preserves_universe_order():
    symbols = ["A", "B", "C", "D"]
    df = _long({"B": 75})
    assert _symbols_missing(symbols, df) == ["A", "C", "D"]


def test_missing_all_when_batch_empty():
    symbols = ["RELIANCE", "TCS"]
    assert _symbols_missing(symbols, pd.DataFrame()) == ["RELIANCE", "TCS"]


def test_missing_none_when_fully_covered():
    symbols = ["RELIANCE", "TCS"]
    assert _symbols_missing(symbols, _long({"RELIANCE": 1, "TCS": 75})) == []
