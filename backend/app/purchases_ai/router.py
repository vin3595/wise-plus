"""Scan a supplier's bill (photo or PDF) into a draft purchase. Ease My Bill's reader, Wise's stock.

The only link to the rest of Wise: the draft is shaped like POST /inventory/purchases, and the person saves
it there after checking it. Remove this folder and its line in main.py and Wise is as it was.
Bills sent on WhatsApp wait here (scanned_bills) until saved there or in the app.

The AI call takes several seconds, so it runs with no database connection held open."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..core.permissions import Perm
from ..db import tenant_session
from ..deps import Auth, ensure_outlet_access, get_db, require
from ..models import Outlet, Tenant, User
from .models import ScannedBill
from .service import ScanError, enabled, read_bill, tenant_draft

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
        if db.get(Outlet, outlet_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")
        ensure_outlet_access(db, auth, outlet_id)


def _draft(auth: Auth, bill: dict) -> dict:
    with tenant_session(auth.tenant_id) as db:
        return tenant_draft(db, bill)


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
    try:
        bill, thumb, kind = await read_bill(data, ct, name)
    except ScanError as e:
        raise HTTPException(e.status, str(e)) from e
    draft = await run_in_threadpool(_draft, auth, bill)
    draft["thumbnail"] = thumb
    draft["low_confidence"] = bool(kind.get("should_warn"))
    return draft


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
    rows = db.scalars(select(ScannedBill).where(ScannedBill.status == "pending").order_by(ScannedBill.created_at.desc()).limit(50))
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
