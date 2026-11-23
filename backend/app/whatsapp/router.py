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
import time
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
HOLD_SECS = 30 * 60


class _Store:
    """phone -> value, kept in the wa_state table so every API process sees the same thing.
    _choice: the restaurant (login id) picked with @loginid; _held: photos sent before that choice."""
    def __init__(self, col: str):
        self.col = col

    def _row(self, db, p10, create=False):
        from ..stockplus.models import WaState
        r = db.get(WaState, p10)
        if r is None and create:
            r = WaState(phone10=p10)
            db.add(r)
        return r

    def get(self, p10, default=None):
        from ..db import system_session
        with system_session() as db:
            r = self._row(db, p10)
            v = getattr(r, self.col) if r else None
            return default if v is None else v

    def __getitem__(self, p10):
        v = self.get(p10)
        if v is None:
            raise KeyError(p10)
        return v

    def __setitem__(self, p10, value):
        from ..db import system_session
        with system_session() as db:
            setattr(self._row(db, p10, create=True), self.col, value)

    def pop(self, p10, default=None):
        from ..db import system_session
        with system_session() as db:
            r = self._row(db, p10)
            v = getattr(r, self.col) if r else None
            if r is not None:
                setattr(r, self.col, None)
            return default if v is None else v

    def __contains__(self, p10):
        return self.get(p10) is not None

    def __bool__(self):
        from sqlalchemy import func, select

        from ..db import system_session
        from ..stockplus.models import WaState
        with system_session() as db:
            return bool(db.scalar(select(func.count()).select_from(WaState).where(getattr(WaState, self.col).is_not(None))))

    def clear(self):
        from sqlalchemy import update

        from ..db import system_session
        from ..stockplus.models import WaState
        with system_session() as db:
            db.execute(update(WaState).values({self.col: None}))


_choice = _Store("choice")
_held = _Store("held")


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
        from .numbers import webhook_secrets          # a restaurant's own Meta app (set up by hand) points here too
        if not any(hmac.compare_digest(sig, "sha256=" + hmac.new(x.encode(), raw, hashlib.sha256).hexdigest()) for x in webhook_secrets()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad signature")
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": True}
    from . import health
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field") or "messages"
            if field != "messages":                      # quality, templates, the account: alerts for you
                background.add_task(_safe, health.on_account, field, change.get("value") or {})
                continue
            if change.get("value", {}).get("statuses"):  # delivery ticks on what we sent
                background.add_task(_safe, health.on_statuses, change["value"]["statuses"])
            via = ((change.get("value") or {}).get("metadata") or {}).get("phone_number_id")
            names = {(ct.get("wa_id") or ""): ((ct.get("profile") or {}).get("name") or "")
                     for ct in change.get("value", {}).get("contacts", []) or []}
            for m in change.get("value", {}).get("messages", []) or []:
                mid = m.get("id", "")
                if mid in _seen:
                    continue
                _seen[mid] = None
                while len(_seen) > 2000:
                    _seen.popitem(last=False)
                inter = m.get("interactive") or {}
                tap = inter.get("button_reply") or inter.get("list_reply") or {}
                btn = m.get("button") or {}       # a quick reply under a template carries our payload
                text = tap.get("id") or (m.get("text") or {}).get("body") or btn.get("payload") or btn.get("text") or ""
                media = m.get("image") or m.get("document")
                if m.get("from") and media and media.get("id"):
                    background.add_task(handle_media, m["from"], media["id"], media.get("mime_type") or "",
                                        media.get("filename") or "bill", media.get("caption") or "", via)
                elif m.get("from") and text:
                    background.add_task(handle, m["from"], text, names.get(m["from"], ""), via)
                if m.get("from"):                       # wise-plus: the 24-hour window for the nightly summary
                    background.add_task(_seen_now, m["from"])
    return {"ok": True}       # always 200 fast, or Meta keeps retrying


@router.post("/fast2sms/{key}")
async def receive_f2s(key: str, request: Request, background: BackgroundTasks) -> dict:
    """Replies through Fast2SMS (their WhatsApp webhook, payload template set in their panel): text, button taps,
    photos and PDFs go the same way as Meta's. The secret is in the address (Fast2SMS's new panel can't sign)."""
    hook = get_settings().wa_f2s_hook
    if not hook or not hmac.compare_digest(key, hook):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong address")
    raw = await request.body()
    try:
        d = json.loads(raw)
    except ValueError:
        from urllib.parse import parse_qsl
        d = dict(parse_qsl(raw.decode(errors="replace")))
    if isinstance(d, list):
        d = d[0] if d else {}
    if not isinstance(d, dict) or str(d.get("webhook_type", "")) != "incoming_message":
        return {"ok": True}                      # delivery ticks: Fast2SMS keeps those in its report
    frm = re.sub(r"\D", "", str(d.get("from") or ""))
    mid = str(d.get("message_id") or "")
    if not frm or (mid and mid in _seen):
        return {"ok": True}
    if mid:
        _seen[mid] = None
        while len(_seen) > 2000:
            _seen.popitem(last=False)
    kind = str(d.get("message_type") or "text").lower()
    url = str(d.get("media_url") or "")
    if kind in ("image", "document") and url.startswith("https://"):
        background.add_task(handle_media, frm, "url:" + url, str(d.get("mime_type") or ""), "bill", str(d.get("caption") or ""), None)
    else:
        text = str(d.get("body") or d.get("caption") or "").strip()
        if text:
            background.add_task(handle, frm, text, str(d.get("name") or ""), None)
    background.add_task(_seen_now, frm)
    return {"ok": True}


def _safe(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("whatsapp: webhook event")


def _seen_now(phone: str) -> None:
    from ..stockplus.nightly import seen
    try:
        seen(client.last10(phone))
    except Exception:  # noqa: BLE001
        log.exception("whatsapp: couldn't note the 24-hour window")


def _who(phone10: str) -> list[tuple[str, str, str, str]]:
    """(tenant_id, login_id, role, user_id) for every active owner/manager account with this mobile number."""
    allowed = [r.value for r, perms in ROLE_PERMS.items() if Perm.reports_view in perms]
    from ..stockplus.models import WaAlert
    muted = select(WaAlert.user_id).where(WaAlert.topic == "wa_off")   # this account switched off on WhatsApp
    with system_session() as db:
        rows = db.execute(select(User.tenant_id, Tenant.login_id, User.role, User.id).join(Tenant, Tenant.id == User.tenant_id)
                          .where(func.right(User.phone, 10) == phone10, User.is_active.is_(True), User.role.in_(allowed),
                                 Tenant.status.not_in(["suspended"]), User.id.not_in(muted))).all()
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
        elif word in {"nightly on", "nightly off", "stop nightly"}:
            from ..stockplus.nightly import subscribe
            on = word == "nightly on"
            subscribe(db, uuid.UUID(chosen[0]), uuid.UUID(chosen[3]), on)
            out = Reply("🌙 You'll get the closing summary every night at " + (get_settings().nightly_at or "night") + "." if on
                        else "Nightly summary stopped. Send *nightly on* to start it again.",
                        [("summary", "Today so far")] if on else [("nightly on", "Start again")])
        else:
            from . import petty as wpetty
            out = wpetty.handle(db, chosen, word, text or "")
            if out is None and word in ("credits", "all credits", "credits all", "restaurants", "all restaurants"):
                from ..billing import platform
                if platform.admin_by_phone(phone):
                    out = Reply(platform.whatsapp_text())
            from . import banks as wbanks
            if out is None and (word in wbanks.WORDS | wbanks.CHECK or word.startswith("bk ")):
                from ..core.permissions import Perm, Role
                from ..services.role_perms import effective
                if Perm.banks not in effective(uuid.UUID(chosen[0]), Role(chosen[2])):
                    out = Reply("Bank accounts are for the owner. Ask them to allow it under Staff › Who can do what.")
                else:
                    out = wbanks.handle(db, chosen[3], word)
            if out is None:
                from . import files as wfiles
                out = wfiles.handle(db, chosen, word)
            if out is None:
                out = answer_rich(db, text or "help")
                if out.text.startswith("I didn't get that") and wpetty.looks_like_expense(word):
                    # "brownie 25 pieces 650 rs": an expense written without 'kharcha'
                    out = wpetty.handle(db, chosen, "kharcha " + word, "kharcha " + (text or "")) or out
    if multi:
        out.text += f"\n\n_@{chosen[1]}_"
    return out


def reply_for(phone: str, text: str) -> str | None:
    r = reply_rich(phone, text)
    return r.text if r else None


def send(phone: str, r: Reply) -> None:
    if client.enabled():
        client.send_options(phone, r.text, r.options)
        if r.document:
            client.send_document(phone, r.document[0], r.document[1])


async def handle_media(phone: str, media_id: str, mime: str, filename: str, caption: str, via: str | None = None) -> None:
    """A photo or PDF: read it as a supplier's bill and say what was found."""
    with client.context(via=via):
        await _handle_media(phone, media_id, mime, filename, caption)


async def _handle_media(phone: str, media_id: str, mime: str, filename: str, caption: str) -> None:
    from . import bills
    try:
        chosen, rest, multi, early = _place(phone, caption)
        if chosen is None:
            if early:
                # Which restaurant is this bill for? Keep the photo and read it once they tap one.
                p10 = client.last10(phone)
                waiting = [h for h in _held.get(p10, []) if time.time() - h[0] < HOLD_SECS]
                first = not waiting
                waiting.append([time.time(), media_id, mime, filename, caption or ""])
                _held[p10] = waiting[-10:]
                if first:   # one question for a batch of photos
                    early.text = "Which restaurant is this bill for? Tap one and I'll read it straight away."
                    early.options = [(cmd.split(" ")[0], label) for cmd, label in early.options]
                    send(phone, early)
            return
        from ..modules import enabled
        on = enabled(chosen[0])
        if "bills" not in on and "sheets" not in on:     # wise-plus: neither photo service switched on
            send(phone, Reply("Bill scan and daily sheets aren't switched on for this business. The owner can turn them on in Settings › Services."))
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


def _release_held(phone: str, text: str) -> bool:
    """They just picked a restaurant ("@shakedemo") and photos are waiting: read those instead of showing help."""
    p10 = client.last10(phone)
    waiting = [h for h in _held.pop(p10, []) if time.time() - h[0] < HOLD_SECS]
    m = re.fullmatch(r"\s*@([a-z0-9-]+)\s*(help)?\s*", text or "", re.I)
    if not waiting or not m:
        if waiting:
            _held[p10] = waiting
        return False
    login = m.group(1).lower()
    import asyncio
    for _, media_id, mime, filename, caption in waiting:
        asyncio.run(handle_media(phone, media_id, mime, filename, f"@{login} {caption}".strip()))
    return True


def offer_reply(phone: str, text: str, name: str = "") -> bool:
    """wise-plus: anyone sending an offer's keyword gets their own QR code (one WhatsApp number serves every business,
    so keywords are unique across all of them). True = it was a keyword."""
    if not text or len(text) > 40 or "\n" in text:
        return False
    from ..config import get_settings
    from ..db import system_session
    from ..offers import service as osv
    with system_session() as db:
        offer = osv.find_keyword(db, text)
        if offer is None:
            return False
        if offer.prepaid:                                # pay online first: the link, or the QR if already paid
            from ..offers import prepay
            text, code = prepay.start(db, offer, client.last10(phone), name)
            if code is None:
                client.send_text(phone, text)
                return True
        else:
            code, why = osv.issue(db, offer, client.last10(phone), name)
            if code is None:
                client.send_text(phone, why)
                return True
        body = osv.reply_text(db, offer, code)
        link = f"{get_settings().public_base_url.rstrip('/')}/api/public/offer-qr/{code.code}.png"
        tid, cstr = offer.tenant_id, code.code
    with client.context(tenant_id=tid, purpose="offer", ref=cstr):
        try:
            client.send_image(phone, link, body)
        except Exception:  # noqa: BLE001 - the picture couldn't be fetched: the code in words still works
            client.send_text(phone, body)
    return True


def handle(phone: str, text: str, name: str = "", via: str | None = None) -> None:
    with client.context(via=via):
        _handle(phone, text, name)


def _handle(phone: str, text: str, name: str = "") -> None:
    try:
        t = (text or "").strip()
        if t.startswith("stop:"):                      # 'Stop offers' on a broadcast
            from ..offers.broadcast import opt_out
            msg = opt_out(phone, t[5:])
            if msg:
                client.send_text(phone, msg)
            return
        if t.startswith("kw:"):                        # 'Get my offer' on a broadcast
            text = t[3:]
        if not _who(client.last10(phone)):              # wise-plus growth: a lead answering our follow-ups
            from ..growth import service as gs
            low = t.lower()
            if low in ("lead-stop", "stop", "unsubscribe") and gs.stop(client.last10(phone)):
                client.send_text(phone, "Done, no more messages from us. Thank you! 🙏")
                return
            if low in ("demo", "call me", "call") and gs.wants_demo(client.last10(phone)):
                return
        if offer_reply(phone, text, name):
            return
        from ..custwa.service import handle_customer   # wise-plus: customers rating a visit
        if (text or "").startswith("fb:") or not _who(client.last10(phone)):
            if handle_customer(phone, text):
                return
        if _release_held(phone, text):
            return
        out = reply_rich(phone, text)
        if out:
            send(phone, out)
    except Exception:  # never let one bad message break the webhook
        log.exception("whatsapp: couldn't answer")
