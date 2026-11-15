"""The scan steps, shared by the app's Scan bill button and bills sent on WhatsApp."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Purchase, StockItem, Supplier
from .match import build_draft


class ScanError(Exception):
    """A message for the person: why this file couldn't be read."""
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def reader():
    """Imported on first use so Wise still starts on a server without the AI packages."""
    try:
        from . import extraction
    except ImportError as e:  # google-genai / pypdfium2 / pillow missing
        raise ScanError(f"Bill scanning isn't installed on this server ({e.name})", 503) from e
    return extraction


def enabled() -> bool:
    try:
        return bool(reader().EMERGENT_LLM_KEY)
    except ScanError:
        return False


async def classify(data: bytes, content_type: str, name: str) -> dict:
    """What kind of document this is (INVOICE, RECEIPT, HANDWRITTEN_BILL, LEDGER_PAGE = a daily sheet, …)."""
    from starlette.concurrency import run_in_threadpool
    ex = reader()
    try:
        first = await run_in_threadpool(ex.get_first_page_png, data, content_type, name)
    except Exception as e:
        raise ScanError("Couldn't open that file. Try a clearer photo or the original PDF.") from e
    return await ex.classify_document(first)


async def read_bill(data: bytes, content_type: str, name: str, kind: dict | None = None) -> tuple[dict, str | None, dict]:
    """(bill as read, thumbnail data URL, what kind of document it looked like). Raises ScanError.
    kind: a classification already made (WhatsApp classifies first to tell bills from daily sheets)."""
    from starlette.concurrency import run_in_threadpool
    ex = reader()
    if not ex.EMERGENT_LLM_KEY:
        raise ScanError("Bill scanning isn't switched on: the server needs WISE_GEMINI_API_KEY", 503)
    if kind is None:
        try:
            first = await run_in_threadpool(ex.get_first_page_png, data, content_type, name)
        except Exception as e:
            raise ScanError("Couldn't open that file. Try a clearer photo or the original PDF.") from e
        kind = await ex.classify_document(first)
    if not kind.get("accepted") and kind.get("confidence", 0) >= 80:
        raise ScanError(f"This doesn't look like a supplier's bill ({kind.get('reason') or kind.get('type', '').lower()}).")
    try:
        bill, thumb = await ex.extract_bill_from_file(data, content_type, name)
    except Exception as e:
        raise ScanError("The bill reader didn't answer. Try again in a minute.", 502) from e
    if bill.get("_extraction_failed") or not (bill.get("items") or bill.get("total")):
        raise ScanError("Couldn't read any items on that bill. Try a straighter, brighter photo.")
    return bill, thumb, kind


def tenant_draft(db: Session, bill: dict) -> dict:
    """Match a read bill against this restaurant's suppliers and stock items; learn from earlier bills."""
    items = [{"id": str(i.id), "name": i.name, "unit": i.unit, "purchase_units": i.purchase_units or []}
             for i in db.scalars(select(StockItem).where(StockItem.is_active.is_(True), StockItem.kind == "raw"))]
    sups = [{"id": str(s.id), "name": s.name, "gstin": s.gstin} for s in db.scalars(select(Supplier).where(Supplier.is_active.is_(True)))]
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


def ready_to_save(draft: dict) -> list[str]:
    """Why this draft can't be saved without a person looking at it ([] = it can)."""
    why = []
    if draft.get("duplicate"):
        why.append("it looks entered already")
    if not draft.get("supplier"):
        why.append(f"the supplier “{draft.get('vendor') or '?'}” isn't in Wise yet")
    unmatched = [ln["bill_name"] for ln in draft.get("lines", []) if not ln.get("stock_item_id")]
    if unmatched:
        why.append(f"{len(unmatched)} line(s) need a stock item")
    if any(not ln.get("qty_m") for ln in draft.get("lines", []) if ln.get("stock_item_id")):
        why.append("a quantity is missing")
    if not draft.get("lines"):
        why.append("no items were read")
    return why
