"""WhatsApp signal fan-out via the Evolution API gateway.

The live real-money bot forwards its BUY/SELL signals to one or more WhatsApp
groups (the `wa_targets` table, editable from /bot) so they can be actioned by
hand — the point is to catch what the bot itself can't execute (e.g. AB4036
surveillance stocks the exchange hard-blocks).

Everything here is best-effort and DEFENSIVE: a gateway outage logs a warning and
returns a falsy result, and never raises into the trading tick. The gateway api
key controls the whole WhatsApp account, so it comes from settings/.env only and
is never written to the DB or logs.
"""

from __future__ import annotations

import logging

import httpx

from src.core.config import settings
from src.core.db import fetch

log = logging.getLogger("core.whatsapp")

# The seeded default group ("Stonks S525 trader signals"). Kept here for reference /
# tests; the live target list is the wa_targets table (sql/012), editable from /bot.
DEFAULT_GROUP_JID = "120363411936940548@g.us"


def configured() -> bool:
    """True when the gateway is switched on and has an api key. Sends no-op otherwise."""
    return bool(settings.wa_enabled and settings.wa_api_key)


def _url(path: str) -> str:
    return f"{settings.wa_gateway_url}/{path.lstrip('/')}"


def _headers() -> dict:
    return {"apikey": settings.wa_api_key or "", "Content-Type": "application/json"}


async def send_text(jid: str, text: str) -> bool:
    """POST one text message to a JID (group '…@g.us' or user '…@s.whatsapp.net').

    Returns True on a 200/201, False otherwise. Never raises — a WhatsApp problem
    must not break trading."""
    if not configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _url(f"/message/sendText/{settings.wa_instance}"),
                headers=_headers(),
                json={"number": jid, "text": text},
            )
        if r.status_code in (200, 201):
            return True
        log.warning("whatsapp send failed",
                    extra={"jid": jid, "status": r.status_code, "body": r.text[:200]})
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("whatsapp send errored", extra={"jid": jid, "err": str(exc)[:200]})
        return False


async def enabled_targets() -> list[dict]:
    """The enabled destination groups (jid + label), ordered."""
    rows = await fetch("SELECT jid, label FROM wa_targets WHERE enabled = TRUE ORDER BY id")
    return [{"jid": r["jid"], "label": r["label"]} for r in rows]


async def broadcast(text: str) -> int:
    """Send `text` to every enabled target. Returns the count of successful deliveries."""
    if not configured():
        return 0
    ok = 0
    for t in await enabled_targets():
        if await send_text(t["jid"], text):
            ok += 1
    return ok


async def fetch_groups() -> list[dict]:
    """List WhatsApp groups on the gateway (for the /bot group picker). Read-only.

    Returns [{jid, subject, size}, …]; empty on any error or when not configured."""
    if not configured():
        return []
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.get(
                _url(f"/group/fetchAllGroups/{settings.wa_instance}"),
                headers=_headers(),
                params={"getParticipants": "false"},
            )
        if r.status_code not in (200, 201):
            log.warning("whatsapp fetch_groups failed", extra={"status": r.status_code})
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        out = [{"jid": g.get("id"), "subject": g.get("subject"), "size": g.get("size")}
               for g in data if isinstance(g, dict) and g.get("id")]
        out.sort(key=lambda g: (g["subject"] or "").lower())
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("whatsapp fetch_groups errored", extra={"err": str(exc)[:200]})
        return []


def format_order_event(order: dict, *, now_ist_str: str) -> str:
    """Build the WhatsApp text for one REAL order event of the live bot.

    Sourced from `real_orders` (what the bot actually did), not the strategy replay:
      * placed (status 'open')       → a plain heads-up that the bot placed it.
      * rejected (status error/…)    → a clear 'do it manually' warning with the broker
                                        error (e.g. AB4036), which is the whole point of
                                        the feed — catching what the bot can't execute."""
    side = str(order["side"]).upper()
    sym = order["symbol"]
    qty = int(order["qty"])
    price = float(order.get("price") or 0)
    status = str(order.get("status") or "").lower()
    if status == "open":
        emoji = "\U0001F7E2" if side == "BUY" else "\U0001F534"  # 🟢 / 🔴
        return (f"{emoji} Live bot placed {side}  {qty} × {sym}  @ ₹{price:,.2f}\n"
                f"{order.get('reason', '') or ''}  ·  {now_ist_str}")
    err = str(order.get("error") or "").strip() or "rejected by broker"
    return (f"⚠️ Live bot {side}  {qty} × {sym}  — REJECTED\n"
            f"{err}\n"
            f"Buy/sell it manually if you want it.  ·  {now_ist_str}")


def format_quarantine_skip(item: dict, *, reason_code: str | None, now_ist_str: str) -> str:
    """Build the WhatsApp text for a signal the bot DELIBERATELY did not place because the
    symbol is benched after an earlier surveillance/cautionary block (e.g. AB4036).

    Unlike a rejection, no order was even attempted — the bot knows it would fail — so this
    is a pure 'the strategy wants this, do it by hand' nudge. It fires once per distinct
    signal (deduped on the intent key upstream), so you keep getting pinged each time the
    strategy re-signals a benched name, without the bot spamming doomed orders."""
    side = str(item["side"]).upper()
    sym = item["symbol"]
    qty = int(item["qty"])
    price = float(item.get("price") or 0)
    code = f" ({reason_code})" if reason_code else ""
    return (f"🚫 Live bot wants {side}  {qty} × {sym}  @ ₹{price:,.2f}\n"
            f"Skipped — on the surveillance bench{code}, the broker blocks it.\n"
            f"Buy it manually if you want it.  ·  {now_ist_str}")


def format_suspension_alert(symbol: str, *, days_lag: int, qty: int, now_ist_str: str) -> str:
    """Build the WhatsApp text for a HELD position that has stopped pricing — the
    corporate-action heads-up (suspension / delisting / merger in progress).

    The bot can't exit or manage a scrip that isn't trading, and a merger/delisting needs a
    human decision (tender the shares, take the acquirer's stock or the cash), so this is a
    pure 'go handle this by hand' nudge — never an automated trade."""
    return (f"⚠️ Heads up — you HOLD {qty} × {symbol}, but it hasn't priced in ~{days_lag} "
            f"day(s) while the rest of the market has.\n"
            f"That usually means a suspension, delisting, or a merger/M&A in progress. The bot "
            f"can't manage or exit a scrip that isn't trading — check the corporate action and "
            f"handle it manually.  ·  {now_ist_str}")


def format_cash_reconcile(*, amount: float, kind: str, now_ist_str: str, confirm_url: str = "") -> str:
    """Build the WhatsApp text for an unexplained change in the account's FREE CASH — cash that
    moved with no bot or manual trade behind it, i.e. almost certainly a deposit or withdrawal
    that hasn't been recorded yet.

    The bot has queued a one-tap confirmation on the dashboard; this nudges the owner to resolve
    it. Nothing is auto-booked — only the human can tell a deposit from a dividend (Angel exposes
    one blended cash figure, not a labelled ledger). `confirm_url`, when set, deep-links to /bot."""
    amt = abs(float(amount))
    where = f"Confirm it in one tap: {confirm_url}" if confirm_url else \
        "Confirm it in one tap on your /bot dashboard"
    if kind == "withdrawal":
        return (f"💸 ₹{amt:,.0f} left your account cash with no matching trade.\n"
                f"{where} — so your return stays accurate.  ·  {now_ist_str}")
    return (f"💰 ₹{amt:,.0f} appeared in your account cash with no matching bot or manual trade.\n"
            f"{where} — otherwise it counts as profit instead of capital.  ·  {now_ist_str}")


def _signed_rupees(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}₹{abs(float(v)):,.0f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


DIGEST_MAX_FILL_LINES = 20


def format_weekly_digest(s: dict) -> str:
    """Build the weekly WhatsApp digest for the live account (tools/weekly_digest.py). Pure.

    `s` keys: week_label; last_sync; sync_warning (or None); cash, holdings_value, pnl
    (metrics.split_pnl dict); week_pnl + week_since (change in deposit-adjusted total P&L since the
    previous weekly digest, or None on the first one); deposits_week (net recorded deposits this
    week — they move invested, not P&L); fills [{day, side, symbol, qty, price, source:
    "bot"|"manual"}] in time order; bot_enabled; verdict (entry_verdict headline, or None); wanted
    (shadow symbols this week); xirr_pct (metrics.live_account_xirr, or None). The "why no bot buys" line shows only when the bot is ON and made no
    BUY all week — when it's OFF, that IS the reason. Fill lines cap at DIGEST_MAX_FILL_LINES."""
    p = s["pnl"]
    lines = [f"📊 Weekly digest · {s['week_label']}",
             f"Net worth ₹{p['net_worth']:,.0f}  (cash ₹{s['cash']:,.0f} + holdings "
             f"₹{s['holdings_value']:,.0f})"]
    wk = s.get("week_pnl")
    wk_txt = (f"This week {_signed_rupees(wk)} (since {s.get('week_since') or 'last digest'})  ·  "
              if wk is not None else "")
    pct = f" ({p['pct']:+.1f}%)" if p.get("pct") is not None else ""
    lines.append(f"{wk_txt}Total P&L {_signed_rupees(p['total_pnl'])}{pct} on ₹{p['invested']:,.0f} invested")
    xirr = s.get("xirr_pct")
    xirr_txt = f"  ·  XIRR {xirr:+.1f}%/yr" if xirr is not None else ""
    lines.append(f"Realized {_signed_rupees(p['realized_pnl'])}  ·  Unrealized {_signed_rupees(p['unrealized_pnl'])}{xirr_txt}")
    dep = float(s.get("deposits_week") or 0.0)
    if abs(dep) >= 0.5:
        lines.append(f"{'Deposits' if dep > 0 else 'Withdrawals'} this week: {_signed_rupees(dep)} "
                     f"(capital, not P&L)")
    fills = s.get("fills") or []
    if fills:
        buys = [f for f in fills if f["side"] == "BUY"]
        sells = [f for f in fills if f["side"] != "BUY"]

        def notional(fs: list[dict]) -> float:
            return sum(int(f["qty"]) * float(f["price"]) for f in fs)

        lines.append(f"Trades this week: {_plural(len(buys), 'buy')} (₹{notional(buys):,.0f})  ·  "
                     f"{_plural(len(sells), 'sell')} (₹{notional(sells):,.0f})")
        for f in fills[:DIGEST_MAX_FILL_LINES]:
            dot = "\U0001F7E2" if f["side"] == "BUY" else "\U0001F534"  # 🟢 / 🔴
            who = "" if f["source"] == "bot" else " (manual)"
            lines.append(f"  {dot} {f['day']} {f['side']} {int(f['qty'])} × {f['symbol']} "
                         f"@ ₹{float(f['price']):,.2f}{who}")
        if len(fills) > DIGEST_MAX_FILL_LINES:
            lines.append(f"  …and {len(fills) - DIGEST_MAX_FILL_LINES} more (see /bot)")
    else:
        lines.append("No trades this week.")
    if not s.get("bot_enabled"):
        lines.append("⏸ Bot is currently OFF — it won't place orders until you switch it on.")
    elif s.get("verdict") and not any(f["side"] == "BUY" and f["source"] == "bot" for f in fills):
        lines.append(f"Why no bot buys: {s['verdict']}")
    wanted = s.get("wanted") or []
    if wanted:
        lines.append(f"🔭 Strategy wanted this week: {', '.join(wanted)}")
    if s.get("sync_warning"):
        lines.append(f"⚠️ {s['sync_warning']}")
    elif s.get("last_sync"):
        lines.append(f"as of {s['last_sync']}")
    return "\n".join(lines)
