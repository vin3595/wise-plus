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
from ..models import Customer, Item, Outlet, Tenant, Variant
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


def item_label(db: Session, offer: Offer) -> str:
    v = db.get(Variant, offer.variant_id)
    it = db.get(Item, v.item_id) if v else None
    if it is None:
        return offer.name
    return (f"{it.name}{'' if v.name in ('Regular', '') else f' ({v.name})'}")[:120]


def clean_name(raw: str | None) -> str | None:
    n = re.sub(r"[\x00-\x1f<>]", "", (raw or "")).strip()
    n = re.sub(r"\s+", " ", n)[:60]
    return n or None


def remember(db: Session, offer: Offer, phone10: str, wa_name: str | None) -> str | None:
    """db: system session. The customer goes into this business's customers (name from their bills, else WhatsApp)."""
    cu = db.scalar(select(Customer).where(Customer.tenant_id == offer.tenant_id, Customer.phone == phone10))
    if cu is None:
        cu = Customer(tenant_id=offer.tenant_id, phone=phone10, name=wa_name, points_paise=0, stamps=0)
        db.add(cu)
        try:
            with db.begin_nested():
                db.flush()
        except Exception:  # noqa: BLE001 - added at the same moment elsewhere
            cu = db.scalar(select(Customer).where(Customer.tenant_id == offer.tenant_id, Customer.phone == phone10))
    elif wa_name and not cu.name:
        cu.name = wa_name
    return (cu.name if cu is not None else None) or wa_name


def blocked(db: Session, offer: Offer, phone10: str) -> str | None:
    """Why this number can't get a new code now (None = it can)."""
    day = business_date()
    if not live(offer, day):
        return "Sorry, this offer has ended."
    n = db.scalar(select(func.count()).select_from(OfferCode).where(OfferCode.offer_id == offer.id, OfferCode.phone10 == phone10)) or 0
    if n >= offer.per_phone:
        return "You've already used this offer. Thank you! 🙏"
    if offer.daily_limit is not None:
        today = db.scalar(select(func.count()).select_from(OfferCode).where(OfferCode.offer_id == offer.id, OfferCode.issued_on == day)) or 0
        if today >= offer.daily_limit:
            return "Today's codes for this offer are all taken. Try again tomorrow!"
    return None


def open_code(db: Session, offer: Offer, phone10: str) -> OfferCode | None:
    day = business_date()
    return db.scalar(select(OfferCode).where(OfferCode.offer_id == offer.id, OfferCode.phone10 == phone10, OfferCode.status == "issued",
                                             OfferCode.expires_on >= day).order_by(OfferCode.created_at.desc()))


def issue(db: Session, offer: Offer, phone10: str, wa_name: str | None = None, prepaid_ref: str | None = None,
          prepaid_paise: int | None = None) -> tuple[OfferCode | None, str]:
    """A code for this number (the same one again if it hasn't been used). Returns (code, message if none).
    The code keeps the customer's name and number, the item and the offer price. A prepaid code (paid online
    already) is always made: the money is in."""
    day = business_date()
    wa_name = clean_name(wa_name)
    if prepaid_ref is None:
        open_ = open_code(db, offer, phone10)
        if open_ is not None:
            if not open_.cust_name:
                open_.cust_name = remember(db, offer, phone10, wa_name)
            return open_, ""
        why = blocked(db, offer, phone10)
        if why:
            return None, why
    last = day + timedelta(days=max(offer.code_days, 1) - 1)
    if offer.valid_to and last > offer.valid_to and not prepaid_ref:
        last = offer.valid_to
    name, item = remember(db, offer, phone10, wa_name), item_label(db, offer)
    v = db.get(Variant, offer.variant_id)
    for _ in range(5):
        c = OfferCode(tenant_id=offer.tenant_id, offer_id=offer.id, code=new_code(), phone10=phone10, expires_on=last, issued_on=day,
                      cust_name=name, item_name=item, price_paise=offer.price_paise, regular_paise=v.price_paise if v else None,
                      prepaid_ref=prepaid_ref, prepaid_paise=prepaid_paise)
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
    what = code.item_name or item_label(db, offer)
    price = code.price_paise if code.price_paise is not None else offer.price_paise
    where = ""
    if offer.outlet_ids:
        names = [o.name for o in db.scalars(select(Outlet).where(Outlet.id.in_([uuid.UUID(x) for x in offer.outlet_ids])))]
        where = f"\nAt: {', '.join(names)}"
    hi = f"Hi {code.cust_name}! " if code.cust_name else ""
    if code.prepaid_ref:
        return (f"✅ {hi}Payment received. Your *{what}* is ready to collect at {t.app_name or t.name}.{where}\n\n"
                f"Show this QR at the counter, nothing more to pay.\nCode: *{code.code}* · valid till {code.expires_on.day} {code.expires_on:%b}"
                + (f"\n\n{offer.message}" if offer.message else ""))
    return (f"🎁 {hi}*{offer.name}*\n{what} for just *{rupees(price)}* at {t.app_name or t.name}.{where}\n\n"
            f"Show this QR at the counter.\nCode: *{code.code}* · valid till {code.expires_on.day} {code.expires_on:%b}"
            + (f"\n\n{offer.message}" if offer.message else ""))


def phone_fmt(p10: str) -> str:
    return f"+91 {p10[:5]} {p10[5:]}" if len(p10) == 10 else p10


_FONTS = __import__("pathlib").Path(__file__).parent / "fonts"


def qr_png(code: str, card: dict | None = None) -> bytes:
    """The QR alone, or (card) a picture with the business, the item and price. The customer's name and number aren't
    printed: they're kept with the code, so the counter sees them when it's scanned."""
    import segno
    buf = io.BytesIO()
    segno.make(code, error="m", micro=False).save(buf, kind="png", scale=12, border=3)
    if not card:
        return buf.getvalue()
    from PIL import Image, ImageDraw, ImageFont
    qr = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    W = 720
    qr = qr.resize((520, 520), Image.NEAREST)
    bold = lambda n: ImageFont.truetype(str(_FONTS / "DejaVuSans-Bold.ttf"), n)  # noqa: E731
    reg = lambda n: ImageFont.truetype(str(_FONTS / "DejaVuSans.ttf"), n)  # noqa: E731
    img = Image.new("RGB", (W, 1010), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 120], fill=(17, 24, 39))

    def fit(text: str, font_fn, size: int, width: int):
        while size > 16 and d.textlength(text, font=font_fn(size)) > width:
            size -= 2
        return font_fn(size)

    def center(y: int, text: str, font, fill=(17, 24, 39)):
        d.text(((W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    brand = card.get("brand") or ""
    center(36, brand, fit(brand, bold, 40, W - 60), (255, 255, 255))
    title = card.get("offer") or ""
    center(150, title, fit(title, bold, 36, W - 60))
    img.paste(qr, ((W - 520) // 2, 205))
    y = 745
    item = f"{card.get('item') or ''}  ·  {card.get('price') or ''}"
    center(y, item, fit(item, bold, 34, W - 60), (190, 18, 60))
    y += 62
    d.line([60, y, W - 60, y], fill=(229, 231, 235), width=2)
    y += 24
    y += 4
    center(y, f"Code {code}", bold(28), (75, 85, 99))
    y += 44
    center(y, card.get("valid") or "", reg(24), (107, 114, 128))
    y += 40
    center(y, "Show this at the counter · one use only", reg(22), (156, 163, 175))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def card_for(db: Session, c: OfferCode) -> dict:
    o = db.get(Offer, c.offer_id)
    t = db.get(Tenant, c.tenant_id)
    price = c.price_paise if c.price_paise is not None else (o.price_paise if o else 0)
    return {"brand": (t.app_name or t.name) if t else "", "offer": o.name if o else "", "item": c.item_name or (item_label(db, o) if o else ""),
            "price": "PAID ✓" if c.prepaid_ref else rupees(price),
            "valid": f"Valid till {c.expires_on.day} {c.expires_on:%b %Y}"}


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
    if o is None or (not live(o, day) and not c.prepaid_ref):        # a paid-for code is honoured till it expires
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
