"""Supplier balances and statements, shared by the Dues screen, the assistant and WhatsApp."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CashMovement, Purchase, Supplier
from .models import SupplierEntry


@dataclass
class Balance:
    supplier_id: str
    name: str
    opening_paise: int
    bills_paise: int
    paid_paise: int
    balance_paise: int
    last_bill_date: date | None
    last_payment_date: date | None


def _cash_to(db: Session) -> dict[str, tuple[int, date | None]]:
    """Cash paid out of a drawer with purpose 'supplier', by the name the cashier typed (lower case)."""
    rows = db.execute(select(func.lower(func.trim(CashMovement.paid_to)), func.sum(CashMovement.amount_paise),
                             func.max(func.date(CashMovement.created_at)))
                      .where(CashMovement.kind == "out", CashMovement.purpose == "supplier", CashMovement.paid_to.is_not(None))
                      .group_by(func.lower(func.trim(CashMovement.paid_to)))).all()
    return {k: (int(v or 0), d) for k, v, d in rows if k}


def balances(db: Session, include_zero: bool = False) -> list[Balance]:
    sups = db.scalars(select(Supplier).where(Supplier.is_active.is_(True))).all()
    bills = {sid: (int(t or 0), d) for sid, t, d in db.execute(
        select(Purchase.supplier_id, func.sum(Purchase.total_paise), func.max(Purchase.invoice_date))
        .where(Purchase.status == "posted", Purchase.supplier_id.is_not(None)).group_by(Purchase.supplier_id)).all()}
    ent: dict = {}
    for sid, kind, total, last in db.execute(
            select(SupplierEntry.supplier_id, SupplierEntry.kind, func.sum(SupplierEntry.amount_paise), func.max(SupplierEntry.entry_date))
            .where(SupplierEntry.status == "posted").group_by(SupplierEntry.supplier_id, SupplierEntry.kind)).all():
        ent[(sid, kind)] = (int(total or 0), last)
    cash = _cash_to(db)
    out = []
    for s in sups:
        opening = ent.get((s.id, "opening"), (0, None))[0]
        billed, last_bill = bills.get(s.id, (0, None))
        pay, last_pay = ent.get((s.id, "payment"), (0, None))
        dn = ent.get((s.id, "debit_note"), (0, None))[0]
        cash_paid, cash_last = cash.get(s.name.strip().lower(), (0, None))
        paid = pay + dn + cash_paid
        last_paid = max([d for d in (last_pay, cash_last) if d], default=None)
        b = Balance(str(s.id), s.name, opening, billed, paid, opening + billed - paid, last_bill, last_paid)
        if include_zero or b.balance_paise or b.bills_paise or b.opening_paise:
            out.append(b)
    out.sort(key=lambda b: -b.balance_paise)
    return out


def statement(db: Session, supplier_id: uuid.UUID, limit: int = 200) -> dict:
    """Every bill, payment and note for one supplier, oldest first, with a running balance."""
    s = db.get(Supplier, supplier_id)
    rows = []
    for p in db.scalars(select(Purchase).where(Purchase.supplier_id == supplier_id, Purchase.status == "posted")):
        rows.append({"date": p.invoice_date, "kind": "bill", "ref": p.invoice_no, "debit_paise": p.total_paise, "credit_paise": 0,
                     "id": str(p.id), "note": f"{len(p.lines or [])} item(s)"})
    for e in db.scalars(select(SupplierEntry).where(SupplierEntry.supplier_id == supplier_id, SupplierEntry.status == "posted")):
        if e.kind == "opening":
            rows.append({"date": e.entry_date, "kind": "opening", "ref": None, "debit_paise": max(e.amount_paise, 0),
                         "credit_paise": max(-e.amount_paise, 0), "id": str(e.id), "note": e.note})
        else:
            rows.append({"date": e.entry_date, "kind": e.kind, "ref": e.reference, "debit_paise": 0, "credit_paise": e.amount_paise,
                         "id": str(e.id), "mode": e.mode, "note": e.note})
    if s is not None:
        for m in db.scalars(select(CashMovement).where(CashMovement.kind == "out", CashMovement.purpose == "supplier",
                                                       func.lower(func.trim(CashMovement.paid_to)) == s.name.strip().lower())):
            rows.append({"date": m.created_at.date(), "kind": "cash_drawer", "ref": None, "debit_paise": 0, "credit_paise": m.amount_paise,
                         "id": str(m.id), "mode": "cash", "note": m.note or m.reason})
    order = {"opening": 0, "bill": 1, "payment": 2, "debit_note": 2, "cash_drawer": 2}
    rows.sort(key=lambda r: (r["date"], order[r["kind"]]))
    bal = 0
    for r in rows:
        bal += r["debit_paise"] - r["credit_paise"]
        r["balance_paise"] = bal
    return {"supplier": {"id": str(supplier_id), "name": s.name if s else None, "phone": s.phone if s else None},
            "balance_paise": bal, "entries": rows[-limit:]}
