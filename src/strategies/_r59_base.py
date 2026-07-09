"""Shared S447 chassis for the Round-59 strategies S455 / S505 / S525.

NOT a registered strategy — it exports no `STRATEGY`, so the auto-discovery registry
imports and skips it. The three files that DO export STRATEGY (s455_*, s505_*, s525_*)
derive from S447_BASE here, exactly mirroring the algo's dataclasses.replace chain
S447 → S455 (patience) → S505 (universe regime) → S525 (+ drawdown governor).

Every field below was dumped VERBATIM from the algo project's build_strategies()
(S447_s440_side4tier_22) so the vendored strategies are byte-for-byte the ones the algo
validated. The chassis is the S283 multi-mode adaptive-exit engine (same as the live
S404), differing only in the per-regime exit ladders: bull uses the wide BULL_XWIDE
ladder, sideways the 4-tier SIDE_4TIER_22 ladder, bear the S311 ladder.
"""

from __future__ import annotations

import dataclasses

from src.engine.v2_engine import ModeParams, StrategyV2

# ---- Depth-bucket exit ladders (min_depth_pct, ((sell_pct, frac), ...)) ----
# S311 ladder — the global adaptive default and the bear-mode ladder.
_S311_BUCKETS = (
    (0.0,  ((0.10, 0.5), (0.18, 1.0))),
    (8.0,  ((0.14, 0.5), (0.24, 1.0))),
    (15.0, ((0.18, 0.5), (0.32, 1.0))),
)
# BULL_XWIDE — wide bull ladder (S440 let deep bull entries run further).
_BULL_XWIDE = (
    (0.0,  ((0.12, 0.5), (0.20, 1.0))),
    (8.0,  ((0.16, 0.5), (0.28, 1.0))),
    (15.0, ((0.22, 0.5), (0.40, 1.0))),
)
# SIDE_4TIER_22 — 4-tier sideways ladder; entries >22% below the 90d high get the
# widest tier (S447's defining change over S440).
_SIDE_4TIER_22 = (
    (0.0,  ((0.08, 0.5), (0.15, 1.0))),
    (8.0,  ((0.11, 0.5), (0.19, 1.0))),
    (15.0, ((0.14, 0.5), (0.24, 1.0))),
    (22.0, ((0.18, 0.5), (0.32, 1.0))),
)


# S447 chassis — the shared base every Round-59 strategy below derives from.
S447_BASE = StrategyV2(
    name="S447_s440_side4tier_22",
    fall_threshold=-0.030,
    volume_spike_min=1.1,
    pyramid_levels=((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04)),
    pyramid_basis="avg",
    pyramid_volume_filter=True,
    exit_tiers=((0.15, 0.5), (0.25, 1.0)),
    adaptive_exit_by_depth=_S311_BUCKETS,
    allocation_mode="pct_equity",
    allocation_pct=0.16,
    scan_times=("11:00", "14:00"),
    macd_filter="positive",
    macd_filter_in_bear_market=True,
    regime_source="NIFTY_50",
    vix_bear_threshold=20.0,
    vix_only_bear=False,
    mode_params_bull=ModeParams(
        fall_threshold=-0.025, allocation_pct=0.18,
        exit_tiers=((0.15, 0.5), (0.26, 1.0)), volume_spike_min=1.1, macd_filter="__off__",
        adaptive_exit_by_depth=_BULL_XWIDE,
    ),
    mode_params_bear=ModeParams(
        fall_threshold=-0.030, allocation_pct=0.15,
        exit_tiers=((0.13, 0.5), (0.22, 1.0)), volume_spike_min=1.2,
        macd_filter="positive", sma_above_prev=20,
        adaptive_exit_by_depth=_S311_BUCKETS,
    ),
    mode_params_sideways=ModeParams(
        fall_threshold=-0.025, allocation_pct=0.16,
        exit_tiers=((0.13, 0.5), (0.23, 1.0)), volume_spike_min=1.1, macd_filter="__off__",
        adaptive_exit_by_depth=_SIDE_4TIER_22,
    ),
)

# S455 patience layer, shared by S455/S505/S525 (the algo's _patience(S447, 250, 0.12)).
PATIENCE_250_12 = dict(patience_sell_after_days=250, patience_sell_min_profit=0.12)
# S505 Round-59 regime levers, shared by S505/S525 (the algo's _r59 kwargs).
R59_UNI_VIXPCT_CRASH = dict(mode_regime_source="universe", mode_crash_overlay_pct=0.08,
                            mode_vix_percentile=0.80)


def derive(name: str, **overrides) -> StrategyV2:
    """S447_BASE with `name` + field overrides — the vendored equivalent of the algo's
    dataclasses.replace chain."""
    return dataclasses.replace(S447_BASE, name=name, **overrides)
