"""Scan a supplier's bill (photo or PDF) into a draft purchase. Ease My Bill's reader, Wise's stock.

The only link to the rest of Wise: the draft is shaped like POST /inventory/purchases, and the person saves
it there after checking it. Remove this folder and its line in main.py and Wise is as it was.
Bills sent on WhatsApp wait here (scanned_bills) until saved there or in the app.

The AI call takes several seconds, so it runs with no database connection held open."""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..core.permissions import Perm
from ..db import tenant_session
from ..deps import Auth, ensure_outlet_access, get_db, require
from ..models import Outlet, Tenant, User
from .models import ScannedBill
from .service import ScanError, classify, enabled, read_bill, tenant_draft

router = APIRouter(prefix="/inventory/purchases/scan", tags=["bill scan"])

MAX_BYTES = 15 * 1024 * 1024
ACCEPT = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}


@router.get("/status")
def scan_status(auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    return {"enabled": enabled()}


def _check(auth: Auth, outlet_id: uuid.UUID) -> None:
    with tenant_session(auth.tenant_id) as db:
        user = db.get(User, auth.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
        tenant = db.get(Tenant, auth.tenant_id)
        if tenant is None or tenant.status in ("suspended", "read_only"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This restaurant's account can't add purchases right now")
        from ..billing.service import locked, scan_or_402   # wise-plus: trial over / scans used up
        if locked(db, tenant):
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Your free trial has ended. Choose a plan in Settings › Billing.")
        scan_or_402(db, tenant)
        if db.get(Outlet, outlet_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")
        ensure_outlet_access(db, auth, outlet_id)


def _statement(auth: Auth, st: dict) -> dict:
    from ..ledger.statement import check
    with tenant_session(auth.tenant_id) as db:
        return check(db, auth.tenant_id, st, new_supplier=False)


def _draft(auth: Auth, bill: dict) -> dict:
    with tenant_session(auth.tenant_id) as db:
        return tenant_draft(db, bill)


def _count(auth: Auth) -> None:
    from ..billing.service import count_scan
    with tenant_session(auth.tenant_id) as db:
        count_scan(db, db.get(Tenant, auth.tenant_id))


@router.post("")
async def scan_bill(outlet_id: uuid.UUID = Form(...), file: UploadFile = File(...),
                    auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    await run_in_threadpool(_check, auth, outlet_id)
    ct = (file.content_type or "").lower()
    name = file.filename or "bill"
    if ct not in ACCEPT and not name.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic")):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Send a photo (JPG, PNG, WebP) or a PDF of the bill")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "That file is over 15 MB")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The file is empty")
    from ..ledger import statement as stm
    try:
        kind = ({"type": "SUPPLIER_STATEMENT"} if await run_in_threadpool(stm.looks_like_statement, data, ct, name)
                else await classify(data, ct, name))
        if kind.get("type") == "SUPPLIER_STATEMENT":
            # A supplier's ledger, not a bill: check it against the POS instead (Money › Supplier dues shows it).
            try:
                st = await stm.read_statement(data, ct, name)
            except stm.StatementError as e:
                raise HTTPException(e.status, str(e)) from e
            if not st["rows"]:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No bills or payments found on that statement")
            await run_in_threadpool(_count, auth)
            return {"kind": "statement"} | await run_in_threadpool(_statement, auth, st)
        bill, thumb, kind = await read_bill(data, ct, name, kind=kind)
    except ScanError as e:
        raise HTTPException(e.status, str(e)) from e
    await run_in_threadpool(_count, auth)
    draft = await run_in_threadpool(_draft, auth, bill)
    draft["thumbnail"] = thumb
    draft["low_confidence"] = bool(kind.get("should_warn"))
    # Bill keeper: the original photo/PDF is kept and linked to the purchase once it's saved.
    draft["scan_id"] = await run_in_threadpool(_keep, auth, outlet_id, data, ct, name, draft, bill)
    return draft


EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "application/pdf": "pdf"}


def keep_file(tenant_id, sid: uuid.UUID, data: bytes, ct: str, name: str = "") -> str:
    folder = os.path.join(get_settings().upload_dir, "scans", str(tenant_id))
    os.makedirs(folder, exist_ok=True)
    # Only known extensions: the uploaded file name is never trusted (it could carry "../" and escape the folder).
    guess = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    ext = EXT.get(ct) or (guess if guess in set(EXT.values()) | {"jpeg"} else "bin")
    path = os.path.join(folder, f"{sid}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _keep(auth: Auth, outlet_id: uuid.UUID, data: bytes, ct: str, name: str, draft: dict, bill: dict) -> str:
    sid = uuid.uuid4()
    path = keep_file(auth.tenant_id, sid, data, ct, name)
    with tenant_session(auth.tenant_id) as db:
        db.add(ScannedBill(id=sid, tenant_id=auth.tenant_id, outlet_id=outlet_id, source="app", user_id=auth.user_id,
                           file_path=path, draft={**draft, "_bill": bill}))
    return str(sid)


# ---------------------------------------------------------------- bills sent on WhatsApp, waiting for a check

def _pending_out(b: ScannedBill, full: bool) -> dict:
    d = b.draft or {}
    out = {"id": str(b.id), "outlet_id": str(b.outlet_id), "source": b.source, "created_at": b.created_at.isoformat(),
           "vendor": (d.get("supplier") or {}).get("name") or d.get("vendor"), "invoice_no": d.get("invoice_no"),
           "total_paise": d.get("total_paise") or 0, "lines": len(d.get("lines") or []),
           "unmatched": sum(1 for ln in d.get("lines") or [] if not ln.get("stock_item_id"))}
    if full:
        out["draft"] = d
    return out


@router.get("/pending")
def pending(db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> list[dict]:
    rows = db.scalars(select(ScannedBill).where(ScannedBill.status == "pending", ScannedBill.source != "app")
                      .order_by(ScannedBill.created_at.desc()).limit(50))
    return [_pending_out(b, False) for b in rows]


@router.get("/pending/{bill_id}")
def pending_one(bill_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    b = db.get(ScannedBill, bill_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if b.status == "pending":
        # Suppliers or stock items may have been added since it was read: match again.
        fresh = tenant_draft(db, {**(b.draft.get("_bill") or {})}) if b.draft.get("_bill") else None
        if fresh:
            b.draft = {**fresh, "thumbnail": b.draft.get("thumbnail"), "_bill": b.draft["_bill"]}
    return _pending_out(b, True)


class DoneIn(BaseModel):
    purchase_id: uuid.UUID | None = None      # saved through POST /inventory/purchases
    discard: bool = False


@router.post("/pending/{bill_id}/done")
def pending_done(bill_id: uuid.UUID, body: DoneIn, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    b = db.get(ScannedBill, bill_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    b.status = "discarded" if body.discard else "saved"
    b.purchase_id = body.purchase_id
    return {"ok": True}


# ---------------------------------------------------------------- bill keeper: every scanned bill, with its original

@router.get("/kept")
def kept(q: str | None = None, month: str | None = None, db: Session = Depends(get_db),
         auth: Auth = Depends(require(Perm.inventory_manage))) -> list[dict]:
    """Every bill read from a photo (app or WhatsApp), newest first; q matches supplier or invoice number,
    month is YYYY-MM (bill date, or the day it was scanned when the bill has no date)."""
    rows = db.scalars(select(ScannedBill).where(ScannedBill.status != "discarded").order_by(ScannedBill.created_at.desc()).limit(1000))
    out = []
    needle = (q or "").strip().lower()
    for b in rows:
        d = b.draft or {}
        vendor = (d.get("supplier") or {}).get("name") or d.get("vendor") or ""
        day = d.get("invoice_date") or b.created_at.date().isoformat()
        if needle and needle not in vendor.lower() and needle not in (d.get("invoice_no") or "").lower():
            continue
        if month and not str(day).startswith(month):
            continue
        out.append({"id": str(b.id), "source": b.source, "status": b.status, "scanned_at": b.created_at.isoformat(),
                    "vendor": vendor, "invoice_no": d.get("invoice_no"), "invoice_date": d.get("invoice_date"),
                    "total_paise": d.get("total_paise") or 0, "purchase_id": str(b.purchase_id) if b.purchase_id else None,
                    "thumbnail": d.get("thumbnail"), "has_file": bool(b.file_path and os.path.exists(b.file_path)),
                    "is_pdf": bool(b.file_path and b.file_path.endswith(".pdf"))})
    return out[:300]


@router.get("/kept/{bill_id}/file")
def kept_file(bill_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))):
    from fastapi.responses import FileResponse
    b = db.get(ScannedBill, bill_id)
    if b is None or not b.file_path or not os.path.exists(b.file_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The original isn't kept for this bill")
    ext = b.file_path.rsplit(".", 1)[-1]
    media = {v: k for k, v in EXT.items()}.get(ext, "application/octet-stream")
    d = b.draft or {}
    name = "-".join(x for x in [((d.get("supplier") or {}).get("name") or d.get("vendor") or "bill").replace("/", "-"),
                                (d.get("invoice_no") or "").replace("/", "-")] if x)
    return FileResponse(b.file_path, media_type=media, filename=f"{name}.{ext}")


@router.get("/for-purchase/{purchase_id}")
def for_purchase(purchase_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.inventory_manage))) -> dict:
    """The kept bill behind a purchase, if it came from a photo."""
    b = db.scalar(select(ScannedBill).where(ScannedBill.purchase_id == purchase_id))
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No photo kept for this purchase")
    return {"id": str(b.id), "has_file": bool(b.file_path and os.path.exists(b.file_path))}
