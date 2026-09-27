"""End-of-day WhatsApp digest for the live real-money account. Meant to run once per weekday
after the close (15:35 IST via the PM2 cron app `paperaglo-daily-digest`), but works any time.

What it sends (whatsapp.format_daily_digest):
  * net worth, the P&L since the previous digest, total P&L (+%) on invested, realized/unrealized
    — the SAME math as /api/bot/stats (split_pnl over opening capital + deposits), so the digest
    and the dashboard can never disagree;
  * today's fills, bot and manual;
  * if the bot is ON and bought nothing, the one-line "why not" (real_executor.entry_verdict over
    the trader's entry_diagnostics), plus what the shadow logger says the strategy wanted.
Holiday (no candles today) and "trader never synced today" each get a short heads-up instead.

Send-once per day: a real_signals 'INFO' row keyed digest:<date> (the same ledger the other alerts
use). Its `price` column stores that day's deposit-adjusted total P&L, so tomorrow's "since" figure
is a plain difference of two snapshots. A failed send leaves sent_ok FALSE, so a re-run retries.

Run ON THE VPS:  python -m tools.daily_digest
                 python -m tools.daily_digest --dry-run    # print only: no send, no record
                 python -m tools.daily_digest --force      # send outside the after-close window
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, time, timedelta

from src.core import whatsapp
from src.core.config import settings
from src.core.db import close_pool, execute, fetch, fetchrow
from src.core.metrics import split_pnl
from src.core.time import IST, MARKET_CLOSE, now_ist
from src.engine.real_executor import entry_verdict

DIGEST_REASON = "daily digest"


async def build_digest(now: datetime) -> tuple[dict, float | None]:
    """Gather the digest facts for `now`'s IST day → (formatter input, total P&L to snapshot)."""
    day = now.date()
    start = datetime.combine(day, time(0), IST)
    end = start + timedelta(days=1)
    s: dict = {"date_label": now.strftime("%a %d %b %Y")}

    bars = await fetchrow(
        "SELECT 1 AS x FROM candles WHERE interval = '5m' AND ts >= $1 AND ts < $2 LIMIT 1", start, end)
    s["market_data"] = bars is not None

    funds = await fetchrow(
        "SELECT available_cash::float8 AS cash, as_of FROM real_funds ORDER BY as_of DESC LIMIT 1")
    as_of = funds["as_of"] if funds else None
    s["last_sync"] = as_of.astimezone(IST).strftime("%d %b %H:%M IST") if as_of else None
    s["synced"] = bool(as_of and as_of.astimezone(IST).date() == day)
    if not (s["market_data"] and s["synced"]):
        return s, None

    # Same inputs + math as /api/bot/stats.
    hv = await fetchrow(
        "SELECT COALESCE(SUM(qty * COALESCE(ltp, avg_price)), 0)::float8 AS v, "
        "COALESCE(SUM(pnl), 0)::float8 AS pnl FROM real_holdings")
    dep = await fetchrow("SELECT COALESCE(SUM(amount), 0)::float8 AS total FROM real_deposits")
    cash = float(funds["cash"]) if funds["cash"] is not None else 0.0
    holdings_value = float(hv["v"]) if hv else 0.0
    unrealized = float(hv["pnl"]) if hv else 0.0
    invested = settings.real_opening_capital + (float(dep["total"]) if dep and dep["total"] else 0.0)
    pnl = split_pnl(cash + holdings_value, invested, unrealized)
    s.update(cash=cash, holdings_value=holdings_value, pnl=pnl)

    # Day P&L = change in the deposit-adjusted total since the last SYNCED digest (a deposit moves
    # net worth and invested equally, so it cancels out instead of showing up as profit).
    prev = await fetchrow(
        "SELECT signal_key, price::float8 AS total FROM real_signals "
        "WHERE side = 'INFO' AND reason = $1 AND signal_key LIKE 'digest:%' AND signal_key < $2 "
        "ORDER BY signal_key DESC LIMIT 1",
        DIGEST_REASON, f"digest:{day.isoformat()}")
    if prev:
        prev_day = datetime.fromisoformat(prev["signal_key"].split(":", 1)[1])
        s["day_pnl"] = pnl["total_pnl"] - float(prev["total"])
        s["day_since"] = prev_day.strftime("%a %d %b")

    bot_fills = await fetch(
        "SELECT side, symbol, filled_qty AS qty, avg_fill_price::float8 AS price FROM real_orders "
        "WHERE status = 'complete' AND filled_qty > 0 AND updated_at >= $1 AND updated_at < $2 "
        "ORDER BY updated_at", start, end)
    manual_fills = await fetch(
        "SELECT side, symbol, qty, avg_price::float8 AS price FROM manual_trades "
        "WHERE order_ts >= $1 AND order_ts < $2 ORDER BY order_ts", start, end)
    s["fills"] = (
        [{"side": str(r["side"]).upper(), "symbol": r["symbol"], "qty": r["qty"],
          "price": r["price"] or 0.0, "source": "bot"} for r in bot_fills]
        + [{"side": str(r["side"]).upper(), "symbol": r["symbol"], "qty": r["qty"],
            "price": r["price"] or 0.0, "source": "manual"} for r in manual_fills])

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
            code, headline = entry_verdict(
                diag, bot_enabled=s["bot_enabled"], today_str=day.isoformat(),
                now_hhmm=now.strftime("%H:%M"), is_weekday=day.weekday() < 5)
            if code not in ("nodata", "off", "bought"):
                s["verdict"] = headline
        except Exception as exc:  # noqa: BLE001
            print(f"  (entry diagnostics unavailable: {exc})")
        try:
            rows = await fetch(
                "SELECT DISTINCT symbol FROM shadow_buy_points WHERE portfolio_id = $1 AND signal_date = $2 "
                "ORDER BY symbol", pf["id"], day)  # DATE param must be a date object
            s["wanted"] = [r["symbol"] for r in rows]
        except Exception as exc:  # noqa: BLE001
            print(f"  (shadow buy-points unavailable: {exc})")
    return s, pnl["total_pnl"]


async def main(argv: list[str]) -> None:
    dry = "--dry-run" in argv
    now = now_ist()
    key = f"digest:{now.date().isoformat()}"
    # PM2 also runs a cron_restart app once on every `pm2 start` / restart / reboot-resurrect, not
    # just on the schedule. Without this, a mid-day deploy would send a half-day digest and mark
    # the date sent (skipping the real one), and a weekend resurrect would report a "holiday".
    if not dry and "--force" not in argv and (now.weekday() >= 5 or now.time() < MARKET_CLOSE):
        print(f"{now:%a %H:%M} IST is outside the digest window (weekdays after "
              f"{MARKET_CLOSE:%H:%M}) — skipping. Use --dry-run to preview or --force to send.")
        return
    try:
        if not dry:
            done = await fetchrow("SELECT sent_ok FROM real_signals WHERE signal_key = $1", key)
            if done and done["sent_ok"]:
                print(f"{key} already sent — nothing to do.")
                return
        s, total = await build_digest(now)
        text = whatsapp.format_daily_digest(s)
        print(text)
        if dry:
            print("\n(dry run — not sent, not recorded)")
            return
        delivered = await whatsapp.broadcast(text)
        # price = the day's total-P&L snapshot for tomorrow's "since" line; only a synced digest
        # carries one (reason differs), so a holiday/no-sync day is skipped by the lookup.
        await execute(
            """
            INSERT INTO real_signals
                (signal_key, portfolio_id, symbol, side, qty, price, reason, placeable, note, sent_ok, targets)
            VALUES ($1, NULL, 'DIGEST', 'INFO', 0, $2, $3, FALSE, $4, $5, $6)
            ON CONFLICT (signal_key) DO UPDATE
              SET price = EXCLUDED.price, reason = EXCLUDED.reason, note = EXCLUDED.note,
                  sent_ok = real_signals.sent_ok OR EXCLUDED.sent_ok,
                  targets = GREATEST(real_signals.targets, EXCLUDED.targets)
            """,
            key, total if total is not None else 0.0,
            DIGEST_REASON if total is not None else f"{DIGEST_REASON} (no data)",
            text[:500], delivered > 0, delivered,
        )
        print(f"\nsent to {delivered} target(s)" if delivered else "\nNOT delivered (WhatsApp unconfigured or down) — a re-run retries")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
