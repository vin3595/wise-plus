"""Supplier dues: balances, a statement per supplier, and payments / opening balances / debit notes."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.permissions import Perm
from ..deps import live_check, Auth, get_db, require
from ..models import Supplier
from ..services import audit
from .balances import balances, statement
from .models import SupplierEntry

router = APIRouter(prefix="/ledger", tags=["supplier ledger"])


class EntryIn(BaseModel):
    supplier_id: uuid.UUID
    kind: str = Field(pattern=r"^(opening|payment|debit_note|credit_note)$")
    entry_date: date
    amount_paise: int = Field(ge=-10_000_000_000, le=10_000_000_000)
    mode: str | None = Field(default=None, pattern=r"^(cash|upi|bank|cheque|card|other)$")
    reference: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=200)
    bank_account_id: uuid.UUID | None = None


@router.get("/suppliers")
def supplier_balances(all: bool = False, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    rows = balances(db, include_zero=all)
    return {"total_paise": sum(r.balance_paise for r in rows if r.balance_paise > 0),
            "suppliers": [r.__dict__ for r in rows]}


@router.get("/suppliers/{supplier_id}")
def supplier_statement(supplier_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    if db.get(Supplier, supplier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found")
    return statement(db, supplier_id)


@router.post("/entries", status_code=201)
def add_entry(body: EntryIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    if db.get(Supplier, body.supplier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found")
    if body.kind != "opening" and body.amount_paise <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter an amount above zero")
    if body.kind == "payment" and not body.mode:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Say how it was paid")
    e = SupplierEntry(tenant_id=auth.tenant_id, supplier_id=body.supplier_id, entry_date=body.entry_date, kind=body.kind,
                      amount_paise=body.amount_paise, mode=body.mode if body.kind == "payment" else None,
                      reference=(body.reference or "").strip() or None, note=(body.note or "").strip() or None, created_by=auth.user_id,
                      bank_account_id=body.bank_account_id if body.kind == "payment" else None)
    db.add(e)
    db.flush()
    audit.record(db, auth.tenant_id, auth.user_id, "create", "supplier_entry", e.id,
                 after={"kind": e.kind, "amount_paise": e.amount_paise, "supplier_id": str(e.supplier_id)})
    return {"id": str(e.id), "balance_paise": statement(db, body.supplier_id)["balance_paise"]}


@router.post("/entries/{entry_id}/cancel")
def cancel_entry(entry_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    e = db.get(SupplierEntry, entry_id)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    if e.status == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "Already cancelled")
    e.status = "cancelled"
    audit.record(db, auth.tenant_id, auth.user_id, "cancel", "supplier_entry", e.id, before={"amount_paise": e.amount_paise})
    return {"ok": True}


# ------------------------------------------------------------------ check a supplier's statement

def _start(auth: Auth) -> None:
    from ..billing.service import locked, scan_or_402
    from ..db import tenant_session
    from ..models import Tenant
    with tenant_session(auth.tenant_id) as db:
        tenant = db.get(Tenant, auth.tenant_id)
        if tenant is None or tenant.status in ("suspended", "read_only"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account can't do that right now")
        if locked(db, tenant):
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Your free trial has ended. Choose a plan in Settings › Billing.")
        scan_or_402(db, tenant)


def _finish(auth: Auth, st: dict, supplier_id: uuid.UUID | None, new_supplier: bool) -> dict:
    from ..billing.service import count_scan
    from ..db import tenant_session
    from ..models import Tenant
    from . import statement as stm
    with tenant_session(auth.tenant_id) as db:
        count_scan(db, db.get(Tenant, auth.tenant_id))
        if supplier_id and db.get(Supplier, supplier_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found")
        return stm.check(db, auth.tenant_id, st, supplier_id, new_supplier)


@router.post("/statement", dependencies=[Depends(live_check)])
async def check_statement(file: UploadFile = File(...), supplier_id: uuid.UUID | None = Form(None),
                          new_supplier: bool = Form(False), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    """Read a supplier's ledger and show which of their bills are in the POS and which payments aren't in our dues ledger."""
    from fastapi.concurrency import run_in_threadpool
    from .statement import StatementError, read_statement
    await run_in_threadpool(_start, auth)
    data = await file.read(15 * 1024 * 1024 + 1)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The file is empty")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "That file is over 15 MB")
    try:
        st = await read_statement(data, (file.content_type or "").lower(), file.filename or "statement")
    except StatementError as e:
        raise HTTPException(e.status, str(e)) from e
    if not st["rows"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No bills or payments found on that statement")
    return await run_in_threadpool(_finish, auth, st, supplier_id, new_supplier)


class StmtPayment(BaseModel):
    date: date
    amount_paise: int = Field(gt=0, le=10_000_000_000)
    kind: str = Field(default="payment", pattern=r"^(payment|debit_note|credit_note)$")
    mode: str | None = Field(default="bank", pattern=r"^(cash|upi|bank|cheque|card|other)$")
    ref: str | None = Field(default=None, max_length=60)
    bank: str | None = Field(default=None, max_length=120)
    particulars: str | None = Field(default=None, max_length=120)


class StmtOpening(BaseModel):
    date: date
    amount_paise: int = Field(ge=-10_000_000_000, le=10_000_000_000)


class StmtApply(BaseModel):
    supplier_id: uuid.UUID
    payments: list[StmtPayment] = Field(default_factory=list, max_length=500)
    opening: StmtOpening | None = None


@router.post("/statement/apply")
def apply_statement(body: StmtApply, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    from .statement import apply
    sup = db.get(Supplier, body.supplier_id)
    if sup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found")
    ids = apply(db, auth.tenant_id, auth.user_id, sup, [p.model_dump() for p in body.payments],
                body.opening.model_dump() if body.opening else None)
    audit.record(db, auth.tenant_id, auth.user_id, "create", "supplier_statement", sup.id, after={"entries": len(ids)})
    return {"added": len(ids), "balance_paise": statement(db, body.supplier_id)["balance_paise"]}
