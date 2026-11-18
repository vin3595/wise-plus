"""wise-plus: prepaid WhatsApp offers. The keyword gets the offer and a Razorpay payment link (the restaurant's own
Razorpay account, set under Payments); once paid, the QR comes on WhatsApp and the counter just hands the item over:
the bill is saved as paid by UPI with the Razorpay payment id.

Razorpay Payment Links: https://razorpay.com/docs/api/payments/payment-links/ — webhook event payment_link.paid.
'test' (Payments not set up for real money) makes a practice link on our own page instead."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PaymentSettings, Tenant
from . import service as osv
from .models import Offer, OfferCode, OfferPayment

LINK_HOURS = 24


class PayError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def provider(cfg: PaymentSettings | None) -> str | None:
    """razorpay (real money), test (practice) or None (online payment isn't set up)."""
    if cfg is None:
        return "test"
    if cfg.provider == "razorpay" and cfg.key_id and cfg.key_secret:
        return "razorpay"
    if cfg.provider == "test":
        return "test"
    return None


def _client(cfg: PaymentSettings):
    import httpx
    return httpx.Client(base_url="https://api.razorpay.com/v1", auth=(cfg.key_id or "", cfg.key_secret or ""), timeout=10)


def _create_link(cfg: PaymentSettings, op: OfferPayment, offer: Offer, tenant: Tenant, item: str) -> None:
    if op.provider == "test":
        op.link_id = f"test_{op.id.hex[:20]}"
        op.short_url = f"{get_settings().public_base_url.rstrip('/')}/api/public/offer-pay/{op.id}"
        return
    body = {"amount": op.amount_paise, "currency": "INR", "accept_partial": False,
            "description": f"{offer.name}: {item}"[:2000], "reference_id": op.id.hex[:40],
            "expire_by": int(op.expires_at.timestamp()),
            "customer": {"contact": f"+91{op.phone10}", **({"name": op.cust_name[:50]} if op.cust_name else {})},
            "notify": {"sms": False, "email": False}, "reminder_enable": False,
            "notes": {"wise_offer_payment": str(op.id), "offer": offer.keyword, "business": (tenant.app_name or tenant.name)[:200]}}
    try:
        with _client(cfg) as c:
            r = c.post("/payment_links", json=body)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:  # noqa: BLE001 - network, timeout
        raise PayError(f"Couldn't reach Razorpay ({e.__class__.__name__})") from None
    if r.status_code >= 400 or not data.get("short_url"):
        raise PayError((data.get("error") or {}).get("description") or f"Razorpay said {r.status_code}")
    op.link_id, op.short_url = data["id"], data["short_url"]


def start(db: Session, offer: Offer, phone10: str, wa_name: str | None) -> tuple[str, OfferCode | None]:
    """db: system session. What to send back for the keyword: (text, a paid code whose QR to send instead)."""
    tenant = db.get(Tenant, offer.tenant_id)
    have = osv.open_code(db, offer, phone10)
    if have is not None:                                  # paid already and not used: the QR again
        return "", have
    op = db.scalar(select(OfferPayment).where(OfferPayment.offer_id == offer.id, OfferPayment.phone10 == phone10,
                                              OfferPayment.status == "created", OfferPayment.expires_at > _now() + timedelta(minutes=10))
                   .order_by(OfferPayment.created_at.desc()))
    cfg = db.get(PaymentSettings, offer.tenant_id)
    if op is not None and refresh(db, cfg, op):           # they paid but the webhook hasn't come (or isn't set up)
        return "", db.get(OfferCode, op.code_id)
    if op is None:
        why = osv.blocked(db, offer, phone10)
        if why:
            return why, None
        prov = provider(cfg)
        if prov == "test" and not _staff(db, offer.tenant_id, phone10):
            prov = None                                   # practice links are only for the business's own people
        if prov is None:
            return "Sorry, online payment for this offer isn't available right now. Please try again later.", None
        op = OfferPayment(id=uuid.uuid4(), tenant_id=offer.tenant_id, offer_id=offer.id, phone10=phone10, provider=prov,
                          cust_name=osv.remember(db, offer, phone10, osv.clean_name(wa_name)), amount_paise=offer.price_paise,
                          expires_at=_now() + timedelta(hours=LINK_HOURS))
        try:
            _create_link(cfg, op, offer, tenant, osv.item_label(db, offer))
        except PayError:
            return "Sorry, we couldn't make a payment link just now. Please send the keyword again in a minute.", None
        db.add(op)
        db.flush()
    return pay_text(db, offer, op), None


def _staff(db: Session, tenant_id, phone10: str) -> bool:
    from ..models import User
    return db.scalar(select(User.id).where(User.tenant_id == tenant_id, User.phone.like(f"%{phone10}"), User.is_active)) is not None


def pay_text(db: Session, offer: Offer, op: OfferPayment) -> str:
    from ..models import Variant
    t = db.get(Tenant, offer.tenant_id)
    v = db.get(Variant, offer.variant_id)
    item = osv.item_label(db, offer)
    usual = f" (usual price {osv.rupees(v.price_paise)})" if v and v.price_paise > offer.price_paise else ""
    hi = f"Hi {op.cust_name}! " if op.cust_name else ""
    test = "\n\n_This is a practice link: no real money._" if op.provider == "test" else ""
    return (f"🎁 {hi}*{offer.name}*\n{item} for just *{osv.rupees(offer.price_paise)}*{usual} at {t.app_name or t.name}.\n\n"
            f"Pay here: {op.short_url}\n\nYour QR comes on this chat as soon as you pay. Show it at the counter and "
            f"collect your {item}, nothing more to pay. Link works for {LINK_HOURS} hours."
            + (f"\n\n{offer.message}" if offer.message else "") + test)


def paid(db: Session, op: OfferPayment, payment_id: str, amount_paise: int, fee_paise: int | None = None) -> OfferCode | None:
    """The money came in (webhook, check, or practice page): the QR code, once. Returns the new code (None if done before)."""
    if op.status == "paid":
        return None
    op.payment_id, op.paid_at, op.fee_paise = payment_id[:40], _now(), fee_paise
    if amount_paise != op.amount_paise:
        op.status, op.problem = "problem", f"Paid ₹{amount_paise / 100:g} but the offer is ₹{op.amount_paise / 100:g}"
        return None
    offer = db.get(Offer, op.offer_id)
    code, _ = osv.issue(db, offer, op.phone10, op.cust_name, prepaid_ref=op.payment_id, prepaid_paise=amount_paise)
    op.status, op.code_id = "paid", code.id if code else None
    db.flush()
    return code


def refresh(db: Session, cfg: PaymentSettings | None, op: OfferPayment) -> bool:
    """Ask Razorpay whether the link was paid. True if it was (and the code is made)."""
    if op.provider != "razorpay" or not op.link_id or cfg is None:
        return False
    try:
        with _client(cfg) as c:
            r = c.get(f"/payment_links/{op.link_id}")
        data = r.json() if r.status_code < 400 else {}
    except Exception:  # noqa: BLE001
        return False
    if data.get("status") != "paid":
        return False
    pays = [p for p in data.get("payments") or [] if p.get("status") in ("captured", "authorized")]
    if not pays:
        return False
    p = pays[0]
    paid(db, op, str(p.get("payment_id") or p.get("id")), int(p.get("amount") or data.get("amount_paid") or 0))
    return op.status == "paid"


def from_webhook(event: dict) -> tuple[str, str, int, int | None] | None:
    """(link id, payment id, amount, fee incl. GST) from a payment_link.paid event."""
    if event.get("event") != "payment_link.paid":
        return None
    pl = event.get("payload") or {}
    link = (pl.get("payment_link") or {}).get("entity") or {}
    pay = (pl.get("payment") or {}).get("entity") or {}
    if not link.get("id") or not pay.get("id") or pay.get("status") not in ("captured", "authorized"):
        return None
    fee = pay.get("fee")
    return str(link["id"]), str(pay["id"]), int(pay.get("amount") or 0), int(fee) if fee is not None else None


def send_qr(phone10: str, code_id: uuid.UUID) -> None:
    """After the payment is saved: the QR on WhatsApp (separate session; a failure here doesn't undo the payment)."""
    from ..db import system_session
    from ..whatsapp import client
    with system_session() as db:
        c = db.get(OfferCode, code_id)
        if c is None:
            return
        offer = db.get(Offer, c.offer_id)
        body = osv.reply_text(db, offer, c)
        link = f"{get_settings().public_base_url.rstrip('/')}/api/public/offer-qr/{c.code}.png"
        tid, code_str = c.tenant_id, c.code
    with client.context(tenant_id=tid, purpose="offer", ref=code_str):
        try:
            client.send_image("91" + phone10, link, body)
        except Exception:  # noqa: BLE001
            try:
                client.send_text("91" + phone10, body)
            except Exception:  # noqa: BLE001
                pass
