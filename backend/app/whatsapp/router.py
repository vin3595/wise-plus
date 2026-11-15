"""WhatsApp webhook (wise-plus): the owner messages the restaurant's WhatsApp number and gets answers.

Only owners and managers whose mobile number is on their staff account get an answer; anyone else is
ignored (no reply, so nothing leaks). Someone in more than one restaurant starts with @loginid once.
Meta: Webhook URL https://<address>/api/whatsapp/webhook, verify token = WISE_WA_VERIFY_TOKEN,
subscribe to 'messages'."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from ..config import get_settings
from ..core.permissions import Perm, Role, ROLE_PERMS
from ..db import system_session, tenant_session
from ..models import Tenant, User
from . import client

log = logging.getLogger("wise.whatsapp")
router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

_seen: OrderedDict[str, None] = OrderedDict()     # Meta retries; answer each message once
_choice: dict[str, str] = {}                       # phone -> login_id picked with @loginid


@router.get("/webhook")
def verify(request: Request) -> PlainTextResponse:
    q = request.query_params
    s = get_settings()
    if q.get("hub.mode") == "subscribe" and s.wa_verify_token and hmac.compare_digest(q.get("hub.verify_token", ""), s.wa_verify_token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong verify token")


@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks) -> dict:
    raw = await request.body()
    secret = get_settings().wa_app_secret
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WhatsApp isn't set up on this server")
    sig = request.headers.get("x-hub-signature-256", "")
    good = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad signature")
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": True}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []) or []:
                mid = m.get("id", "")
                if mid in _seen:
                    continue
                _seen[mid] = None
                while len(_seen) > 2000:
                    _seen.popitem(last=False)
                text = (m.get("text") or {}).get("body") or (m.get("button") or {}).get("text") or ""
                if m.get("from") and text:
                    background.add_task(handle, m["from"], text)
    return {"ok": True}       # always 200 fast, or Meta keeps retrying


def _who(phone10: str) -> list[tuple[str, str, str]]:
    """(tenant_id, login_id, role) for every active owner/manager account with this mobile number."""
    allowed = [r.value for r, perms in ROLE_PERMS.items() if Perm.reports_view in perms]
    with system_session() as db:
        rows = db.execute(select(User.tenant_id, Tenant.login_id, User.role).join(Tenant, Tenant.id == User.tenant_id)
                          .where(func.right(User.phone, 10) == phone10, User.is_active.is_(True), User.role.in_(allowed),
                                 Tenant.status.not_in(["suspended"]))).all()
    return [(str(t), l, r) for t, l, r in rows]


def reply_for(phone: str, text: str) -> str | None:
    """What to answer; None = stay silent (a number that isn't an owner or manager)."""
    from ..assistant.answers import answer
    p10 = client.last10(phone)
    places = _who(p10)
    if not places:
        log.info("whatsapp: ignoring message from unknown number …%s", p10[-4:])
        return None
    m = re.match(r"\s*@([a-z0-9-]+)\s*(.*)", text, re.S | re.I)
    if m:
        want = m.group(1).lower()
        if want not in {l for _, l, _ in places}:
            return "You're not on that restaurant. Yours: " + ", ".join(f"@{l}" for _, l, _ in places)
        _choice[p10] = want
        text = m.group(2) or "help"
    pick = _choice.get(p10)
    chosen = next((pl for pl in places if pl[1] == pick), None) or (places[0] if len(places) == 1 else None)
    if chosen is None:
        return ("You're on more than one restaurant. Start with which one, e.g.\n"
                + "\n".join(f"*@{l} dues*" for _, l, _ in places) + "\nAfter that I'll remember it.")
    with tenant_session(chosen[0]) as db:
        out = answer(db, text)
    return out + (f"\n\n_@{chosen[1]}_" if len(places) > 1 else "")


def handle(phone: str, text: str) -> None:
    try:
        out = reply_for(phone, text)
        if out and client.enabled():
            client.send_text(phone, out)
    except Exception:  # never let one bad message break the webhook
        log.exception("whatsapp: couldn't answer")
