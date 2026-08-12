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


def format_signal(trade: dict, *, portfolio_name: str, placeable: bool,
                  note: str | None, now_ist_str: str) -> str:
    """Build the WhatsApp message text for one BUY/SELL signal.

    Placeable signals read as a plain heads-up; unplaceable ones (quarantine / no
    cash / phantom sell) carry a clear 'act manually' warning — that's the whole
    reason the feed exists."""
    side = str(trade["side"]).upper()
    emoji = "\U0001F7E2" if side == "BUY" else "\U0001F534"  # 🟢 / 🔴
    qty = int(trade["qty"])
    price = float(trade["price"])
    sym = trade["symbol"]
    lines = [
        f"{emoji} {side}  {qty} × {sym}  @ ₹{price:,.2f}",
        f"≈ ₹{qty * price:,.0f}  ·  {trade.get('reason', '')}",
        f"{portfolio_name}  ·  {now_ist_str}",
    ]
    if not placeable:
        lines.append(f"⚠️ Bot can't place this — {note or 'manual action needed'}. "
                     f"Buy/sell manually.")
    return "\n".join(lines)
