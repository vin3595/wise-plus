"""Scan a supplier's bill (photo or PDF) into a draft purchase. Ease My Bill's reader, Wise's stock.

The only link to the rest of Wise: the draft is shaped like POST /inventory/purchases, and the person saves
it there after checking it. Remove this folder and its one line in main.py and Wise is as it was.

The AI call takes several seconds, so it runs with no database connection held open."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ..core.permissions import Perm
from ..db import tenant_session
from ..deps import Auth, ensure_outlet_access, require
from ..models import Outlet, Purchase, StockItem, Supplier, Tenant, User
from .match import build_draft

router = APIRouter(prefix="/inventory/purchases/scan", tags=["bill scan"])

MAX_BYTES = 15 * 1024 * 1024
ACCEPT = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}


def _reader():
    """Imported on first use so Wise still starts on a server without the AI packages."""
    try:
        from . import extraction
    except ImportError as e:  # google-genai / pypdfium2 / pillow missing
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Bill scanning isn't installed on this server ({e.name})") from e
    return extraction


def enabled() -> bool:
    try:
        return bool(_reader().EMERGENT_LLM_KEY)
    except HTTPException:
        return False


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


def _match(auth: Auth, outlet_id: uuid.UUID, bill: dict) -> dict:
    with tenant_session(auth.tenant_id) as db:
        items = [{"id": str(i.id), "name": i.name, "unit": i.unit, "purchase_units": i.purchase_units or []}
                 for i in db.scalars(select(StockItem).where(StockItem.is_active.is_(True), StockItem.kind == "raw"))]
        sups = [{"id": str(s.id), "name": s.name, "gstin": s.gstin}
                for s in db.scalars(select(Supplier).where(Supplier.is_active.is_(True)))]
        draft = build_draft(bill, items, sups, {})
        sup_id = draft["supplier"]["id"] if draft["supplier"] else None
        if sup_id:
            # What this supplier's bill lines were saved as before: learn from the person's own corrections.
            remembered: dict[str, str] = {}
            past = db.scalars(select(Purchase).where(Purchase.supplier_id == uuid.UUID(sup_id), Purchase.status == "posted")
                              .order_by(Purchase.created_at.desc()).limit(40))
            for p in past:
                for ln in p.lines or []:
                    if ln.get("bill_name"):
                        remembered.setdefault(ln["bill_name"].strip().lower(), ln["stock_item_id"])
            if remembered:
                draft = build_draft(bill, items, sups, remembered)
            if draft["invoice_no"]:
                dup = db.scalar(select(Purchase).where(Purchase.supplier_id == uuid.UUID(sup_id), Purchase.invoice_no == draft["invoice_no"],
                                                       Purchase.status == "posted").limit(1))
                if dup is not None:
                    draft["duplicate"] = {"id": str(dup.id), "invoice_date": str(dup.invoice_date), "total_paise": dup.total_paise}
        return draft


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
    ex = _reader()
    if not ex.EMERGENT_LLM_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Bill scanning isn't switched on: the server needs WISE_GEMINI_API_KEY")
    try:
        first = await run_in_threadpool(ex.get_first_page_png, data, ct, name)
    except Exception as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Couldn't open that file. Try a clearer photo or the original PDF.") from e
    kind = await ex.classify_document(first)
    if not kind.get("accepted") and kind.get("confidence", 0) >= 80:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"This doesn't look like a supplier's bill ({kind.get('reason') or kind.get('type', '').lower()}).")
    try:
        bill, thumb = await ex.extract_bill_from_file(data, ct, name)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The bill reader didn't answer. Try again in a minute.") from e
    if bill.get("_extraction_failed") or not (bill.get("items") or bill.get("total")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Couldn't read any items on that bill. Try a straighter, brighter photo.")
    draft = await run_in_threadpool(_match, auth, outlet_id, bill)
    draft["thumbnail"] = thumb
    draft["low_confidence"] = bool(kind.get("should_warn"))
    return draft
