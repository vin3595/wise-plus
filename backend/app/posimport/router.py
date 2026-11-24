"""wise-plus: bring sales from another POS into EMB (the months before a restaurant switched).

Check first (nothing saved), then import. Bills already brought in are skipped, so the same file or an
overlapping one can be uploaded again. A whole upload can be taken back out."""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..core.contact import normalize_phone
from ..core.permissions import Perm
from ..deps import Auth, get_db, live_check, require
from ..models import Bill, Outlet
from ..routers.common import get_or_404
from ..services import audit
from .models import ImportedBill, ImportedLine, PosImport
from .parse import FIELDS, figures, parse

router = APIRouter(prefix="/pos-import", tags=["pos import"], dependencies=[Depends(live_check)])
MAX_BYTES = 25_000_000
IST = ZoneInfo("Asia/Kolkata")
SOURCES = ("Petpooja", "Posist", "DotPe", "Rista", "Torqus", "Zomato / Swiggy", "Excel", "Other")


def _read(file: UploadFile) -> bytes:
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File must be under 25 MB. Export fewer months at a time.")
    return data


def _mapping(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        m = json.loads(raw)
        return {str(k): str(v or "") for k, v in m.items()} if isinstance(m, dict) else None
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Column choices aren't readable") from None


def _plan(db: Session, outlet_id: uuid.UUID, data: bytes, name: str, mapping: dict | None):
    p = parse(data, name, mapping)
    out = {
        "fields": [{"key": k, "label": lab, "required": k in ("bill_no", "date")} for k, (lab, _) in FIELDS.items()],
        "headers": p.headers, "mapping": p.mapping, "item_wise": p.item_wise,
        "errors": [{"row": e.row, "message": e.message} for e in p.errors[:50]],
        "warnings": [], "ok": p.ok,
    }
    if not p.bills:
        return p, [], out
    figs = [(b, figures(b)) for b in p.bills]
    days = sorted({b.day for b, _ in figs})
    existing = set(db.execute(select(ImportedBill.business_date, ImportedBill.bill_no).where(
        ImportedBill.outlet_id == outlet_id, ImportedBill.business_date >= days[0],
        ImportedBill.business_date <= days[-1])).all())
    new = [(b, f) for b, f in figs if (b.day, b.bill_no[:40]) not in existing]
    paid = [(b, f) for b, f in new if f.status == "paid"]
    months: dict = defaultdict(lambda: {"bills": 0, "sales_paise": 0, "tax_paise": 0})
    modes: dict = defaultdict(int)
    for b, f in paid:
        m = months[b.day.strftime("%Y-%m")]
        m["bills"] += 1
        m["sales_paise"] += f.total
        m["tax_paise"] += f.cgst + f.sgst + f.igst
        for k, v in f.pays.items():
            modes[k] += v
    own_days = set(db.scalars(select(func.distinct(Bill.business_date)).where(
        Bill.outlet_id == outlet_id, Bill.business_date >= days[0], Bill.business_date <= days[-1])))
    warnings = []
    if own_days:
        warnings.append(f"EMB already has its own bills on {len(own_days)} of these days "
                        f"({min(own_days):%d %b} to {max(own_days):%d %b}). Those days would count twice; "
                        "remove those rows from the file or pick a file that ends the day before you started on EMB.")
    odd = [b.bill_no for b, f in paid if f.total <= 0]
    if odd:
        warnings.append(f"{len(odd)} bills have a total of ₹0 or less (e.g. {', '.join(odd[:3])}).")
    if p.item_wise:
        no_amount = sum(1 for b, _ in paid for ln in b.lines if ln.amount is None)
        if no_amount:
            warnings.append(f"{no_amount} item rows have no amount; their share of the bill was split by quantity.")
    if "payment" not in p.mapping:
        warnings.append("No payment type column: every bill will show as 'Other' in payment reports.")
    part = sum(1 for _, f in paid if "part" in f.pays)
    if part:
        warnings.append(f"{part} bills are 'Part Payment' with no breakup in the file; they show as 'Part payment' in payment reports.")
    other = sum(1 for _, f in paid if "other" in f.pays)
    if other and "payment" in p.mapping:
        warnings.append(f"{other} bills have a payment type we didn't recognise (shown as 'Other').")
    out.update({
        "warnings": warnings,
        "bills_in_file": len(p.bills), "already_imported": len(figs) - len(new),
        "bills": len(paid), "cancelled": len(new) - len(paid),
        "date_from": str(days[0]), "date_to": str(days[-1]),
        "sales_paise": sum(f.total for _, f in paid),
        "tax_paise": sum(f.cgst + f.sgst + f.igst for _, f in paid),
        "discount_paise": sum(f.discount for _, f in paid),
        "items": sum(len(f.lines) for _, f in paid),
        "months": [{"month": k, **v} for k, v in sorted(months.items())],
        "payments": dict(sorted(modes.items(), key=lambda kv: -kv[1])),
        "sample": [{"bill_no": b.bill_no, "date": str(b.day), "time": b.at.strftime("%H:%M") if b.at else None,
                    "total_paise": f.total, "tax_paise": f.cgst + f.sgst + f.igst, "discount_paise": f.discount,
                    "payments": f.pays, "status": f.status,
                    "items": [f"{ln['qty']} × {ln['item']}{'' if ln['variant'] == 'Regular' else ' ' + ln['variant']}" for ln in f.lines][:6]}
                   for b, f in figs[:5]],
    })
    return p, new, out


@router.post("/check")
def check(outlet_id: uuid.UUID = Form(...), file: UploadFile = File(...), mapping: str | None = Form(default=None),
          auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    """What the file holds and what would be imported. Nothing is saved."""
    get_or_404(db, Outlet, outlet_id, "Outlet")
    _, _, out = _plan(db, outlet_id, _read(file), file.filename or "file.xlsx", _mapping(mapping))
    return out


@router.post("")
def run(outlet_id: uuid.UUID = Form(...), source: str = Form(default="Other"), file: UploadFile = File(...),
        mapping: str | None = Form(default=None),
        auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    get_or_404(db, Outlet, outlet_id, "Outlet")
    name = (file.filename or "file.xlsx")[:200]
    p, new, out = _plan(db, outlet_id, _read(file), name, _mapping(mapping))
    if not p.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, out["errors"][0]["message"] if out["errors"] else "Nothing to import")
    if not new:
        raise HTTPException(status.HTTP_409_CONFLICT, "Every bill in this file is already imported")
    days = [b.day for b, _ in new]
    paid_total = sum(f.total for _, f in new if f.status == "paid")
    imp = PosImport(tenant_id=auth.tenant_id, outlet_id=outlet_id, source=(source or "Other")[:40], file_name=name,
                    date_from=min(days), date_to=max(days), bills=len(new), total_paise=paid_total,
                    item_wise=p.item_wise, created_by=auth.user_id)
    db.add(imp)
    db.flush()
    bills, lines = [], []
    for b, f in new:
        phone = b.values.get("customer_phone")
        try:
            phone = normalize_phone(phone) if phone else None
        except ValueError:
            phone = None
        at = b.at.replace(tzinfo=IST).astimezone(timezone.utc) if b.at else None
        bid = uuid.uuid4()
        bills.append(dict(id=bid, tenant_id=auth.tenant_id, import_id=imp.id, outlet_id=outlet_id, business_date=b.day,
                          billed_at=at, bill_no=b.bill_no[:40], status=f.status, order_type=f.order_type,
                          gross_paise=f.gross, discount_paise=f.discount, taxable_paise=f.taxable, cgst_paise=f.cgst,
                          sgst_paise=f.sgst, igst_paise=f.igst, round_off_paise=f.round_off, total_paise=f.total,
                          payments=f.pays, customer_name=(b.values.get("customer_name") or "")[:80] or None,
                          customer_phone=phone, customer_gstin=(b.values.get("customer_gstin") or "")[:15].upper() or None))
        for ln in f.lines:
            lines.append(dict(id=uuid.uuid4(), tenant_id=auth.tenant_id, bill_id=bid, item_name=ln["item"],
                              variant_name=ln["variant"], category=ln["category"], hsn_sac=ln["hsn"], qty=ln["qty"],
                              gross_paise=ln["gross"], discount_paise=ln["discount"], taxable_paise=ln["taxable"],
                              tax_paise=ln["tax"], rate_bp=ln["rate_bp"]))
    for i in range(0, len(bills), 2000):
        db.execute(ImportedBill.__table__.insert(), bills[i:i + 2000])
    for i in range(0, len(lines), 5000):
        db.execute(ImportedLine.__table__.insert(), lines[i:i + 5000])
    audit.record(db, auth.tenant_id, auth.user_id, "import", "pos_sales", imp.id,
                 after={"file": name, "source": imp.source, "bills": len(new), "total_paise": paid_total,
                        "from": str(imp.date_from), "to": str(imp.date_to)})
    return {**out, "import_id": str(imp.id), "imported": len(new)}


@router.get("")
def history(outlet_id: uuid.UUID | None = None, auth: Auth = Depends(require(Perm.tenant_settings)),
            db: Session = Depends(get_db)) -> list[dict]:
    stmt = select(PosImport, Outlet.name).join(Outlet, Outlet.id == PosImport.outlet_id).order_by(PosImport.created_at.desc())
    if outlet_id:
        stmt = stmt.where(PosImport.outlet_id == outlet_id)
    return [{"id": str(i.id), "outlet_id": str(i.outlet_id), "outlet": o, "source": i.source, "file_name": i.file_name,
             "date_from": str(i.date_from), "date_to": str(i.date_to), "bills": i.bills, "total_paise": i.total_paise,
             "item_wise": i.item_wise, "created_at": i.created_at.isoformat()} for i, o in db.execute(stmt)]


@router.delete("/{import_id}")
def undo(import_id: uuid.UUID, auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    imp = get_or_404(db, PosImport, import_id, "Import")
    n = imp.bills
    db.execute(delete(ImportedBill).where(ImportedBill.import_id == imp.id))   # lines go with their bills
    audit.record(db, auth.tenant_id, auth.user_id, "delete", "pos_sales", imp.id,
                 before={"file": imp.file_name, "bills": n, "total_paise": imp.total_paise})
    db.delete(imp)
    return {"removed": n}


@router.get("/sources")
def sources(auth: Auth = Depends(require(Perm.tenant_settings))) -> list[str]:
    _ = datetime
    return list(SOURCES)
