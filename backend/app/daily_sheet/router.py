"""Money > Daily sheets: upload a photo, check and correct it, save; history by date."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.permissions import Perm
from ..db import tenant_session
from ..deps import live_check, Auth, ensure_outlet_access, get_db, require
from ..models import Outlet
from ..purchases_ai.service import ScanError
from .models import DailySheet
from .service import PAYOUTS, cash_counted, enabled, keep_photo, parse_date, read_sheet, rows, save, summary

router = APIRouter(prefix="/daily-sheets", tags=["daily sheets"])


def _out(s: DailySheet, full: bool = False) -> dict:
    d = s.data or {}
    exp = sum(a for n, a in rows(d.get("expenses")) if n.upper() not in PAYOUTS)
    o = {"id": str(s.id), "outlet_id": str(s.outlet_id), "sheet_date": str(s.sheet_date) if s.sheet_date else None, "status": s.status,
         "source": s.source, "created_at": s.created_at.isoformat() if s.created_at else None,
         "cash_in": float((d.get("cash_in") or {}).get("total") or 0) or cash_counted(d.get("cash_in")),
         "cash_out": float((d.get("cash_out") or {}).get("total") or 0) or cash_counted(d.get("cash_out")),
         "expenses": exp, "absent": len(d.get("absent") or []), "has_photo": bool(s.photo_path)}
    if full:
        o["data"] = d
        o["summary"] = summary(d, s.sheet_date)
    return o


@router.get("/status", dependencies=[Depends(live_check)])
def sheet_status(auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    return {"enabled": enabled()}


@router.get("")
def list_sheets(month: str | None = None, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> list[dict]:
    q = select(DailySheet).where(DailySheet.status.in_(["saved", "pending"]))
    if month:
        y, m = (int(x) for x in month.split("-"))
        q = q.where(DailySheet.sheet_date >= date(y, m, 1), DailySheet.sheet_date < (date(y + (m == 12), m % 12 + 1, 1)))
    return [_out(s) for s in db.scalars(q.order_by(DailySheet.sheet_date.desc().nulls_first(), DailySheet.created_at.desc()).limit(120))]


@router.post("/scan", status_code=201, dependencies=[Depends(live_check)])
async def scan(outlet_id: uuid.UUID = Form(...), file: UploadFile = File(...), auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    data = await file.read(15 * 1024 * 1024 + 1)
    if not data or len(data) > 15 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Send a photo or PDF under 15 MB")
    with tenant_session(auth.tenant_id) as db:
        if db.get(Outlet, outlet_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")
        ensure_outlet_access(db, auth, outlet_id)
    try:
        sheet = await read_sheet(data, file.content_type or "")
    except ScanError as e:
        raise HTTPException(e.status, str(e)) from e
    with tenant_session(auth.tenant_id) as db:
        sid = uuid.uuid4()
        s = DailySheet(id=sid, tenant_id=auth.tenant_id, outlet_id=outlet_id, sheet_date=parse_date(sheet), data=sheet, source="app",
                       created_by=auth.user_id, photo_path=keep_photo(str(auth.tenant_id), sid, data, file.content_type or ""))
        db.add(s)
        db.flush()
        return _out(s, True)


@router.get("/{sheet_id}")
def get_sheet(sheet_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    s = db.get(DailySheet, sheet_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sheet not found")
    return _out(s, True)


@router.get("/{sheet_id}/photo")
def photo(sheet_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> FileResponse:
    s = db.get(DailySheet, sheet_id)
    if s is None or not s.photo_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No photo")
    return FileResponse(s.photo_path)


class SheetEdit(BaseModel):
    sheet_date: date | None = None
    data: dict | None = None
    save: bool = False


@router.put("/{sheet_id}")
def edit_sheet(sheet_id: uuid.UUID, body: SheetEdit, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    s = db.get(DailySheet, sheet_id)
    if s is None or s.status in ("discarded", "replaced"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sheet not found")
    if body.data is not None:
        s.data = body.data
    if body.sheet_date is not None:
        s.sheet_date = body.sheet_date
    note = None
    if body.save or s.status == "saved":
        try:
            note = save(db, s)
        except ScanError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    db.flush()
    return {**_out(s, True), "note": note}


@router.post("/{sheet_id}/discard")
def discard(sheet_id: uuid.UUID, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    s = db.get(DailySheet, sheet_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sheet not found")
    s.status = "discarded"
    return {"ok": True}
