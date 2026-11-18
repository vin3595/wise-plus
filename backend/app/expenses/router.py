"""wise-plus: Money › Petty expenses. Loose slips (snap or type), the petty cash wallet, regular vendors."""
from __future__ import annotations


import os
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.kds import business_date
from ..core.permissions import Perm

from ..deps import Auth, ensure_outlet_access, get_db, require
from ..models import CashMovement, Outlet, StockItem
from . import service as svc
from .models import PAID_LABEL, ExpenseVendor, PettyExpense

router = APIRouter(prefix="/expenses", tags=["expenses"])
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "application/pdf": "pdf"}


def _folder(tenant_id) -> str:
    f = os.path.join(get_settings().upload_dir, "petty", str(tenant_id))
    os.makedirs(f, exist_ok=True)
    return f


def keep_slip(tenant_id, data: bytes, ct: str) -> str:
    """Saves the photo; returns a token (the file name) to attach when the entries are saved."""
    token = f"{uuid.uuid4().hex}.{EXT.get(ct, 'jpg')}"
    with open(os.path.join(_folder(tenant_id), token), "wb") as f:
        f.write(data)
    return token


def slip_path(tenant_id, token: str | None) -> str | None:
    if not token:
        return None
    name = os.path.basename(token)
    if name != token or not name.split(".")[0].isalnum():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Bad slip")
    p = os.path.join(_folder(tenant_id), name)
    if not os.path.exists(p):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That slip photo has expired: snap it again")
    return p


class ItemIn(BaseModel):
    amount_paise: int = Field(gt=0, le=10_000_000)
    category: str = Field(min_length=1, max_length=40)
    what: str | None = Field(default=None, max_length=120)
    qty_m: int | None = Field(default=None, ge=0, le=10**12)
    unit: str | None = Field(default=None, max_length=20)
    stock_item_id: uuid.UUID | None = None
    spent_on: date | None = None
    paid_from: str = Field(default="petty", pattern=r"^(petty|counter|owner|upi)$")


class SaveIn(BaseModel):
    outlet_id: uuid.UUID
    items: list[ItemIn] = Field(min_length=1, max_length=40)
    slip_token: str | None = Field(default=None, max_length=60)
    slip_hash: str | None = Field(default=None, max_length=64)


@router.get("")
def list_expenses(outlet_id: uuid.UUID | None = None, d1: date | None = None, d2: date | None = None, category: str | None = None,
                  paid_from: str | None = None, state: str = "ok", auth: Auth = Depends(require(Perm.billing)),
                  db: Session = Depends(get_db)) -> dict:
    q = select(PettyExpense)
    if outlet_id:
        ensure_outlet_access(db, auth, outlet_id)
        q = q.where(PettyExpense.outlet_id == outlet_id)
    if d1:
        q = q.where(PettyExpense.spent_on >= d1)
    if d2:
        q = q.where(PettyExpense.spent_on <= d2)
    if category:
        q = q.where(func.lower(PettyExpense.category) == category.lower())
    if paid_from:
        q = q.where(PettyExpense.paid_from == paid_from)
    if state in ("ok", "cancelled"):
        q = q.where(PettyExpense.status == state)
    rows = db.scalars(q.order_by(PettyExpense.spent_on.desc(), PettyExpense.created_at.desc()).limit(2000)).all()
    names = {i.id: i.name for i in db.scalars(select(StockItem).where(StockItem.id.in_({r.stock_item_id for r in rows if r.stock_item_id} or {None})))}
    ok = [r for r in rows if r.status == "ok"]
    by_cat: dict[str, dict] = {}
    for r in ok:
        c = by_cat.setdefault(r.category, {"category": r.category, "amount_paise": 0, "count": 0, "qty": {}})
        c["amount_paise"] += r.amount_paise
        c["count"] += 1
        if r.qty_m and r.unit:
            c["qty"][r.unit] = c["qty"].get(r.unit, 0) + r.qty_m
    by_from = {k: sum(r.amount_paise for r in ok if r.paid_from == k) for k in PAID_LABEL}
    return {"expenses": [svc.out(db, r, names) for r in rows],
            "total_paise": sum(r.amount_paise for r in ok), "no_slip": sum(1 for r in ok if not r.file_path),
            "by_category": sorted(by_cat.values(), key=lambda c: -c["amount_paise"]),
            "by_paid_from": [{"paid_from": k, "label": PAID_LABEL[k], "amount_paise": v} for k, v in by_from.items() if v]}


@router.get("/setup")
def setup(outlet_id: uuid.UUID | None = None, auth: Auth = Depends(require(Perm.billing)), db: Session = Depends(get_db)) -> dict:
    """Categories (yours first), stock items to link, regular vendors, and the outlet's petty cash."""
    used = [c for (c, _n) in db.execute(select(PettyExpense.category, func.count()).group_by(PettyExpense.category)
                                      .order_by(func.count().desc()).limit(30)).all()]
    used += [c for (c,) in db.execute(select(CashMovement.category).where(CashMovement.purpose == "expense", CashMovement.category.is_not(None))
                                      .distinct().limit(30)).all() if c not in used]
    cats = used + [c for c in svc.CATEGORIES if c not in used]
    items = [{"id": str(i.id), "name": i.name, "unit": i.unit, "units": [u["name"] for u in (i.purchase_units or [])]}
             for i in db.scalars(select(StockItem).where(StockItem.is_active.is_(True), StockItem.kind == "raw").order_by(StockItem.name))]
    vend = db.scalars(select(ExpenseVendor).where(ExpenseVendor.is_active.is_(True)).order_by(ExpenseVendor.sort, ExpenseVendor.name)).all()
    if outlet_id:
        ensure_outlet_access(db, auth, outlet_id)
        vend = [v for v in vend if v.outlet_id in (None, outlet_id)]
    links = {}
    for c in cats[:40]:
        it = svc.item_for(db, c)
        r = svc.last_rate(db, c, outlet_id)
        if it or r:
            links[c] = {"stock_item_id": str(it.id) if it else (r or {}).get("stock_item_id"), "rate": r}
    return {"categories": cats, "stock_items": items, "vendors": [svc.vendor_out(v) for v in vend], "links": links,
            "wallet": svc.wallet(db, outlet_id) if outlet_id else None, "paid_from": PAID_LABEL}


@router.post("", status_code=201)
def save(body: SaveIn, auth: Auth = Depends(require(Perm.billing)), db: Session = Depends(get_db)) -> dict:
    ensure_outlet_access(db, auth, body.outlet_id)
    if db.get(Outlet, body.outlet_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")
    path = slip_path(auth.tenant_id, body.slip_token)
    made = [svc.add(db, auth.tenant_id, auth.user_id, svc.New(
        outlet_id=body.outlet_id, amount_paise=i.amount_paise, category=i.category, what=i.what, qty_m=i.qty_m or None, unit=i.unit,
        stock_item_id=i.stock_item_id, spent_on=i.spent_on, paid_from=i.paid_from, file_path=path, photo_hash=body.slip_hash,
        source="scan" if path else "app")) for i in body.items]
    return {"saved": len(made), "total_paise": sum(m.amount_paise for m in made), "expenses": [svc.out(db, m) for m in made],
            "wallet": svc.wallet(db, body.outlet_id)}


@router.post("/scan")
async def scan(outlet_id: uuid.UUID = Form(...), file: UploadFile = File(...), auth: Auth = Depends(require(Perm.billing)),
               db: Session = Depends(get_db)) -> dict:
    """Read a photo of slips into entries to check and save."""
    from starlette.concurrency import run_in_threadpool

    from .reader import SlipError, read
    ensure_outlet_access(db, auth, outlet_id)
    data = await file.read()
    if not data or len(data) > 15_000_000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Send a photo under 15 MB")
    ct = (file.content_type or "").split(";")[0].lower()
    h = svc.photo_hash(data)
    seen = db.scalar(select(PettyExpense).where(PettyExpense.photo_hash == h, PettyExpense.status == "ok").limit(1))
    try:
        got = await read(data, ct, file.filename or "slip", business_date())
    except SlipError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    token = await run_in_threadpool(keep_slip, auth.tenant_id, data, ct)
    return {"slip_token": token, "slip_hash": h, "has_gstin": got["has_gstin"], "note": got["note"],
            "already": {"spent_on": seen.spent_on.isoformat(), "amount_paise": seen.amount_paise, "category": seen.category} if seen else None,
            "drafts": [draft(db, s, outlet_id) for s in got["slips"]]}


def draft(db: Session, s: dict, outlet_id) -> dict:
    """A read line, with the stock item and quantity filled in from last time when the slip didn't say."""
    it = svc.item_for(db, s["category"])
    if not s.get("qty_m"):
        q, u = svc.guess_qty(db, s["category"], s["amount_paise"], outlet_id)
        if q:
            s = s | {"qty_m": q, "unit": u, "qty_guessed": True}
    return s | {"stock_item_id": str(it.id) if it else None, "paid_from": "petty"}


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@router.post("/{expense_id}/cancel")
def cancel(expense_id: uuid.UUID, body: CancelIn, auth: Auth = Depends(require(Perm.approve)), db: Session = Depends(get_db)) -> dict:
    x = db.get(PettyExpense, expense_id)
    if x is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    ensure_outlet_access(db, auth, x.outlet_id)
    svc.cancel(db, x, auth.user_id, body.reason.strip())
    return svc.out(db, x)


@router.get("/{expense_id}/slip")
def slip(expense_id: uuid.UUID, auth: Auth = Depends(require(Perm.billing)), db: Session = Depends(get_db)) -> FileResponse:
    x = db.get(PettyExpense, expense_id)
    if x is None or not x.file_path or not os.path.exists(x.file_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No slip photo")
    ensure_outlet_access(db, auth, x.outlet_id)
    ext = x.file_path.rsplit(".", 1)[-1]
    return FileResponse(x.file_path, media_type={v: k for k, v in EXT.items()}.get(ext, "application/octet-stream"),
                        filename=f"slip-{x.spent_on}.{ext}")


# ------------------------------------------------------------------ petty cash wallet

class TopupIn(BaseModel):
    outlet_id: uuid.UUID
    amount_paise: int = Field(ge=-10_000_000, le=10_000_000)
    source: str = Field(default="owner", pattern=r"^(owner|counter|bank)$")
    note: str | None = Field(default=None, max_length=120)


@router.post("/topups", status_code=201)
def topup(body: TopupIn, auth: Auth = Depends(require(Perm.approve)), db: Session = Depends(get_db)) -> dict:
    ensure_outlet_access(db, auth, body.outlet_id)
    svc.topup(db, auth.tenant_id, auth.user_id, body.outlet_id, body.amount_paise, body.source, body.note)
    return svc.wallet(db, body.outlet_id)


# ------------------------------------------------------------------ regular vendors

class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    outlet_id: uuid.UUID | None = None
    category: str = Field(min_length=1, max_length=40)
    what: str | None = Field(default=None, max_length=120)
    qty_m: int | None = Field(default=None, ge=0, le=10**12)
    unit: str | None = Field(default=None, max_length=20)
    stock_item_id: uuid.UUID | None = None
    amount_paise: int = Field(gt=0, le=10_000_000)
    paid_from: str = Field(default="petty", pattern=r"^(petty|counter|owner|upi)$")
    is_active: bool = True


def _apply(v: ExpenseVendor, b: VendorIn) -> None:
    v.name, v.outlet_id, v.category, v.what = b.name.strip(), b.outlet_id, svc.clean_category(b.category), (b.what or "").strip() or None
    v.qty_m, v.unit, v.stock_item_id = b.qty_m or None, (b.unit or "").strip() or None, b.stock_item_id
    v.amount_paise, v.paid_from, v.is_active = b.amount_paise, b.paid_from, b.is_active


@router.get("/vendors")
def vendors(auth: Auth = Depends(require(Perm.billing)), db: Session = Depends(get_db)) -> list[dict]:
    return [svc.vendor_out(v) for v in db.scalars(select(ExpenseVendor).order_by(ExpenseVendor.sort, ExpenseVendor.name))]


@router.post("/vendors", status_code=201)
def add_vendor(body: VendorIn, auth: Auth = Depends(require(Perm.approve)), db: Session = Depends(get_db)) -> dict:
    v = ExpenseVendor(id=uuid.uuid4(), tenant_id=auth.tenant_id, name="", category="", amount_paise=1)
    _apply(v, body)
    db.add(v)
    db.flush()
    return svc.vendor_out(v)


@router.put("/vendors/{vendor_id}")
def edit_vendor(vendor_id: uuid.UUID, body: VendorIn, auth: Auth = Depends(require(Perm.approve)), db: Session = Depends(get_db)) -> dict:
    v = db.get(ExpenseVendor, vendor_id)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _apply(v, body)
    return svc.vendor_out(v)


class UseIn(BaseModel):
    outlet_id: uuid.UUID
    amount_paise: int | None = Field(default=None, gt=0, le=10_000_000)
    qty_m: int | None = Field(default=None, ge=0, le=10**12)
    paid_from: str | None = Field(default=None, pattern=r"^(petty|counter|owner|upi)$")
    spent_on: date | None = None


@router.post("/vendors/{vendor_id}/use", status_code=201)
def use_vendor(vendor_id: uuid.UUID, body: UseIn, auth: Auth = Depends(require(Perm.billing)), db: Session = Depends(get_db)) -> dict:
    """Today's delivery from a regular, in one tap (change the amount or quantity if it was different)."""
    v = db.get(ExpenseVendor, vendor_id)
    if v is None or not v.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    ensure_outlet_access(db, auth, body.outlet_id)
    qty = body.qty_m if body.qty_m is not None else v.qty_m
    amt = body.amount_paise or v.amount_paise
    if body.amount_paise and body.qty_m is None and v.qty_m and v.amount_paise:   # a different amount: same rate, more or less of it
        qty = round(v.qty_m * body.amount_paise / v.amount_paise)
    x = svc.add(db, auth.tenant_id, auth.user_id, svc.New(
        outlet_id=body.outlet_id, amount_paise=amt, category=v.category, what=v.what or v.name, qty_m=qty, unit=v.unit,
        stock_item_id=v.stock_item_id, spent_on=body.spent_on, paid_from=body.paid_from or v.paid_from, vendor_id=v.id, source="vendor"))
    return {"expense": svc.out(db, x), "wallet": svc.wallet(db, body.outlet_id)}
