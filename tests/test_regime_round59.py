"""Tests for the Round-59 regime levers ported into the vendored engine for S505/S525:
mode_regime_source (universe/breadth), mode_crash_overlay_pct, mode_vix_percentile,
mode_hysteresis_days. Parity (defaults = legacy) is covered by the rest of the suite
staying green; these prove the NEW behaviour actually fires. dd_governor is exercised
end-to-end by the S505/S525 parity harness (Phase 4), not here.
"""

from __future__ import annotations

import math
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

from src.engine.v2_engine import (  # noqa: E402
    classify_regime_by_date,
    clear_regime_cache,
    prime_regime_index,
)


def _series(values, start="2023-06-01") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series([float(v) for v in values], index=[d.date() for d in idx], dtype=float)


def _transitions(r: pd.Series) -> int:
    v = r.values
    return int((v[1:] != v[:-1]).sum())


def test_universe_source_reads_universe_not_nifty():
    # NIFTY in a downtrend, the traded universe in an uptrend — the whole point of
    # mode_regime_source="universe" is to follow the universe, not the large-cap index.
    clear_regime_cache()
    prime_regime_index("NIFTY_50", _series(np.linspace(200, 100, 130)))
    prime_regime_index("UNIVERSE", _series(np.linspace(100, 200, 130)))

    uni = classify_regime_by_date(source="universe")
    nif = classify_regime_by_date(source="NIFTY_50")
    assert uni.iloc[-1] == "bull"   # universe uptrend
    assert nif.iloc[-1] == "bear"   # legacy source unchanged


def test_crash_overlay_forces_bear_on_a_non_bear_day():
    # A steady uptrend that dips ~12% on the final day: the slow DMA rules haven't
    # turned bear yet (they lag), but the fast crash overlay must catch the drawdown.
    clear_regime_cache()
    vals = list(np.linspace(100, 160, 90))
    vals[-1] = 160 * 0.88
    prime_regime_index("UNIVERSE", _series(vals))

    base = classify_regime_by_date(source="universe")
    crash = classify_regime_by_date(source="universe", crash_overlay_pct=0.08)
    assert base.iloc[-1] != "bear"    # DMA lag: not yet bear
    assert crash.iloc[-1] == "bear"   # overlay catches the fast drawdown


def test_vix_percentile_fires_where_a_fixed_threshold_would_not():
    # VIX sits calm (~12) then spikes to 40. A fixed threshold of 50 never triggers,
    # but 40 is far above its own rolling-252d 80th percentile → percentile fires.
    clear_regime_cache()
    prime_regime_index("UNIVERSE", _series(np.linspace(100, 180, 300)))  # uptrend
    prime_regime_index("INDIA_VIX", _series([12.0] * 290 + [40.0] * 10))

    pct = classify_regime_by_date(source="universe", vix_only_bear=True, vix_percentile=0.80)
    fixed = classify_regime_by_date(source="universe", vix_only_bear=True,
                                    vix_bear_threshold=50.0, vix_percentile=None)
    assert pct.iloc[-1] == "bear"    # 40 > rolling p80 (~12)
    assert fixed.iloc[-1] != "bear"  # 40 < fixed 50


def test_hysteresis_never_increases_transitions():
    # A whipsawing universe: hysteresis can only reduce (or equal) the number of
    # regime flips, never add them — and there must be flips to smooth in the first place.
    clear_regime_cache()
    prime_regime_index("UNIVERSE", _series([130 + 15 * math.sin(i / 3.0) for i in range(200)]))

    raw = classify_regime_by_date(source="universe", hysteresis_days=0)
    smoothed = classify_regime_by_date(source="universe", hysteresis_days=5)
    assert _transitions(raw) > 0
    assert _transitions(smoothed) <= _transitions(raw)


def test_legacy_defaults_unchanged():
    # The default call path (NIFTY_50, no overlays) must be identical to a bare call —
    # this is the parity contract that keeps the live S404 untouched.
    clear_regime_cache()
    prime_regime_index("NIFTY_50", _series(np.linspace(100, 150, 120)))
    a = classify_regime_by_date()
    b = classify_regime_by_date(source="NIFTY_50", hysteresis_days=0,
                                crash_overlay_pct=None, vix_percentile=None)
    assert list(a.values) == list(b.values)
    assert a.iloc[-1] == "bull"
