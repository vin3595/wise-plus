"""Offers (Marketing › WhatsApp offers), the QR picture sent to customers, and barcodes (scan to bill, labels)."""
from __future__ import annotations

import random
import re
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.permissions import Perm
from ..db import system_session
from ..deps import Auth, ensure_outlet_access, get_db, require
from ..models import Item, Outlet, StockItem, Variant
from ..services import audit
from . import service as osv
from .models import Offer, OfferCode

router = APIRouter(tags=["offers", "barcodes"])
public = APIRouter(prefix="/public/offer-qr", tags=["offers"])


# ------------------------------------------------------------------ offers

class OfferIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    keyword: str = Field(min_length=3, max_length=30)
    variant_id: uuid.UUID
    price_paise: int = Field(ge=0, le=10_000_000)
    outlet_ids: list[uuid.UUID] | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    per_phone: int = Field(default=1, ge=1, le=100)
    code_days: int = Field(default=7, ge=1, le=365)
    daily_limit: int | None = Field(default=None, ge=1, le=100000)
    message: str | None = Field(default=None, max_length=300)
    is_active: bool = True


def _out(db: Session, o: Offer) -> dict:
    v = db.get(Variant, o.variant_id)
    it = db.get(Item, v.item_id) if v else None
    counts = dict(db.execute(select(OfferCode.status, func.count()).where(OfferCode.offer_id == o.id).group_by(OfferCode.status)).all())
    return {"id": str(o.id), "name": o.name, "keyword": o.keyword, "variant_id": str(o.variant_id),
            "item": f"{it.name}{'' if not v or v.name == 'Regular' else f' ({v.name})'}" if it else None,
            "regular_paise": v.price_paise if v else None, "price_paise": o.price_paise, "outlet_ids": o.outlet_ids,
            "valid_from": o.valid_from, "valid_to": o.valid_to, "per_phone": o.per_phone, "code_days": o.code_days,
            "daily_limit": o.daily_limit, "message": o.message, "is_active": o.is_active,
            "issued": sum(counts.values()), "used": counts.get("used", 0)}


def _apply(db: Session, o: Offer, body: OfferIn) -> None:
    if db.get(Variant, body.variant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    for oid in body.outlet_ids or []:
        if db.get(Outlet, oid) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")
    if body.valid_from and body.valid_to and body.valid_to < body.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The offer ends before it starts")
    kw = osv.norm_keyword(body.keyword)
    if not re.fullmatch(r"[A-Z0-9]{3,30}", kw):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Keyword: letters and numbers only, e.g. COFFEE29")
    o.name, o.keyword, o.variant_id, o.price_paise = body.name.strip(), kw, body.variant_id, body.price_paise
    o.outlet_ids = [str(x) for x in body.outlet_ids] if body.outlet_ids else None
    o.valid_from, o.valid_to, o.per_phone, o.code_days = body.valid_from, body.valid_to, body.per_phone, body.code_days
    o.daily_limit, o.message, o.is_active = body.daily_limit, (body.message or "").strip() or None, body.is_active


def _keyword_free(kw: str, mine: uuid.UUID | None) -> bool:
    with system_session() as sdb:            # keywords are unique across every business on this WhatsApp number
        other = sdb.scalar(select(Offer.id).where(Offer.keyword == osv.norm_keyword(kw)))
        return other is None or other == mine


@router.get("/offers")
def list_offers(db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.menu_view))) -> dict:
    from ..config import get_settings
    rows = db.scalars(select(Offer).order_by(Offer.created_at.desc())).all()
    return {"offers": [_out(db, o) for o in rows], "whatsapp_number": get_settings().wa_display_number or None}


@router.post("/offers", status_code=201)
def add_offer(body: OfferIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.menu_manage))) -> dict:
    if not _keyword_free(body.keyword, None):
        raise HTTPException(status.HTTP_409_CONFLICT, "That keyword is taken. Try another, e.g. with your brand's name")
    o = Offer(tenant_id=auth.tenant_id)
    _apply(db, o, body)
    db.add(o)
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "That keyword is taken. Try another") from None
    audit.record(db, auth.tenant_id, auth.user_id, "create", "offer", o.id, after={"keyword": o.keyword, "price_paise": o.price_paise})
    return _out(db, o)


@router.put("/offers/{offer_id}")
def edit_offer(offer_id: uuid.UUID, body: OfferIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.menu_manage))) -> dict:
    o = db.get(Offer, offer_id)
    if o is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer not found")
    if not _keyword_free(body.keyword, o.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "That keyword is taken. Try another")
    _apply(db, o, body)
    audit.record(db, auth.tenant_id, auth.user_id, "update", "offer", o.id, after={"keyword": o.keyword, "active": o.is_active})
    return _out(db, o)


@router.get("/offers/{offer_id}/codes")
def offer_codes(offer_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.menu_view))) -> list[dict]:
    rows = db.scalars(select(OfferCode).where(OfferCode.offer_id == offer_id).order_by(OfferCode.created_at.desc()).limit(500)).all()
    return [{"code": c.code, "phone": "…" + c.phone10[-4:], "status": c.status, "issued_on": c.issued_on, "expires_on": c.expires_on,
             "used_at": c.used_at} for c in rows]


@public.get("/{code}.png")
def qr_picture(code: str) -> Response:
    """The QR sent on WhatsApp. Only real codes get a picture (it's not a QR maker for anything)."""
    c = osv.clean(code)
    if not osv.is_code(c):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    with system_session() as db:
        if db.scalar(select(OfferCode.id).where(OfferCode.code == c)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return Response(osv.qr_png(c), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ------------------------------------------------------------------ barcodes

def ean13_check(d12: str) -> str:
    s = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d12))
    return str((10 - s % 10) % 10)


def valid_ean13(code: str) -> bool:
    return bool(re.fullmatch(r"\d{13}", code)) and ean13_check(code[:12]) == code[12]


def new_ean13(taken: set[str]) -> str:
    """In-store EAN-13: starts with 20-29 (GS1's range for a shop's own codes), so it never clashes with a maker's barcode."""
    while True:
        d = "2" + str(random.randint(0, 9)) + "".join(str(random.randint(0, 9)) for _ in range(10))
        c = d + ean13_check(d)
        if c not in taken:
            return c


def clean_barcode(raw: str | None) -> str | None:
    s = re.sub(r"\s+", "", raw or "")
    if not s:
        return None
    if not re.fullmatch(r"[A-Za-z0-9\-.]{4,32}", s):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A barcode is 4–32 letters or digits")
    if re.fullmatch(r"\d{13}", s) and not valid_ean13(s):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That EAN-13 barcode's last digit is wrong. Scan it again")
    return s


@router.get("/barcodes/lookup")
def lookup(code: str, outlet_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.kot_send))) -> dict:
    """What the scanner read: an offer code, an item size, or a stock item."""
    ensure_outlet_access(db, auth, outlet_id)
    raw = (code or "").strip()
    if osv.is_code(raw):
        try:
            c, o = osv.check(db, raw, outlet_id)
        except osv.OfferError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
        v = db.get(Variant, o.variant_id)
        return {"kind": "offer", "code": c.code, "offer": o.name, "variant_id": str(o.variant_id), "item_id": str(v.item_id),
                "price_paise": o.price_paise, "regular_paise": v.price_paise, "expires_on": c.expires_on}
    bc = re.sub(r"\s+", "", raw)
    v = db.scalar(select(Variant).where(Variant.barcode == bc))
    if v is not None:
        return {"kind": "variant", "variant_id": str(v.id), "item_id": str(v.item_id)}
    st = db.scalar(select(StockItem).where(StockItem.barcode == bc))
    if st is not None:
        return {"kind": "stock", "stock_item_id": str(st.id), "name": st.name}
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"No item has barcode {bc}. Link it under Menu › Barcodes")


class BarcodeIn(BaseModel):
    barcode: str | None = Field(default=None, max_length=40)


def _set(db: Session, model, row_id: uuid.UUID, raw: str | None, auth: Auth, what: str) -> dict:
    row = db.get(model, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")
    bc = clean_barcode(raw)
    if bc:
        other_v = db.scalar(select(Variant).where(Variant.barcode == bc))
        other_s = db.scalar(select(StockItem).where(StockItem.barcode == bc))
        clash = (other_v if other_v is not None and other_v.id != row.id else None) or (other_s if other_s is not None and other_s.id != row.id else None)
        if clash is not None:
            name = db.get(Item, clash.item_id).name if isinstance(clash, Variant) else clash.name
            raise HTTPException(status.HTTP_409_CONFLICT, f"That barcode is already on {name}")
    row.barcode = bc
    audit.record(db, auth.tenant_id, auth.user_id, "update", f"{what.lower().replace(' ', '_')}_barcode", row.id, after={"barcode": bc})
    return {"id": str(row.id), "barcode": bc}


@router.put("/barcodes/variants/{variant_id}")
def set_variant_barcode(variant_id: uuid.UUID, body: BarcodeIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.menu_manage))) -> dict:
    return _set(db, Variant, variant_id, body.barcode, auth, "Item")


@router.put("/barcodes/stock/{item_id}")
def set_stock_barcode(item_id: uuid.UUID, body: BarcodeIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    return _set(db, StockItem, item_id, body.barcode, auth, "Stock item")


class GenerateIn(BaseModel):
    variant_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
    stock_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)


@router.post("/barcodes/generate")
def generate(body: GenerateIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.menu_manage))) -> dict:
    """Give our own barcode to the chosen items that don't have one yet (an existing barcode is never replaced)."""
    taken = set(db.scalars(select(Variant.barcode).where(Variant.barcode.is_not(None)))) | \
        set(db.scalars(select(StockItem.barcode).where(StockItem.barcode.is_not(None))))
    made = 0
    for model, ids in ((Variant, body.variant_ids), (StockItem, body.stock_item_ids)):
        for i in ids:
            row = db.get(model, i)
            if row is not None and not row.barcode:
                row.barcode = new_ean13(taken)
                taken.add(row.barcode)
                made += 1
    audit.record(db, auth.tenant_id, auth.user_id, "update", "barcodes", auth.tenant_id, after={"made": made})
    return {"made": made}


@router.get("/barcodes")
def all_barcodes(outlet_id: uuid.UUID | None = None, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.menu_view))) -> dict:
    """Every item size and stock item with its barcode and price (for the Barcodes page and labels)."""
    from ..models import OutletVariantOverride
    over = {}
    if outlet_id:
        ensure_outlet_access(db, auth, outlet_id)
        over = {r.variant_id: r.price_paise for r in db.scalars(select(OutletVariantOverride).where(
            OutletVariantOverride.outlet_id == outlet_id, OutletVariantOverride.price_paise.is_not(None)))}
    items = []
    for v, it in db.execute(select(Variant, Item).join(Item, Item.id == Variant.item_id)
                            .where(Variant.is_active.is_(True), Item.is_active.is_(True)).order_by(Item.name, Variant.sort_order)):
        items.append({"variant_id": str(v.id), "name": it.name, "size": None if v.name == "Regular" else v.name,
                      "price_paise": over.get(v.id, v.price_paise), "barcode": v.barcode})
    stock = [{"stock_item_id": str(s.id), "name": s.name, "unit": s.unit, "barcode": s.barcode}
             for s in db.scalars(select(StockItem).where(StockItem.is_active.is_(True)).order_by(StockItem.name))]
    return {"items": items, "stock": stock}
