"""Supplier dues: balances, a statement per supplier, and payments / opening balances / debit notes."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.permissions import Perm
from ..deps import Auth, get_db, require
from ..models import Supplier
from ..services import audit
from .balances import balances, statement
from .models import SupplierEntry

router = APIRouter(prefix="/ledger", tags=["supplier ledger"])


class EntryIn(BaseModel):
    supplier_id: uuid.UUID
    kind: str = Field(pattern=r"^(opening|payment|debit_note)$")
    entry_date: date
    amount_paise: int = Field(ge=-10_000_000_000, le=10_000_000_000)
    mode: str | None = Field(default=None, pattern=r"^(cash|upi|bank|cheque|card|other)$")
    reference: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=200)


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
                      reference=(body.reference or "").strip() or None, note=(body.note or "").strip() or None, created_by=auth.user_id)
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
