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


def xirr(flows: list[tuple[datetime, float]]) -> float | None:
    """Annualised money-weighted return (XIRR) as a percent, or None. Pure.

    `flows` are (when, amount) from the INVESTOR's side: money put in is negative, money taken
    out (withdrawals, and the terminal value) positive. Solves Σ amount / (1+r)^(years since the
    first flow) = 0 by bisection — for the usual shape (outflows, then a positive terminal value)
    NPV falls monotonically in r, so bisection always converges where Newton can overshoot. None
    when there's nothing to solve: no outflow or no inflow, or no sign change on the bracket."""
    if not flows or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    t0 = min(t for t, _ in flows)
    pts = [((t - t0).total_seconds() / (365.0 * 86400.0), float(a)) for t, a in flows]

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** y for y, a in pts)

    lo, hi = -0.9999, 1.0
    while npv(hi) > 0 and hi < 1e6:  # widen until the root is bracketed
        hi *= 2.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0 * 100.0


def live_account_xirr(started_at: datetime | None, opening_capital: float,
                      deposits: list[tuple[datetime, float]], net_worth: float,
                      at: datetime | None = None) -> float | None:
    """XIRR (percent/yr) of the live SIP account: the opening capital goes in at `started_at`,
    each recorded deposit at its timestamp (a negative amount = withdrawal = money back out), and
    today's net worth comes out at `at`. Unlike estimated_apy — which treats every rupee as if it
    were invested on day one, so a fresh SIP drags the rate down — this weights each deposit by how
    long it has actually been working. None for the first MIN_DAYS_LIVE days (annualising a few
    days is noise, same rule as estimated_apy) or when there's no value to measure."""
    at = at or now_ist()
    if not started_at or net_worth <= 0 or (at - started_at).total_seconds() / 86400.0 < MIN_DAYS_LIVE:
        return None
    flows = [(started_at, -float(opening_capital))]
    flows += [(ts, -float(amt)) for ts, amt in deposits if amt]
    flows.append((at, float(net_worth)))
    return xirr(flows)
