"""Money › Banks: accounts, statement uploads, and the lines to check."""
from __future__ import annotations

import os
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.permissions import Perm
from ..db import tenant_session
from ..deps import live_check, Auth, get_db, require
from ..models import Outlet, Supplier
from ..services import audit
from . import service as svc
from .models import KIND_LABEL, BankAccount, BankTxn
from .parse import BankParseError, read_statement

router = APIRouter(prefix="/banks", tags=["banks"])
MAX = 15 * 1024 * 1024


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    bank: str | None = Field(default=None, max_length=40)
    last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    outlet_ids: list[uuid.UUID] | None = None
    opening_paise: int = Field(default=0, ge=-10_000_000_000, le=10_000_000_000)
    opening_date: date | None = None
    tally_ledger: str | None = Field(default=None, max_length=80)
    is_active: bool = True


@router.get("")
def accounts(db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> dict:
    rows = db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.name)).all()
    out = [svc.account_out(db, a) for a in rows]
    return {"accounts": out, "total_paise": sum(a["balance_paise"] or 0 for a in out), "kinds": KIND_LABEL}


def _apply(a: BankAccount, body: AccountIn, db: Session) -> None:
    if body.outlet_ids:
        for o in body.outlet_ids:
            if db.get(Outlet, o) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")
    a.name, a.bank, a.last4 = body.name.strip(), (body.bank or "").strip() or None, body.last4
    a.outlet_ids = [str(x) for x in body.outlet_ids] if body.outlet_ids else None
    a.opening_paise, a.opening_date = body.opening_paise, body.opening_date
    a.tally_ledger, a.is_active = (body.tally_ledger or "").strip() or None, body.is_active


@router.post("", status_code=201)
def add_account(body: AccountIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> dict:
    if db.scalar(select(BankAccount).where(BankAccount.name == body.name.strip())):
        raise HTTPException(status.HTTP_409_CONFLICT, "There's already an account with that name")
    a = BankAccount(tenant_id=auth.tenant_id, name=body.name.strip())
    _apply(a, body, db)
    db.add(a)
    db.flush()
    audit.record(db, auth.tenant_id, auth.user_id, "create", "bank_account", a.id, after={"name": a.name})
    return svc.account_out(db, a)


@router.put("/{account_id}")
def edit_account(account_id: uuid.UUID, body: AccountIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> dict:
    a = db.get(BankAccount, account_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    clash = db.scalar(select(BankAccount).where(BankAccount.name == body.name.strip(), BankAccount.id != a.id))
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT, "There's already an account with that name")
    _apply(a, body, db)
    audit.record(db, auth.tenant_id, auth.user_id, "update", "bank_account", a.id, after={"name": a.name})
    return svc.account_out(db, a)


def _store(tenant_id, data: bytes, name: str) -> str:
    folder = os.path.join(get_settings().upload_dir, "statements", str(tenant_id))
    os.makedirs(folder, exist_ok=True)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ext = ext if ext in {"pdf", "xlsx", "xls", "csv", "txt", "jpg", "jpeg", "png", "webp"} else "bin"
    path = os.path.join(folder, f"{uuid.uuid4()}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def run_import(tenant_id, user_id, data: bytes, st: dict, name: str, account_id=None) -> dict:
    path = _store(tenant_id, data, name)
    with tenant_session(tenant_id) as db:
        return svc.import_statement(db, tenant_id, user_id, st, account_id, name, path)


@router.post("/statements", dependencies=[Depends(live_check)])
async def upload(file: UploadFile = File(...), account_id: uuid.UUID | None = Form(None),
                 auth: Auth = Depends(require(Perm.banks))) -> dict:
    data = await file.read(MAX + 1)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The file is empty")
    if len(data) > MAX:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "That file is over 15 MB")
    name = file.filename or "statement"
    try:
        st = await read_statement(data, (file.content_type or "").lower(), name)
    except BankParseError as e:
        raise HTTPException(e.status, str(e)) from e
    return await run_in_threadpool(run_import, auth.tenant_id, auth.user_id, data, st, name, account_id)


@router.get("/txns")
def txns(account_id: uuid.UUID | None = None, show: str = "check", date_from: date | None = None, date_to: date | None = None,
         limit: int = 300, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> list[dict]:
    q = select(BankTxn)
    if account_id:
        q = q.where(BankTxn.bank_account_id == account_id)
    if show == "check":
        q = q.where(BankTxn.status.in_(("suggested", "unmatched")))
    elif show == "ignored":
        q = q.where(BankTxn.status == "ignored")
    if date_from:
        q = q.where(BankTxn.txn_date >= date_from)
    if date_to:
        q = q.where(BankTxn.txn_date <= date_to)
    rows = db.scalars(q.order_by(BankTxn.txn_date.desc(), BankTxn.created_at.desc()).limit(min(max(limit, 1), 2000))).all()
    return [svc.txn_out(t) for t in rows]


class KindIn(BaseModel):
    kind: str = Field(max_length=15)
    category: str | None = Field(default=None, max_length=40)
    supplier_id: uuid.UUID | None = None
    other_account_id: uuid.UUID | None = None
    remember: bool = True


@router.post("/txns/{txn_id}")
def set_kind(txn_id: uuid.UUID, body: KindIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> dict:
    t = db.get(BankTxn, txn_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Line not found")
    if body.kind not in KIND_LABEL:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose what this money was")
    if body.supplier_id and db.get(Supplier, body.supplier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found")
    if body.kind == "expense" and not (body.category or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Say what the expense was for, e.g. Electricity")
    try:
        more = svc.set_kind(db, t, body.kind, auth.user_id, svc.clean_category(body.category), body.supplier_id, body.other_account_id, body.remember)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    return {"txn": svc.txn_out(t), "also_sorted": more}


@router.post("/txns/{txn_id}/ignore")
def ignore(txn_id: uuid.UUID, undo: bool = False, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> dict:
    t = db.get(BankTxn, txn_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Line not found")
    t.status, t.matched_by = ("unmatched", None) if undo else ("ignored", "person")
    return svc.txn_out(t)


@router.post("/rematch")
def rematch(db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> dict:
    """Try again on the open lines (after adding purchases, payments or the outlets an account takes money for)."""
    from .match import match_account
    n0 = db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.status.in_(("suggested", "unmatched"))))
    for a in db.scalars(select(BankAccount)).all():
        open_ = db.scalars(select(BankTxn).where(BankTxn.bank_account_id == a.id, BankTxn.status.in_(("suggested", "unmatched")))).all()
        for t in open_:
            if t.matched_by != "person":
                t.status, t.kind, t.note = "unmatched", None, None
        match_account(db, a, list(open_), auth.user_id)
    n1 = db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.status.in_(("suggested", "unmatched"))))
    return {"to_check_before": n0, "to_check_now": n1}


@router.get("/categories")
def categories(db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.banks))) -> list[str]:
    from ..models import CashMovement
    used = set(db.scalars(select(BankTxn.category).where(BankTxn.category.is_not(None)).distinct()).all())
    used |= set(db.scalars(select(CashMovement.category).where(CashMovement.category.is_not(None)).distinct()).all())
    return sorted(used | {"Electricity", "Rent", "Repairs", "Gas", "Packaging", "Marketing", "Bank charges", "Phone & internet", "Transport"})
