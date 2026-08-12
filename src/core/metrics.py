"""Portfolio performance metrics shared by the dashboard + portfolio detail.

Estimated APY annualises the *current* return so far via CAGR (compound annual
growth rate). It is an extrapolation, not a guarantee — and it is wild for the
first few days of live trading (a +2% move over 5 days annualises to a silly
number), so we return None until the portfolio has at least `MIN_DAYS_LIVE` days
of history. The UI shows "—" in that warm-up window.
"""

from __future__ import annotations

from datetime import datetime

from src.core.time import now_ist


# Below this many days live, CAGR extrapolation is noise — show "—" instead.
MIN_DAYS_LIVE = 7


def days_live(started_at: datetime | None) -> int:
    """Whole days since a portfolio started running (0 if unknown or in the future).

    Powers the "days running" counter — it answers "how much return in how much
    time?" alongside the P&L figures. started_at is tz-aware (DB UTC); now_ist() is
    tz-aware IST, so the subtraction is tz-safe."""
    if not started_at:
        return 0
    days = (now_ist() - started_at).total_seconds() / 86400.0
    return max(0, int(days))


def split_pnl(net_worth: float, invested: float, unrealized: float) -> dict:
    """Decompose total P&L into realized + unrealized for the live account.

    `net_worth` = free cash + current market value of holdings; `invested` = the cost
    basis actually deployed (sum of SIP deposits); `unrealized` = the broker's
    mark-to-market on open holdings (Σ real_holdings.pnl). Then:

        total_pnl = net_worth − invested
        realized  = total_pnl − unrealized   (booked gains from closed trades + fees)

    `pct` is total P&L over the amount invested. Returns a dict of floats (pct is
    None when nothing has been invested yet)."""
    total = net_worth - invested
    realized = total - unrealized
    pct = (total / invested * 100.0) if invested > 0 else None
    return {
        "invested": invested,
        "net_worth": net_worth,
        "total_pnl": total,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "pct": pct,
    }


def estimated_apy(equity: float, capital: float, started_at: datetime | None) -> float | None:
    """CAGR as a percent (e.g. 42.5 == +42.5%/yr), or None while warming up.

    CAGR = (equity / capital) ** (365 / days_live) − 1, expressed in percent.
    Returns None when inputs are unusable (non-positive capital/equity, no
    started_at, or fewer than MIN_DAYS_LIVE days of history).
    """
    if not started_at or capital <= 0 or equity <= 0:
        return None
    # started_at is tz-aware (DB UTC); now_ist() is tz-aware IST — subtraction is tz-safe.
    days_live = (now_ist() - started_at).total_seconds() / 86400.0
    if days_live < MIN_DAYS_LIVE:
        return None
    growth = equity / capital
    cagr = growth ** (365.0 / days_live) - 1.0
    return cagr * 100.0
