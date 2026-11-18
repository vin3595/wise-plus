"""wise-plus: saving petty expenses. Shared by the app, the slip scanner and WhatsApp."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import cash_purpose
from ..core.stock import STANDARD_UNITS, unit_cost_k
from ..models import CashMovement, Shift, StockItem
from ..services import stock as st
from ..services import audit
from ..core.kds import business_date
from .models import PAID_FROM, PAID_LABEL, ExpenseVendor, PettyExpense, PettyTopup

CATEGORIES = ["Milk", "Ice", "Fruits", "Vegetables", "Grocery", "Gas", "Water", "Transport", "Repairs", "Cleaning",
              "Packaging", "Staff food", "Stationery", "Electricity", "Other"]


def clean_category(s: str | None) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())[:40]
    return (s[:1].upper() + s[1:]) if s else "Other"


def photo_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def factor(item: StockItem, unit: str | None) -> int | None:
    """How many base units (g, ml, pc) one `unit` is for this item, or None if it doesn't fit."""
    if not unit or unit.strip().lower() == item.unit:
        return 1
    u = unit.strip().lower()
    for p in item.purchase_units or []:
        if p["name"].strip().lower() == u:
            return int(p["factor"])
    std = STANDARD_UNITS.get(u)
    if std and std[0] == item.unit:
        return std[1]
    return None


def open_shift(db: Session, outlet_id: uuid.UUID) -> Shift | None:
    return db.scalar(select(Shift).where(Shift.outlet_id == outlet_id, Shift.status == "open").order_by(Shift.opened_at.desc()))


@dataclass
class New:
    outlet_id: uuid.UUID
    amount_paise: int
    category: str
    paid_from: str = "petty"
    what: str | None = None
    qty_m: int | None = None
    unit: str | None = None
    stock_item_id: uuid.UUID | None = None
    spent_on: date | None = None
    vendor_id: uuid.UUID | None = None
    file_path: str | None = None
    photo_hash: str | None = None
    source: str = "app"


def item_for(db: Session, category: str) -> StockItem | None:
    """The stock item this category went to last time (Milk → Milk), else one named like it."""
    sid = db.scalar(select(PettyExpense.stock_item_id).where(func.lower(PettyExpense.category) == category.lower(),
                                                              PettyExpense.stock_item_id.is_not(None), PettyExpense.status == "ok")
                    .order_by(PettyExpense.created_at.desc()).limit(1))
    if sid:
        return db.get(StockItem, sid)
    return db.scalar(select(StockItem).where(func.lower(StockItem.name) == category.lower(), StockItem.is_active.is_(True)).limit(1))


def last_rate(db: Session, category: str, outlet_id: uuid.UUID | None = None) -> dict | None:
    """The last time this was bought with a quantity: so ₹300 of milk can be read as 6 L."""
    q = select(PettyExpense).where(func.lower(PettyExpense.category) == category.lower(), PettyExpense.qty_m.is_not(None),
                                   PettyExpense.qty_m > 0, PettyExpense.status == "ok")
    if outlet_id:
        q = q.order_by((PettyExpense.outlet_id == outlet_id).desc(), PettyExpense.created_at.desc())
    else:
        q = q.order_by(PettyExpense.created_at.desc())
    e = db.scalar(q.limit(1))
    if e is None:
        return None
    return {"unit": e.unit, "qty_m": e.qty_m, "amount_paise": e.amount_paise, "stock_item_id": str(e.stock_item_id) if e.stock_item_id else None,
            "paise_per_unit": round(e.amount_paise * 1000 / e.qty_m)}


def guess_qty(db: Session, category: str, amount_paise: int, outlet_id=None) -> tuple[int | None, str | None]:
    r = last_rate(db, category, outlet_id)
    if not r or not r["paise_per_unit"]:
        return None, None
    q = round(amount_paise * 1000 / r["paise_per_unit"])
    # sensible rounding: whole units when close, else to 0.1
    q = round(q / 1000) * 1000 if abs(q - round(q / 1000) * 1000) <= 50 else round(q / 100) * 100
    return (q or None), r["unit"]


def add(db: Session, tenant_id: uuid.UUID, user_id: uuid.UUID, e: New) -> PettyExpense:
    if e.amount_paise <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter the amount")
    if e.paid_from not in PAID_FROM:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Paid from: petty cash, counter cash, owner or UPI")
    cat = clean_category(e.category)
    item = db.get(StockItem, e.stock_item_id) if e.stock_item_id else None
    base_m = None
    if item is not None and e.qty_m:
        f = factor(item, e.unit)
        if f is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{item.name} isn't counted in '{e.unit}'")
        base_m = e.qty_m * f
    x = PettyExpense(id=uuid.uuid4(), tenant_id=tenant_id, outlet_id=e.outlet_id, spent_on=e.spent_on or business_date(),
                     amount_paise=e.amount_paise, category=cat, what=(e.what or "").strip()[:120] or None, qty_m=e.qty_m or None,
                     unit=(e.unit or "").strip()[:20] or None, stock_item_id=item.id if item else None, base_m=base_m,
                     paid_from=e.paid_from, vendor_id=e.vendor_id, file_path=e.file_path, photo_hash=e.photo_hash, source=e.source,
                     created_by=user_id)
    if e.paid_from == "counter":
        sh = open_shift(db, e.outlet_id)
        if sh is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "No shift is open at this outlet: open one, or pay it from petty cash")
        cm = CashMovement(tenant_id=tenant_id, shift_id=sh.id, kind="out", amount_paise=e.amount_paise, purpose="expense", category=cat,
                          reason=cash_purpose.describe("out", "expense", cat, None, None, x.what or ""), note=x.what, created_by=user_id)
        db.add(cm)
        db.flush()
        x.cash_move_id = cm.id
    db.add(x)
    db.flush()
    if item is not None and base_m:
        cost = unit_cost_k(e.amount_paise, base_m)
        st.post(db, tenant_id=tenant_id, outlet_id=e.outlet_id, item=item, qty_m=base_m, kind="purchase", ref_kind="expense",
                ref_id=x.id, user_id=user_id, in_cost_k=cost, note=f"Petty: {cat}"[:120], day=x.spent_on)
        if cost:
            item.last_cost_k = cost
    audit.record(db, tenant_id, user_id, "create", "petty_expense", x.id,
                 after={"amount_paise": x.amount_paise, "category": cat, "paid_from": x.paid_from, "qty_m": x.qty_m, "unit": x.unit})
    return x


def cancel(db: Session, x: PettyExpense, user_id: uuid.UUID, reason: str) -> None:
    if x.status == "cancelled":
        return
    if x.stock_item_id and x.base_m:
        item = db.get(StockItem, x.stock_item_id)
        if item:
            st.post(db, tenant_id=x.tenant_id, outlet_id=x.outlet_id, item=item, qty_m=-x.base_m, kind="purchase", ref_kind="expense",
                    ref_id=x.id, user_id=user_id, note=f"Petty cancelled: {reason}"[:120])
    if x.cash_move_id:
        sh = open_shift(db, x.outlet_id)
        if sh is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This came out of counter cash: open a shift to put it back")
        db.add(CashMovement(tenant_id=x.tenant_id, shift_id=sh.id, kind="in", amount_paise=x.amount_paise, purpose="other",
                            reason=f"Expense cancelled: {x.category}"[:120], note=reason[:120], created_by=user_id))
    x.status, x.cancel_reason = "cancelled", reason[:200]
    db.flush()
    audit.record(db, x.tenant_id, user_id, "cancel", "petty_expense", x.id, after={"reason": reason})


def wallet(db: Session, outlet_id: uuid.UUID) -> dict:
    """The outlet's petty cash: put in, spent, in hand, and since the last top-up (spent, slips missing)."""
    put = db.scalar(select(func.coalesce(func.sum(PettyTopup.amount_paise), 0)).where(PettyTopup.outlet_id == outlet_id)) or 0
    spent = db.scalar(select(func.coalesce(func.sum(PettyExpense.amount_paise), 0)).where(
        PettyExpense.outlet_id == outlet_id, PettyExpense.paid_from == "petty", PettyExpense.status == "ok")) or 0
    last = db.scalar(select(PettyTopup).where(PettyTopup.outlet_id == outlet_id).order_by(PettyTopup.created_at.desc()).limit(1))
    q = select(PettyExpense).where(PettyExpense.outlet_id == outlet_id, PettyExpense.paid_from == "petty", PettyExpense.status == "ok")
    if last is not None:
        q = q.where(PettyExpense.created_at >= last.created_at)
    since = db.scalars(q).all()
    return {"in_hand_paise": put - spent, "put_in_paise": put, "spent_paise": spent,
            "last_topup": {"amount_paise": last.amount_paise, "at": last.created_at.isoformat()} if last else None,
            "since_topup_paise": sum(e.amount_paise for e in since), "since_topup_count": len(since),
            "since_topup_no_slip": sum(1 for e in since if not e.file_path)}


def topup(db: Session, tenant_id, user_id, outlet_id, amount_paise: int, source: str, note: str | None) -> PettyTopup:
    if amount_paise == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter the amount")
    t = PettyTopup(id=uuid.uuid4(), tenant_id=tenant_id, outlet_id=outlet_id, amount_paise=amount_paise, source=source,
                   note=(note or "").strip()[:120] or None, created_by=user_id)
    if source == "counter":
        sh = open_shift(db, outlet_id)
        if sh is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "No shift is open at this outlet to take the cash from")
        cm = CashMovement(tenant_id=tenant_id, shift_id=sh.id, kind="out" if amount_paise > 0 else "in", amount_paise=abs(amount_paise),
                          purpose="other", paid_to="Petty cash", reason="Petty cash top-up" if amount_paise > 0 else "Back from petty cash",
                          created_by=user_id)
        db.add(cm)
        db.flush()
        t.cash_move_id = cm.id
    db.add(t)
    db.flush()
    audit.record(db, tenant_id, user_id, "create", "petty_topup", t.id, after={"amount_paise": amount_paise, "source": source})
    return t


def out(db: Session, x: PettyExpense, names: dict | None = None) -> dict:
    return {"id": str(x.id), "outlet_id": str(x.outlet_id), "spent_on": x.spent_on.isoformat(), "amount_paise": x.amount_paise,
            "category": x.category, "what": x.what, "qty_m": x.qty_m, "unit": x.unit,
            "stock_item_id": str(x.stock_item_id) if x.stock_item_id else None,
            "stock_item": (names or {}).get(x.stock_item_id) if x.stock_item_id else None,
            "paid_from": x.paid_from, "paid_label": PAID_LABEL.get(x.paid_from, x.paid_from), "has_slip": bool(x.file_path),
            "vendor_id": str(x.vendor_id) if x.vendor_id else None, "source": x.source, "status": x.status,
            "cancel_reason": x.cancel_reason, "created_at": x.created_at.isoformat() if x.created_at else None}


def vendor_out(v: ExpenseVendor) -> dict:
    return {"id": str(v.id), "name": v.name, "outlet_id": str(v.outlet_id) if v.outlet_id else None, "category": v.category, "what": v.what,
            "qty_m": v.qty_m, "unit": v.unit, "stock_item_id": str(v.stock_item_id) if v.stock_item_id else None,
            "amount_paise": v.amount_paise, "paid_from": v.paid_from, "is_active": v.is_active}


def qty_text(qty_m: int | None, unit: str | None) -> str:
    if not qty_m:
        return ""
    q = qty_m / 1000
    return f"{q:g} {unit or ''}".strip()
