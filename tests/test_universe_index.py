"""Tests for the equal-weight universe index + breadth builder (src/engine/universe_index).

These lock the construction to the algo's build_universe_index.py so the two agree where
their history overlaps (the parity contract for mode_regime_source="universe"). Real DB data
vs the algo CSV is checked on the VPS by tools/verify_universe_index.py.
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.engine.universe_index import (  # noqa: E402
    build_from_candles,
    build_universe_index,
    daily_close_matrix,
)


def _dates(n, start="2024-01-01"):
    return [d.date() for d in pd.date_range(start, periods=n, freq="D")]


# ---- build_universe_index: index level ----

def test_index_is_mean_return_cumprod_from_1000():
    # Two symbols both +10%/day → mean return 10%/day → 1000, 1100, 1210.
    idx = _dates(3)
    wide = pd.DataFrame({"A": [100.0, 110.0, 121.0], "B": [50.0, 55.0, 60.5]}, index=idx)
    close, _ = build_universe_index(wide)
    assert close.iloc[0] == 1000.0
    assert close.iloc[1] == pytest_approx(1100.0)
    assert close.iloc[2] == pytest_approx(1210.0)


def test_extreme_return_is_clipped_to_50pct():
    # A triples (+200% → clipped to +50%), B flat (0%). Mean = +25% → 1000, 1250.
    idx = _dates(2)
    wide = pd.DataFrame({"A": [100.0, 300.0], "B": [100.0, 100.0]}, index=idx)
    close, _ = build_universe_index(wide)
    assert close.iloc[1] == pytest_approx(1250.0)


def test_late_listed_symbol_contributes_only_its_own_returns():
    # B lists on day 2 (NaN before). Its first available day has no return (NaN → skipped),
    # so day-2 mean return = A's return alone; B's own returns count from day 3.
    idx = _dates(3)
    wide = pd.DataFrame({"A": [100.0, 110.0, 121.0], "B": [np.nan, 200.0, 220.0]}, index=idx)
    close, _ = build_universe_index(wide)
    # day2: only A has a return (+10%) → index 1100. day3: A +10%, B +10% → mean +10% → 1210.
    assert close.iloc[1] == pytest_approx(1100.0)
    assert close.iloc[2] == pytest_approx(1210.0)


# ---- build_universe_index: breadth ----

def test_breadth_all_uptrend_is_one_all_downtrend_is_zero():
    idx = _dates(60)
    up = pd.DataFrame({s: np.linspace(100, 200, 60) for s in ("A", "B", "C")}, index=idx)
    down = pd.DataFrame({s: np.linspace(200, 100, 60) for s in ("A", "B", "C")}, index=idx)
    _, b_up = build_universe_index(up)
    _, b_down = build_universe_index(down)
    assert b_up.iloc[-1] == pytest_approx(1.0)
    assert b_down.iloc[-1] == pytest_approx(0.0)


def test_breadth_is_neutral_before_any_50dma_exists():
    # Fewer than 30 rows → no symbol has a 50-DMA yet → neutral 0.5.
    idx = _dates(10)
    wide = pd.DataFrame({"A": np.linspace(100, 120, 10), "B": np.linspace(100, 80, 10)}, index=idx)
    _, breadth = build_universe_index(wide)
    assert (breadth == 0.5).all()


def test_breadth_fraction_mixed():
    # 4 symbols: 3 up, 1 down → last-day breadth 0.75.
    idx = _dates(60)
    cols = {"A": np.linspace(100, 200, 60), "B": np.linspace(100, 180, 60),
            "C": np.linspace(100, 160, 60), "D": np.linspace(200, 100, 60)}
    _, breadth = build_universe_index(pd.DataFrame(cols, index=idx))
    assert breadth.iloc[-1] == pytest_approx(0.75)


# ---- daily_close_matrix: 5m → daily ----

def test_daily_close_matrix_takes_last_close_per_day():
    rows = []
    for day, closes in [("2024-01-01", [100, 101, 102]), ("2024-01-02", [103, 104])]:
        for i, c in enumerate(closes):
            rows.append({"symbol": "A", "date": pd.Timestamp(day).date(),
                         "timestamp": pd.Timestamp(f"{day} 09:{15 + 5*i:02d}"), "close": float(c)})
    df = pd.DataFrame(rows)
    wide = daily_close_matrix(df)
    assert wide.loc[pd.Timestamp("2024-01-01").date(), "A"] == 102.0  # last 5m close
    assert wide.loc[pd.Timestamp("2024-01-02").date(), "A"] == 104.0


def test_build_from_candles_empty_is_empty():
    close, breadth = build_from_candles(pd.DataFrame())
    assert close.empty and breadth.empty
    close2, breadth2 = build_universe_index(pd.DataFrame())
    assert close2.empty and breadth2.empty


# tiny local approx helper (avoids importing pytest.approx by name at module top)
def pytest_approx(x, tol=1e-6):
    class _A:
        def __eq__(self, other):
            return abs(other - x) <= tol
    return _A()
