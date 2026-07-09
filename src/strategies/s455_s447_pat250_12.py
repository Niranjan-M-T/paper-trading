"""S455_s447_pat250_12 — the S447 chassis + patience-sell (250 days / +12%).

Algo definition: _patience(S447, 250, 0.12) — after 250 trading days, sell a still-
profitable position (>=+12% unrealized) that hasn't reached its next exit tier, to
recycle capital out of long-horizon creepers. Round-56 grid winner (best return/DD
tradeoff on the patience chassis). No regime-source change vs S447 (still NIFTY_50).

`starting_cash` is bound to the portfolio's capital by the trader at replay time.
"""

from src.strategies._r59_base import PATIENCE_250_12, derive

STRATEGY = derive("S455_s447_pat250_12", **PATIENCE_250_12)

DESCRIPTION = (
    "S447 chassis (S283 multi-mode adaptive exits: BULL_XWIDE / S311 / SIDE_4TIER_22) "
    "plus patience-sell after 250 days at >=+12%. Round-56 return/drawdown winner."
)
