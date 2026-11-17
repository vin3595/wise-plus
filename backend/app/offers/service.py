"""Offer codes: given on WhatsApp for a keyword, checked and used at the counter."""
from __future__ import annotations

import io
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.kds import business_date
from ..models import Item, Outlet, Tenant, Variant
from .models import Offer, OfferCode

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"          # no 0/O, 1/I/L: easy to read out or type
CODE_RE = re.compile(r"^OF[2-9A-HJKMNP-Z]{8}$")


class OfferError(Exception):
    pass


def new_code() -> str:
    return "OF" + "".join(secrets.choice(ALPHABET) for _ in range(8))


def clean(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def is_code(raw: str) -> bool:
    return bool(CODE_RE.match(clean(raw)))


def norm_keyword(k: str) -> str:
    return re.sub(r"\s+", "", (k or "").upper())[:30]


def live(o: Offer, day: date) -> bool:
    return o.is_active and (o.valid_from is None or o.valid_from <= day) and (o.valid_to is None or o.valid_to >= day)


def rupees(p: int) -> str:
    return f"₹{p / 100:,.0f}" if p % 100 == 0 else f"₹{p / 100:,.2f}"


def find_keyword(db: Session, text: str) -> Offer | None:
    """db: a system session (one WhatsApp number serves every business; keywords are unique across all of them)."""
    k = norm_keyword(text)
    if not k or len(k) > 30:
        return None
    return db.scalar(select(Offer).where(Offer.keyword == k))


def issue(db: Session, offer: Offer, phone10: str) -> tuple[OfferCode | None, str]:
    """A code for this number (the same one again if it hasn't been used). Returns (code, message if none)."""
    day = business_date()
    if not live(offer, day):
        return None, "Sorry, this offer has ended."
    have = db.scalars(select(OfferCode).where(OfferCode.offer_id == offer.id, OfferCode.phone10 == phone10)
                      .order_by(OfferCode.created_at.desc())).all()
    open_ = next((c for c in have if c.status == "issued" and c.expires_on >= day), None)
    if open_ is not None:
        return open_, ""
    if len(have) >= offer.per_phone:
        return None, "You've already used this offer. Thank you! 🙏"
    if offer.daily_limit is not None:
        today = db.scalar(select(func.count()).select_from(OfferCode).where(OfferCode.offer_id == offer.id, OfferCode.issued_on == day)) or 0
        if today >= offer.daily_limit:
            return None, "Today's codes for this offer are all taken. Try again tomorrow!"
    last = day + timedelta(days=max(offer.code_days, 1) - 1)
    if offer.valid_to and last > offer.valid_to:
        last = offer.valid_to
    for _ in range(5):
        c = OfferCode(tenant_id=offer.tenant_id, offer_id=offer.id, code=new_code(), phone10=phone10, expires_on=last, issued_on=day)
        db.add(c)
        try:
            with db.begin_nested():
                db.flush()
            return c, ""
        except Exception:  # noqa: BLE001 - the one-in-a-trillion clash: try another code
            continue
    raise OfferError("Couldn't make a code")


def reply_text(db: Session, offer: Offer, code: OfferCode) -> str:
    t = db.get(Tenant, offer.tenant_id)
    v = db.get(Variant, offer.variant_id)
    it = db.get(Item, v.item_id) if v else None
    what = f"{it.name}{'' if not v or v.name in ('Regular', '') else f' ({v.name})'}" if it else offer.name
    where = ""
    if offer.outlet_ids:
        names = [o.name for o in db.scalars(select(Outlet).where(Outlet.id.in_([uuid.UUID(x) for x in offer.outlet_ids])))]
        where = f"\nAt: {', '.join(names)}"
    return (f"🎁 *{offer.name}*\n{what} for just *{rupees(offer.price_paise)}* at {t.app_name or t.name}.{where}\n\n"
            f"Show this QR at the counter.\nCode: *{code.code}* · valid till {code.expires_on.day} {code.expires_on:%b}"
            + (f"\n\n{offer.message}" if offer.message else ""))


def qr_png(code: str) -> bytes:
    import segno
    buf = io.BytesIO()
    segno.make(code, error="m", micro=False).save(buf, kind="png", scale=12, border=3)
    return buf.getvalue()


def check(db: Session, raw: str, outlet_id, lock: bool = False) -> tuple[OfferCode, Offer]:
    """The code a cashier scanned: still good, for this outlet, today? (tenant session: other businesses' codes don't exist)"""
    q = select(OfferCode).where(OfferCode.code == clean(raw))
    c = db.scalar(q.with_for_update() if lock else q)
    if c is None:
        raise OfferError("This code isn't valid here")
    o = db.get(Offer, c.offer_id)
    day = business_date()
    if c.status == "used":
        when = c.used_at.astimezone(timezone(timedelta(hours=5, minutes=30))) if c.used_at else None
        raise OfferError("This code was already used" + (f" on {when.day} {when:%b} at {when:%I:%M %p}" if when else ""))
    if c.expires_on < day:
        raise OfferError(f"This code expired on {c.expires_on.day} {c.expires_on:%b}")
    if o is None or not live(o, day):
        raise OfferError("This offer has ended")
    if o.outlet_ids and str(outlet_id) not in o.outlet_ids:
        raise OfferError("This offer isn't valid at this outlet")
    return c, o


VERIFY_MINUTES = 30


def otp_ident(c: OfferCode) -> str:
    return f"offer-{c.code}:{c.phone10}"         # the part after ':' is where the OTP goes


def needs_otp(db: Session, c: OfferCode) -> bool:
    o = db.get(Offer, c.offer_id)
    fresh = c.verified_at is not None and c.verified_at > datetime.now(timezone.utc) - timedelta(minutes=VERIFY_MINUTES)
    return bool(o and o.require_otp and not fresh)


def use(db: Session, c: OfferCode, outlet_id, order_id, kot_id) -> None:
    if needs_otp(db, c):
        raise OfferError("Check the customer first: tap Send OTP and enter the code they got on WhatsApp")
    c.status, c.used_at, c.outlet_id, c.order_id, c.kot_id = "used", datetime.now(timezone.utc), outlet_id, order_id, kot_id


def release(db: Session, order_id=None, kot_id=None) -> int:
    """An order or KOT cancelled before billing: its codes can be used again."""
    q = select(OfferCode).where(OfferCode.status == "used")
    q = q.where(OfferCode.order_id == order_id) if order_id else q.where(OfferCode.kot_id == kot_id)
    n = 0
    for c in db.scalars(q):
        c.status, c.used_at, c.order_id, c.kot_id, c.outlet_id = "issued", None, None, None, None
        n += 1
    return n
