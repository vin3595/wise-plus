"""Credit notes on a bill (refunds / returns), supplier credit notes, and purchase returns (goods sent back)."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.billing import fiscal_year_start
from ..core.kds import business_date
from ..core.permissions import Perm
from ..deps import Auth, ensure_outlet_access, get_db, require
from ..models import Bill, BillLine, CashMovement, Outlet, Purchase, Shift, StockItem, Tenant
from ..services import audit
from ..services.approval import verify_manager_pin
from ..services.billing import fy_counter_date
from ..services.orders import next_number
from .models import REFUND_MODES, CreditNote

router = APIRouter(tags=["credit notes"])


def cn_number(code: str, fy: int, seq: int) -> str:
    n = f"CN/{code}/{fy % 100:02d}{(fy + 1) % 100:02d}{seq:04d}"
    if len(n) > 16:
        raise ValueError("Credit note number too long")
    return n


def _given(db: Session, bill_id) -> dict[str, int]:
    """Quantity already credited per bill line."""
    out: dict[str, int] = {}
    for cn in db.scalars(select(CreditNote).where(CreditNote.bill_id == bill_id, CreditNote.status == "issued")):
        for ln in cn.lines:
            out[ln["bill_line_id"]] = out.get(ln["bill_line_id"], 0) + ln["qty"]
    return out


def note_out(cn: CreditNote, bill: Bill | None = None) -> dict:
    return {"id": str(cn.id), "number": cn.number, "date": cn.note_date, "bill_id": str(cn.bill_id),
            "invoice_no": bill.invoice_no if bill else None, "reason": cn.reason, "lines": cn.lines,
            "taxable_paise": cn.taxable_paise, "cgst_paise": cn.cgst_paise, "sgst_paise": cn.sgst_paise, "igst_paise": cn.igst_paise,
            "round_off_paise": cn.round_off_paise, "total_paise": cn.total_paise, "refund_mode": cn.refund_mode, "status": cn.status,
            "outlet_id": str(cn.outlet_id), "created_at": cn.created_at}


@router.get("/bills/{bill_id}/returnable")
def returnable(bill_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.billing))) -> dict:
    bill = db.get(Bill, bill_id)
    if bill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found")
    ensure_outlet_access(db, auth, bill.outlet_id)
    given = _given(db, bill.id)
    lines = db.scalars(select(BillLine).where(BillLine.bill_id == bill.id)).all()
    notes = db.scalars(select(CreditNote).where(CreditNote.bill_id == bill.id).order_by(CreditNote.created_at)).all()
    return {"invoice_no": bill.invoice_no, "status": bill.status, "total_paise": bill.total_paise,
            "lines": [{"id": str(l.id), "item_name": l.item_name, "variant_name": l.variant_name, "qty": l.qty,
                       "left": l.qty - given.get(str(l.id), 0), "unit_total_paise": (l.taxable_paise + l.cgst_paise + l.sgst_paise + l.igst_paise) // max(l.qty, 1)}
                      for l in lines],
            "notes": [note_out(n, bill) for n in notes]}


class CnLine(BaseModel):
    bill_line_id: uuid.UUID
    qty: int = Field(ge=1, le=10000)


class CnIn(BaseModel):
    lines: list[CnLine] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=200)
    refund_mode: str = Field(pattern=r"^(cash|upi|card|bank|none)$")
    manager_pin: str | None = None


@router.post("/bills/{bill_id}/credit-notes", status_code=201)
def create(bill_id: uuid.UUID, body: CnIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.billing))) -> dict:
    bill = db.get(Bill, bill_id, with_for_update=True)
    if bill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found")
    ensure_outlet_access(db, auth, bill.outlet_id)
    if bill.status != "paid":
        raise HTTPException(status.HTTP_409_CONFLICT, "This bill is cancelled; there's nothing to give back")
    approver = auth.user_id if auth.can(Perm.approve) else verify_manager_pin(db, auth.tenant_id, bill.outlet_id, body.manager_pin).id
    lines = {l.id: l for l in db.scalars(select(BillLine).where(BillLine.bill_id == bill.id))}
    given = _given(db, bill.id)
    out, tot = [], {"taxable": 0, "cgst": 0, "sgst": 0, "igst": 0}
    for c in body.lines:
        l = lines.get(c.bill_line_id)
        if l is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That item isn't on this bill")
        left = l.qty - given.get(str(l.id), 0)
        if c.qty > left:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Only {left} × {l.item_name} can still be returned")
        part = lambda v: (v * c.qty * 2 + l.qty) // (2 * l.qty)   # noqa: E731  (the same share of each amount, rounded)
        row = {"bill_line_id": str(l.id), "item_name": l.item_name, "variant_name": l.variant_name, "hsn_sac": l.hsn_sac, "qty": c.qty,
               "rate_bp": l.rate_bp, "taxable": part(l.taxable_paise), "cgst": part(l.cgst_paise), "sgst": part(l.sgst_paise), "igst": part(l.igst_paise)}
        given[str(l.id)] = given.get(str(l.id), 0) + c.qty
        for k in tot:
            tot[k] += row[k]
        out.append(row)
    exact = sum(tot.values())
    total = (exact + 50) // 100 * 100
    tenant = db.get(Tenant, auth.tenant_id)
    outlet = db.get(Outlet, bill.outlet_id)
    day = business_date()
    fy = fiscal_year_start(day, tenant.fy_start_month)
    seq = next_number(db, auth.tenant_id, outlet.id, fy_counter_date(fy, tenant.fy_start_month), "cn")
    cn = CreditNote(tenant_id=auth.tenant_id, outlet_id=outlet.id, bill_id=bill.id, fy_start=fy, seq=seq,
                    number=cn_number(outlet.code, fy, seq), note_date=day, reason=body.reason.strip(), lines=out,
                    taxable_paise=tot["taxable"], cgst_paise=tot["cgst"], sgst_paise=tot["sgst"], igst_paise=tot["igst"],
                    round_off_paise=total - exact, total_paise=total, refund_mode=body.refund_mode, created_by=auth.user_id, approved_by=approver)
    db.add(cn)
    db.flush()
    if body.refund_mode == "cash":
        # Cash handed back comes out of the drawer that's open at this outlet (so the drawer still adds up).
        shift = db.scalar(select(Shift).where(Shift.outlet_id == outlet.id, Shift.status == "open").order_by(Shift.opened_at.desc()).limit(1))
        if shift is not None:
            m = CashMovement(tenant_id=auth.tenant_id, shift_id=shift.id, kind="out", amount_paise=total, reason=f"Refund {cn.number}"[:120],
                             purpose="other", note=f"Credit note on {bill.invoice_no}"[:120], created_by=auth.user_id)
            db.add(m)
            db.flush()
            cn.cash_movement_id = m.id
    audit.record(db, auth.tenant_id, auth.user_id, "create", "credit_note", cn.id,
                 after={"bill": bill.invoice_no, "total_paise": total, "approved_by": str(approver)})
    return note_out(cn, bill)


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@router.post("/credit-notes/{cn_id}/cancel")
def cancel(cn_id: uuid.UUID, body: CancelIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.approve))) -> dict:
    cn = db.get(CreditNote, cn_id)
    if cn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credit note not found")
    ensure_outlet_access(db, auth, cn.outlet_id)
    if cn.status == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "Already cancelled")
    cn.status = "cancelled"
    if cn.cash_movement_id:
        m = db.get(CashMovement, cn.cash_movement_id)
        shift = db.get(Shift, m.shift_id) if m else None
        if m is not None and shift is not None and shift.status == "open":
            db.add(CashMovement(tenant_id=cn.tenant_id, shift_id=shift.id, kind="in", amount_paise=m.amount_paise,
                                reason=f"Refund {cn.number} cancelled"[:120], purpose="other", created_by=auth.user_id))
    audit.record(db, auth.tenant_id, auth.user_id, "cancel", "credit_note", cn.id, before={"status": "issued"}, after={"reason": body.reason})
    return note_out(cn, db.get(Bill, cn.bill_id))


@router.get("/credit-notes")
def list_notes(outlet_id: uuid.UUID | None = None, d1: date | None = None, d2: date | None = None,
               db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> list[dict]:
    q = select(CreditNote, Bill).join(Bill, Bill.id == CreditNote.bill_id)
    if outlet_id:
        ensure_outlet_access(db, auth, outlet_id)
        q = q.where(CreditNote.outlet_id == outlet_id)
    if d1:
        q = q.where(CreditNote.note_date >= d1)
    if d2:
        q = q.where(CreditNote.note_date <= d2)
    return [note_out(cn, b) for cn, b in db.execute(q.order_by(CreditNote.note_date.desc(), CreditNote.seq.desc()).limit(1000)).all()]


def day_refunds(db: Session, day: date, outlet_id=None) -> int:
    """Credit notes given on a business day (for the closing summary)."""
    from sqlalchemy import func
    q = select(func.sum(CreditNote.total_paise)).where(CreditNote.note_date == day, CreditNote.status == "issued")
    if outlet_id:
        q = q.where(CreditNote.outlet_id == outlet_id)
    return int(db.scalar(q) or 0)


# ------------------------------------------------------------------ purchase returns (goods back to the supplier)

class ReturnLine(BaseModel):
    stock_item_id: uuid.UUID
    qty_m: int = Field(gt=0)            # in the unit it was bought in on that bill


class ReturnIn(BaseModel):
    lines: list[ReturnLine] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=200)
    entry_date: date | None = None


@router.post("/inventory/purchases/{purchase_id}/return", status_code=201)
def purchase_return(purchase_id: uuid.UUID, body: ReturnIn, db: Session = Depends(get_db),
                    auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    """Goods sent back: stock goes down, and a debit note (with its GST) reduces what's owed to the supplier."""
    from ..ledger.models import SupplierEntry
    from ..models import StockMove
    from ..services import stock as st
    p = db.get(Purchase, purchase_id, with_for_update=True)
    if p is None or p.status != "posted":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")
    ensure_outlet_access(db, auth, p.outlet_id)
    if p.supplier_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This bill has no supplier, so there's no one to return it to")
    back: dict[str, int] = {}
    for mv in db.scalars(select(StockMove).where(StockMove.ref_kind == "preturn", StockMove.ref_id == p.id)):
        back[str(mv.stock_item_id)] = back.get(str(mv.stock_item_id), 0) - mv.qty_m       # base units already returned
    value, tax, rows = 0, 0, []
    by_item = {ln["stock_item_id"]: ln for ln in p.lines}
    for r in body.lines:
        ln = by_item.get(str(r.stock_item_id))
        if ln is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That item isn't on this bill")
        base = r.qty_m * ln["factor"]
        if base + back.get(ln["stock_item_id"], 0) > ln["base_m"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"More {ln['name']} than this bill had (or already returned)")
        amt = (ln["amount_paise"] * r.qty_m * 2 + ln["qty_m"]) // (2 * ln["qty_m"])
        t = (amt * ln["gst_bp"] + 5000) // 10000
        value, tax = value + amt, tax + t
        it = db.get(StockItem, r.stock_item_id)
        st.post(db, tenant_id=auth.tenant_id, outlet_id=p.outlet_id, item=it, qty_m=-base, kind="purchase", ref_kind="preturn",
                ref_id=p.id, user_id=auth.user_id, note=f"Returned: {p.invoice_no or ''} {body.reason}"[:200])
        rows.append({"name": ln["name"], "qty_m": r.qty_m, "unit": ln["unit"], "amount_paise": amt, "tax_paise": t})
    e = SupplierEntry(tenant_id=auth.tenant_id, supplier_id=p.supplier_id, entry_date=body.entry_date or business_date(), kind="debit_note",
                      amount_paise=value + tax, reference=f"Return {p.invoice_no or ''}".strip()[:60],
                      note=("; ".join(f"{x['name']}" for x in rows) + f" — {body.reason}")[:200], created_by=auth.user_id)
    db.add(e)
    db.flush()
    audit.record(db, auth.tenant_id, auth.user_id, "create", "purchase_return", p.id, after={"debit_note": str(e.id), "total_paise": value + tax})
    return {"debit_note_id": str(e.id), "amount_paise": value, "tax_paise": tax, "total_paise": value + tax, "lines": rows}
