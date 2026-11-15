"""WhatsApp webhook (wise-plus): the owner messages the restaurant's WhatsApp number and gets answers,
or sends a photo / PDF of a supplier's bill and it is read into a purchase ('save' adds it to stock).

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
import uuid
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from ..config import get_settings
from ..core.permissions import Perm, Role, ROLE_PERMS
from ..db import system_session, tenant_session
from ..models import Tenant, User
from ..assistant.answers import Reply
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
                inter = m.get("interactive") or {}
                tap = inter.get("button_reply") or inter.get("list_reply") or {}
                text = tap.get("id") or (m.get("text") or {}).get("body") or (m.get("button") or {}).get("text") or ""
                media = m.get("image") or m.get("document")
                if m.get("from") and media and media.get("id"):
                    background.add_task(handle_media, m["from"], media["id"], media.get("mime_type") or "",
                                        media.get("filename") or "bill", media.get("caption") or "")
                elif m.get("from") and text:
                    background.add_task(handle, m["from"], text)
    return {"ok": True}       # always 200 fast, or Meta keeps retrying


def _who(phone10: str) -> list[tuple[str, str, str, str]]:
    """(tenant_id, login_id, role, user_id) for every active owner/manager account with this mobile number."""
    allowed = [r.value for r, perms in ROLE_PERMS.items() if Perm.reports_view in perms]
    with system_session() as db:
        rows = db.execute(select(User.tenant_id, Tenant.login_id, User.role, User.id).join(Tenant, Tenant.id == User.tenant_id)
                          .where(func.right(User.phone, 10) == phone10, User.is_active.is_(True), User.role.in_(allowed),
                                 Tenant.status.not_in(["suspended"]))).all()
    return [(str(t), l, r, str(u)) for t, l, r, u in rows]


def _place(phone: str, text: str) -> tuple[tuple | None, str, bool, Reply | None]:
    """(chosen (tenant_id, login_id, role, user_id) or None, text without @loginid, on several restaurants?,
    a reply to send instead). chosen None and reply None = a stranger: stay silent."""
    p10 = client.last10(phone)
    places = _who(p10)
    if not places:
        log.info("whatsapp: ignoring message from unknown number …%s", p10[-4:])
        return None, text, False, None
    m = re.match(r"\s*@([a-z0-9-]+)\s*(.*)", text or "", re.S | re.I)
    if m:
        want = m.group(1).lower()
        if want not in {pl[1] for pl in places}:
            return None, text, True, Reply("You're not on that restaurant. Yours: " + ", ".join(f"@{pl[1]}" for pl in places),
                                           [(f"@{pl[1]} help", f"@{pl[1]}") for pl in places])
        _choice[p10] = want
        text = m.group(2)
    pick = _choice.get(p10)
    chosen = next((pl for pl in places if pl[1] == pick), None) or (places[0] if len(places) == 1 else None)
    if chosen is None:
        rest = (text or "").strip() or "help"
        return None, text, True, Reply("You're on more than one restaurant. Which one? I'll remember it.",
                                       [(f"@{pl[1]} {rest}", f"@{pl[1]}") for pl in places])
    return chosen, text, len(places) > 1, None


SAVE = {"save", "ok", "okay", "yes", "haan", "han", "done"}
ADD = {"add", "add all", "create", "add new", "new"}
DISCARD = {"discard", "cancel", "no", "delete", "nahi", "skip"}


def reply_rich(phone: str, text: str) -> Reply | None:
    """What to answer, with the taps that follow; None = stay silent (a number that isn't an owner or manager)."""
    from ..assistant.answers import answer_rich
    chosen, text, multi, early = _place(phone, text)
    if chosen is None:
        return early
    word = (text or "").strip().lower().strip(".!")
    # A button under one bill: "save <bill id>"; under a daily sheet: "savesheet <sheet id>".
    m = re.fullmatch(r"(save|add|discard) ([0-9a-f-]{36})", word)
    bill_id = None
    if m:
        word, bill_id = m.group(1), uuid.UUID(m.group(2))
    ms = re.fullmatch(r"(savesheet|dropsheet) ([0-9a-f-]{36})", word)
    with tenant_session(chosen[0]) as db:
        from . import sheets
        if ms:
            out = sheets.save_or_drop(db, client.last10(phone), ms.group(1) == "savesheet", uuid.UUID(ms.group(2))) or Reply("That sheet isn't here any more.")
        elif word in SAVE | DISCARD | ADD:
            from . import bills
            out = bills.save_or_discard(db, chosen, client.last10(phone), word not in DISCARD, add_new=word in ADD, bill_id=bill_id)
            if bill_id is None and out.text.startswith("No bill waiting"):
                out = sheets.save_or_drop(db, client.last10(phone), word not in DISCARD) or out
        else:
            out = answer_rich(db, text or "help")
    if multi:
        out.text += f"\n\n_@{chosen[1]}_"
    return out


def reply_for(phone: str, text: str) -> str | None:
    r = reply_rich(phone, text)
    return r.text if r else None


def send(phone: str, r: Reply) -> None:
    if client.enabled():
        client.send_options(phone, r.text, r.options)


async def handle_media(phone: str, media_id: str, mime: str, filename: str, caption: str) -> None:
    """A photo or PDF: read it as a supplier's bill and say what was found."""
    from . import bills
    try:
        chosen, rest, multi, early = _place(phone, caption)
        if chosen is None:
            if early:
                # Which restaurant is this bill for? Ask; the photo has to be sent again with the answer known.
                early.text = "Which restaurant is this bill for? Tap one, then send the photo again."
                early.options = [(cmd.split(" ")[0] + " help", label) for cmd, label in early.options]
                send(phone, early)
            return
        out = await bills.read_sent_bill(chosen, client.last10(phone), media_id, mime, filename, rest)
        if out:
            if multi:
                out.text += f"\n\n_@{chosen[1]}_"
            send(phone, out)
    except Exception:
        log.exception("whatsapp: couldn't read a sent bill")
        if client.enabled():
            client.send_text(phone, "Sorry, I couldn't read that bill. Try again, or use Scan bill in the app.")


def handle(phone: str, text: str) -> None:
    try:
        out = reply_rich(phone, text)
        if out:
            send(phone, out)
    except Exception:  # never let one bad message break the webhook
        log.exception("whatsapp: couldn't answer")
