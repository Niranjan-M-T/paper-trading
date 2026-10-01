"""Weekly WhatsApp digest for the live real-money account. Meant to run once a week after Friday's
close (15:35 IST via the PM2 cron app `paperaglo-weekly-digest`); replaces the daily digest.

What it sends (whatsapp.format_weekly_digest), for the ISO week (Mon–Sun, IST) containing "now":
  * net worth, the week's P&L, total P&L (+%) on invested, realized/unrealized — the SAME math as
    /api/bot/stats (split_pnl over opening capital + deposits), so the digest and the dashboard can
    never disagree;
  * deposits/withdrawals recorded this week (they move invested, not P&L);
  * every fill this week, bot and manual, with buy/sell totals;
  * if the bot is ON and bought nothing all week, the one-line "why not" as of the last trading day
    (real_executor.entry_verdict over the trader's entry_diagnostics), plus what the shadow logger
    says the strategy wanted this week;
  * a warning if the trader hasn't synced the broker recently (the figures would be stale).

Send-once per ISO week: a real_signals 'INFO' row keyed digest:week:<%G-W%V> (the same ledger the
other alerts use). Its `price` column stores that week's deposit-adjusted total P&L, so next week's
figure is a plain difference of two snapshots. A failed send leaves sent_ok FALSE → a re-run retries.

Run ON THE VPS:  python -m tools.weekly_digest
                 python -m tools.weekly_digest --dry-run    # print only: no send, no record
                 python -m tools.weekly_digest --force      # send outside the Fri-close→Sun window
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, time, timedelta

from src.core import whatsapp
from src.core.config import settings
from src.core.db import close_pool, execute, fetch, fetchrow
from src.core.metrics import live_account_xirr, split_pnl
from src.core.time import IST, MARKET_CLOSE, now_ist
from src.engine.real_executor import entry_verdict

WEEKLY_REASON = "weekly digest"


def week_key(now: datetime) -> str:
    return f"digest:week:{now:%G-W%V}"  # zero-padded → lexicographic order == time order


def in_send_window(now: datetime) -> bool:
    """Friday after the close, or the weekend. PM2 also runs a cron_restart app on every start /
    restart / reboot-resurrect, so without this a Mon–Thu deploy would send a half-week digest and
    mark the week sent (skipping the real one). A weekend resurrect is fine — send-once dedups it."""
    return now.weekday() >= 5 or (now.weekday() == 4 and now.time() >= MARKET_CLOSE)


def _last_weekday(d: date) -> date:
    return d if d.weekday() < 5 else d - timedelta(days=d.weekday() - 4)


async def build_digest(now: datetime) -> tuple[dict, float]:
    """Gather the week's facts → (formatter input, total P&L to snapshot)."""
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    start = datetime.combine(monday, time(0), IST)
    end = start + timedelta(days=7)
    s: dict = {"week_label": f"{monday:%d %b} – {monday + timedelta(days=4):%d %b %Y}"}

    funds = await fetchrow(
        "SELECT available_cash::float8 AS cash, as_of FROM real_funds ORDER BY as_of DESC LIMIT 1")
    as_of = funds["as_of"].astimezone(IST) if funds and funds["as_of"] else None
    s["last_sync"] = as_of.strftime("%a %d %b %H:%M IST") if as_of else None
    # The trader syncs every weekday it runs (holidays included), so a sync older than the last
    # weekday means it's down and every figure below is stale.
    if as_of is None:
        s["sync_warning"] = "The trader has never synced with the broker — figures are empty."
    elif as_of.date() < monday:
        s["sync_warning"] = (f"No broker sync this week (last: {s['last_sync']}) — figures are stale; "
                             f"check that paperaglo-real-trader is running.")
    elif as_of.date() < _last_weekday(today):
        s["sync_warning"] = (f"Last broker sync was {s['last_sync']} — check that "
                             f"paperaglo-real-trader is running.")

    # Same inputs + math as /api/bot/stats.
    hv = await fetchrow(
        "SELECT COALESCE(SUM(qty * COALESCE(ltp, avg_price)), 0)::float8 AS v, "
        "COALESCE(SUM(pnl), 0)::float8 AS pnl FROM real_holdings")
    dep_rows = await fetch("SELECT ts, amount::float8 AS amount FROM real_deposits ORDER BY ts")
    cash = float(funds["cash"]) if funds and funds["cash"] is not None else 0.0
    holdings_value = float(hv["v"]) if hv else 0.0
    unrealized = float(hv["pnl"]) if hv else 0.0
    invested = settings.real_opening_capital + sum(float(r["amount"]) for r in dep_rows)
    pnl = split_pnl(cash + holdings_value, invested, unrealized)
    s.update(cash=cash, holdings_value=holdings_value, pnl=pnl)
    # Same money-weighted XIRR as /api/bot/stats (opening capital at the live portfolio's start).
    live = await fetchrow("SELECT started_at FROM portfolios WHERE live = TRUE ORDER BY id LIMIT 1")
    s["xirr_pct"] = live_account_xirr(
        live["started_at"] if live else None, settings.real_opening_capital,
        [(r["ts"], float(r["amount"])) for r in dep_rows], pnl["net_worth"], at=now)

    # Week P&L = change in the deposit-adjusted total since the previous weekly snapshot (a deposit
    # moves net worth and invested equally, so it cancels instead of showing up as profit).
    prev = await fetchrow(
        "SELECT price::float8 AS total, created_at FROM real_signals "
        "WHERE side = 'INFO' AND reason = $1 AND signal_key LIKE 'digest:week:%' AND signal_key < $2 "
        "ORDER BY signal_key DESC LIMIT 1",
        WEEKLY_REASON, week_key(now))
    if prev:
        s["week_pnl"] = pnl["total_pnl"] - float(prev["total"])
        s["week_since"] = prev["created_at"].astimezone(IST).strftime("%a %d %b")

    wk_dep = await fetchrow(
        "SELECT COALESCE(SUM(amount), 0)::float8 AS total FROM real_deposits WHERE ts >= $1 AND ts < $2",
        start, end)
    s["deposits_week"] = float(wk_dep["total"]) if wk_dep else 0.0

    bot_fills = await fetch(
        "SELECT updated_at AS ts, side, symbol, filled_qty AS qty, avg_fill_price::float8 AS price "
        "FROM real_orders WHERE status = 'complete' AND filled_qty > 0 "
        "AND updated_at >= $1 AND updated_at < $2", start, end)
    manual_fills = await fetch(
        "SELECT order_ts AS ts, side, symbol, qty, avg_price::float8 AS price FROM manual_trades "
        "WHERE order_ts >= $1 AND order_ts < $2", start, end)
    fills = [(r, "bot") for r in bot_fills] + [(r, "manual") for r in manual_fills]
    fills.sort(key=lambda x: x[0]["ts"])
    s["fills"] = [{"day": r["ts"].astimezone(IST).strftime("%a"), "side": str(r["side"]).upper(),
                   "symbol": r["symbol"], "qty": r["qty"], "price": r["price"] or 0.0, "source": src}
                  for r, src in fills]

    bot = await fetchrow("SELECT enabled FROM real_bot_state WHERE id = 1")
    s["bot_enabled"] = bool(bot["enabled"]) if bot else False

    # The "why" + shadow parts are extras: a missing sql/016/017 table must not kill the digest.
    pf = await fetchrow("SELECT id FROM portfolios WHERE live = TRUE ORDER BY id LIMIT 1")
    if pf:
        try:
            drow = await fetchrow("SELECT payload FROM entry_diagnostics WHERE portfolio_id = $1", pf["id"])
            diag = drow["payload"] if drow else None
            if isinstance(diag, str):  # no jsonb codec — asyncpg returns text
                diag = json.loads(diag)
            if diag and diag.get("date"):
                # Judge the diagnostics as of THEIR trading day's close (they're written only while
                # the trader runs), so a Friday-evening or weekend send still gets a real answer.
                ref = diag["date"]
                code, headline = entry_verdict(
                    diag, bot_enabled=s["bot_enabled"], today_str=ref,
                    now_hhmm=now.strftime("%H:%M") if ref == today.isoformat() else f"{MARKET_CLOSE:%H:%M}",
                    is_weekday=True)
                if code not in ("nodata", "off", "bought"):
                    s["verdict"] = headline
        except Exception as exc:  # noqa: BLE001
            print(f"  (entry diagnostics unavailable: {exc})")
        try:
            rows = await fetch(
                "SELECT symbol FROM shadow_buy_points WHERE portfolio_id = $1 "
                "AND signal_date >= $2 AND signal_date < $3 "
                "GROUP BY symbol ORDER BY MIN(signal_date), symbol",
                pf["id"], monday, monday + timedelta(days=7))  # DATE params must be date objects
            s["wanted"] = [r["symbol"] for r in rows]
        except Exception as exc:  # noqa: BLE001
            print(f"  (shadow buy-points unavailable: {exc})")
    return s, pnl["total_pnl"]


async def main(argv: list[str]) -> None:
    dry = "--dry-run" in argv
    now = now_ist()
    key = week_key(now)
    if not dry and "--force" not in argv and not in_send_window(now):
        print(f"{now:%a %H:%M} IST is outside the digest window (Friday after "
              f"{MARKET_CLOSE:%H:%M} → Sunday) — skipping. Use --dry-run to preview or --force to send.")
        return
    try:
        if not dry:
            done = await fetchrow("SELECT sent_ok FROM real_signals WHERE signal_key = $1", key)
            if done and done["sent_ok"]:
                print(f"{key} already sent — nothing to do.")
                return
        s, total = await build_digest(now)
        text = whatsapp.format_weekly_digest(s)
        print(text)
        if dry:
            print("\n(dry run — not sent, not recorded)")
            return
        delivered = await whatsapp.broadcast(text)
        # price = the week's total-P&L snapshot, for next week's "this week" line.
        await execute(
            """
            INSERT INTO real_signals
                (signal_key, portfolio_id, symbol, side, qty, price, reason, placeable, note, sent_ok, targets)
            VALUES ($1, NULL, 'DIGEST', 'INFO', 0, $2, $3, FALSE, $4, $5, $6)
            ON CONFLICT (signal_key) DO UPDATE
              SET price = EXCLUDED.price, note = EXCLUDED.note,
                  sent_ok = real_signals.sent_ok OR EXCLUDED.sent_ok,
                  targets = GREATEST(real_signals.targets, EXCLUDED.targets)
            """,
            key, total, WEEKLY_REASON, text[:500], delivered > 0, delivered,
        )
        print(f"\nsent to {delivered} target(s)" if delivered else "\nNOT delivered (WhatsApp unconfigured or down) — a re-run retries")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
