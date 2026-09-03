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
