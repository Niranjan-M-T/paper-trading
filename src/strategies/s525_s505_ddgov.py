"""S525_s505_ddgov — S505 plus a drawdown governor.

Algo definition: _r59(S455, mode_regime_source="universe", mode_vix_percentile=0.80,
mode_crash_overlay_pct=0.08, dd_governor_threshold=0.15, dd_governor_scale=0.5) — i.e.
S505 with the added rule that while account equity is more than 15% below its
high-water mark, new entries are half-sized (de-risk in drawdowns).

`starting_cash` is bound to the portfolio's capital by the trader at replay time.
"""

from src.strategies._r59_base import PATIENCE_250_12, R59_UNI_VIXPCT_CRASH, derive

STRATEGY = derive(
    "S525_s505_ddgov",
    **PATIENCE_250_12, **R59_UNI_VIXPCT_CRASH,
    dd_governor_threshold=0.15, dd_governor_scale=0.5,
)

DESCRIPTION = (
    "S505 (patience + universe regime + VIX-percentile + crash overlay) plus a drawdown "
    "governor: half-size new entries while equity is >15% below its high-water mark."
)
