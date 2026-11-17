"""wise-plus: the bill on WhatsApp after paying, with three buttons for how the visit was.

    queue_bill(db, bill)         called when a bill is saved (only if the outlet has WhatsApp bills on)
    sender()                     loop: sends what's queued through the approved template (WISE_WA_BILL_TEMPLATE)
    handle_customer(phone, text) a customer's button tap or reply; True when it was theirs to handle

The template (category utility) has 4 body values - restaurant, invoice, amount, bill link - and three
quick-reply buttons; each button carries "fb:<bill id>:<rating>" so a tap is tied to the bill.
  😍 Loved it -> 5: thank you, and the outlet's Google review link if it has one
  🙂 It was okay -> 3: thank you, and what could be better? (their reply is kept)
  😞 Not good -> 1: sorry, what went wrong? (kept), and the owner gets a WhatsApp alert straight away
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Bill, Outlet, Tenant, User
from .models import BillFeedback, WaOutbox

log = logging.getLogger("wise.custwa")
RATINGS = {5: "😍 Loved it", 3: "🙂 It was okay", 1: "😞 Not good"}
BUTTON_ORDER = (5, 3, 1)
COMMENT_WINDOW = timedelta(hours=6)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def rupees(paise: int) -> str:
    r = paise / 100
    return f"{r:,.0f}" if r == int(r) else f"{r:,.2f}"


def queue_bill(db: Session, bill: Bill) -> None:
    """After a bill is saved: put the WhatsApp bill in the queue (the sender loop sends it moments later)."""
    s = get_settings()
    if not (bill.customer_phone and s.wa_token and s.wa_phone_number_id and s.wa_bill_template):
        return
    outlet = db.get(Outlet, bill.outlet_id)
    from ..services.training import is_training
    if not outlet or not outlet.wa_bills or is_training(db, outlet.id):
        return
    from ..services.sms import app_base_url, ensure_share_key
    tenant = db.get(Tenant, bill.tenant_id)
    from ..billing.service import take_msg
    ok, alert = take_msg(db, tenant)          # free allowance per outlet, then bought packs
    if alert:
        _tell_owner(db, bill.tenant_id, alert)
    if not ok:
        return
    payload = {"brand": tenant.app_name or tenant.name, "invoice": bill.invoice_no, "amount": rupees(bill.total_paise),
               "link": f"{app_base_url(tenant)}/b/{ensure_share_key(bill)}", "bill_id": str(bill.id)}
    db.execute(text("insert into wa_outbox (id, tenant_id, kind, ref, to_phone, payload, status, tries, next_try_at, created_at) "
                    "values (:id, :t, 'bill', :ref, :to, cast(:p as jsonb), 'queued', 0, now(), now()) on conflict (kind, ref) do nothing"),
               {"id": uuid.uuid4(), "t": bill.tenant_id, "ref": str(bill.id), "to": bill.customer_phone,
                "p": __import__("json").dumps(payload)})


def send_bill(to: str, p: dict) -> None:
    from ..whatsapp import client
    s = get_settings()
    comps = [{"type": "body", "parameters": [{"type": "text", "text": str(p[k])[:300]} for k in ("brand", "invoice", "amount", "link")]}]
    comps += [{"type": "button", "sub_type": "quick_reply", "index": str(i),
               "parameters": [{"type": "payload", "payload": f"fb:{p['bill_id']}:{r}"}]} for i, r in enumerate(BUTTON_ORDER)]
    client._post({"to": client.to_wa(to), "type": "template",
                  "template": {"name": s.wa_bill_template, "language": {"code": "en"}, "components": comps}})


def send_due(limit: int = 20) -> int:
    """Send what's queued. Several API processes may run this: rows are claimed with SKIP LOCKED."""
    from ..db import system_session
    sent = 0
    with system_session() as db:
        rows = db.scalars(select(WaOutbox).where(WaOutbox.status == "queued", WaOutbox.next_try_at <= _now())
                          .order_by(WaOutbox.created_at).limit(limit).with_for_update(skip_locked=True)).all()
        for r in rows:
            try:
                if r.kind == "bill":
                    send_bill(r.to_phone, r.payload)
                elif r.kind == "promo":
                    from ..offers.broadcast import send_promo
                    send_promo(r.to_phone, r.payload)
                r.status, r.error = "sent", None
                sent += 1
            except Exception as e:  # noqa: BLE001
                r.tries += 1
                r.error = str(e)[:300]
                r.status = "failed" if r.tries >= 5 else "queued"
                r.next_try_at = _now() + timedelta(minutes=2 ** r.tries)
                log.warning("custwa: %s to …%s failed (%s)", r.kind, r.to_phone[-4:], r.error)
    return sent


async def sender() -> None:
    s = get_settings()
    if not (s.wa_token and s.wa_phone_number_id):
        return
    while True:
        try:
            await asyncio.to_thread(send_due)
        except Exception:  # noqa: BLE001
            log.exception("custwa sender")
        await asyncio.sleep(5)


# ------------------------------------------------------------------ the customer's side

def _reply(phone: str, body: str) -> None:
    from ..whatsapp import client
    client.send_text(phone, body)


def _tell_owner(db: Session, tenant_id, msg: str) -> None:
    """A note to the owner (a reply if they've messaged in the last 24 hours; otherwise it's on the Billing page)."""
    from ..whatsapp import client
    for p in set(db.scalars(select(User.phone).where(User.tenant_id == tenant_id, User.role == "owner", User.phone.is_not(None))).all()):
        try:
            client.send_options(p, msg, [("buy messages", "Buy more")])
        except Exception:  # noqa: BLE001
            log.info("custwa: note to owner …%s not sent", str(p)[-4:])


def _alert_owner(db: Session, tenant_id, outlet: Outlet, bill: Bill, fb: BillFeedback) -> None:
    """A 'not good' visit: tell the owner and managers (those who get the nightly summary, else the owner)."""
    from ..stockplus.models import WaAlert
    from ..whatsapp import client
    phones = db.scalars(select(User.phone).join(WaAlert, WaAlert.user_id == User.id)
                        .where(WaAlert.tenant_id == tenant_id, WaAlert.topic == "nightly", User.phone.is_not(None))).all()
    if not phones:
        phones = db.scalars(select(User.phone).where(User.tenant_id == tenant_id, User.role == "owner", User.phone.is_not(None))).all()
    msg = (f"😞 A customer rated their visit *Not good*.\n{outlet.name} · bill {bill.invoice_no} · ₹{rupees(bill.total_paise)}"
           f" · …{fb.phone10[-4:]}" + (f"\nThey said: {fb.comment}" if fb.comment else "\nI've asked them what went wrong."))
    for p in set(phones):
        try:
            client.send_text(p, msg)
        except Exception:  # noqa: BLE001 - owner may be outside the 24-hour window; it's in the app anyway
            log.info("custwa: owner alert to …%s not sent", str(p)[-4:])


def handle_customer(phone: str, text_in: str) -> bool:
    """A tap on the bill's buttons, or what a customer wrote after rating. False = not a customer message."""
    from ..db import system_session, tenant_session
    from ..stockplus.models import WaState
    from ..whatsapp import client
    p10 = client.last10(phone)
    m = re.fullmatch(r"fb:([0-9a-f-]{36}):([135])", (text_in or "").strip())
    if m:
        bill_id, rating = uuid.UUID(m.group(1)), int(m.group(2))
        with system_session() as db:
            bill = db.get(Bill, bill_id)
            if bill is None or not bill.customer_phone or client.last10(bill.customer_phone) != p10:
                return True        # not their bill: ignore quietly
            tid = bill.tenant_id
        with tenant_session(tid) as db:
            bill = db.get(Bill, bill_id)
            outlet = db.get(Outlet, bill.outlet_id)
            fb = db.scalar(select(BillFeedback).where(BillFeedback.bill_id == bill_id))
            if fb is None:
                fb = BillFeedback(tenant_id=tid, outlet_id=bill.outlet_id, bill_id=bill_id, phone10=p10, rating=rating)
                db.add(fb)
            else:
                fb.rating = rating
            db.flush()
            fid, review = fb.id, outlet.google_review_url
            if rating == 1:
                _alert_owner(db, tid, outlet, bill, fb)
        with system_session() as db:
            st = db.get(WaState, p10) or WaState(phone10=p10)
            st.feedback = {"feedback_id": str(fid), "tenant_id": str(tid), "at": _now().isoformat()} if rating < 5 else None
            db.merge(st)
        if rating == 5:
            _reply(phone, "Thank you! 😍 We're so glad you loved it." + (
                f"\n\nWould you tell others too? A quick Google review helps us a lot:\n{review}" if review else ""))
        elif rating == 3:
            _reply(phone, "Thanks for telling us. 🙂 What one thing would have made it better? Just reply here.")
        else:
            _reply(phone, "We're really sorry. 😞 What went wrong? Reply here and the owner will see it today.")
        return True
    # A customer who rated okay / not good: their next message is the reason.
    with system_session() as db:
        st = db.get(WaState, p10)
        pend = st.feedback if st else None
        if not pend or _now() - datetime.fromisoformat(pend["at"]) > COMMENT_WINDOW:
            return False
        st.feedback = None
    tid = uuid.UUID(pend["tenant_id"])
    with tenant_session(tid) as db:
        fb = db.get(BillFeedback, uuid.UUID(pend["feedback_id"]))
        if fb is None:
            return False
        fb.comment = ((fb.comment + "\n") if fb.comment else "") + (text_in or "").strip()[:1000]
        db.flush()
        if fb.rating == 1:
            _alert_owner(db, tid, db.get(Outlet, fb.outlet_id), db.get(Bill, fb.bill_id), fb)
    _reply(phone, "Thank you, we've passed this on. We hope to see you again soon. 🙏")
    return True
