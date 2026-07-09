"""Lock the vendored S455/S505/S525 configs to the algo's (Phase 3 of the S505 port).

A field-by-field diff against the live algo project confirmed these are byte-for-byte
the algo's strategies (see the port notes). This test freezes the salient fields so a
later edit to the shared _r59_base chassis can't silently drift the live-bound strategy.
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

from src.strategies.registry import get, names  # noqa: E402

_BULL_XWIDE = ((0.0, ((0.12, 0.5), (0.20, 1.0))), (8.0, ((0.16, 0.5), (0.28, 1.0))),
               (15.0, ((0.22, 0.5), (0.40, 1.0))))
_SIDE_4TIER_22 = ((0.0, ((0.08, 0.5), (0.15, 1.0))), (8.0, ((0.11, 0.5), (0.19, 1.0))),
                  (15.0, ((0.14, 0.5), (0.24, 1.0))), (22.0, ((0.18, 0.5), (0.32, 1.0))))
_S311 = ((0.0, ((0.10, 0.5), (0.18, 1.0))), (8.0, ((0.14, 0.5), (0.24, 1.0))),
         (15.0, ((0.18, 0.5), (0.32, 1.0))))


def test_all_three_registered():
    reg = set(names())
    assert {"S455_s447_pat250_12", "S505_pat_uni_vixpct_crash", "S525_s505_ddgov"} <= reg
    # S447 base is a helper, NOT a registered strategy.
    assert "S447_s440_side4tier_22" not in reg


def test_shared_s447_chassis():
    for name in ("S455_s447_pat250_12", "S505_pat_uni_vixpct_crash", "S525_s505_ddgov"):
        s = get(name)
        assert s.fall_threshold == -0.030
        assert s.allocation_mode == "pct_equity"
        assert s.allocation_pct == 0.16
        assert s.scan_times == ("11:00", "14:00")
        assert s.pyramid_levels == ((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04))
        assert s.macd_filter == "positive" and s.macd_filter_in_bear_market is True
        assert s.adaptive_exit_by_depth == _S311
        assert s.mode_params_bull.adaptive_exit_by_depth == _BULL_XWIDE
        assert s.mode_params_bear.adaptive_exit_by_depth == _S311
        assert s.mode_params_sideways.adaptive_exit_by_depth == _SIDE_4TIER_22
        # All three carry the S455 patience layer.
        assert s.patience_sell_after_days == 250
        assert s.patience_sell_min_profit == 0.12


def test_s455_is_nifty_source_no_round59():
    s = get("S455_s447_pat250_12")
    assert s.mode_regime_source == "NIFTY_50"      # S455 keeps the legacy regime source
    assert s.mode_crash_overlay_pct is None
    assert s.mode_vix_percentile is None
    assert s.dd_governor_threshold is None


def test_s505_round59_levers():
    s = get("S505_pat_uni_vixpct_crash")
    assert s.mode_regime_source == "universe"
    assert s.mode_crash_overlay_pct == 0.08
    assert s.mode_vix_percentile == 0.80
    assert s.dd_governor_threshold is None          # S505 has NO governor (that's S525)


def test_s525_adds_dd_governor_on_top_of_s505():
    s = get("S525_s505_ddgov")
    # Everything S505 has …
    assert s.mode_regime_source == "universe"
    assert s.mode_crash_overlay_pct == 0.08
    assert s.mode_vix_percentile == 0.80
    # … plus the drawdown governor.
    assert s.dd_governor_threshold == 0.15
    assert s.dd_governor_scale == 0.5
