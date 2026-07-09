"""S505_pat_uni_vixpct_crash — the Round-59 champion, and the strategy the live bot
is being migrated to.

Algo definition: _r59(S455, mode_regime_source="universe", mode_vix_percentile=0.80,
mode_crash_overlay_pct=0.08). It takes the S455 patience chassis and swaps the regime
detector from large-cap NIFTY-50 to the traded universe's own equal-weight index +
breadth (fixing the diagnosed benchmark mismatch that mislabelled 2018/2019/2025 bears
as bull), with a rolling-252d VIX-percentile fear threshold and an 8% fast-crash overlay.

Requires the trader to prime the UNIVERSE / UNIVERSE_BREADTH regime series (built from
the DB's 5m candles — see src/engine/universe_index and real_trader). Do NOT point a
real-money portfolio at this until the parity + shadow validation passes.

`starting_cash` is bound to the portfolio's capital by the trader at replay time.
"""

from src.strategies._r59_base import PATIENCE_250_12, R59_UNI_VIXPCT_CRASH, derive

STRATEGY = derive("S505_pat_uni_vixpct_crash", **PATIENCE_250_12, **R59_UNI_VIXPCT_CRASH)

DESCRIPTION = (
    "S455 patience chassis + universe-breadth regime source, rolling-252d VIX-percentile "
    "fear threshold (0.80), and an 8% fast-crash overlay. Round-59 best-levers champion."
)
