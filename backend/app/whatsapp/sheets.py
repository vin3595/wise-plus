"""The daily cash sheet sent on WhatsApp: read with EMB's sheet reader, summarised, saved on Save."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..assistant.answers import Reply
from ..config import get_settings
from ..daily_sheet.models import DailySheet
from ..daily_sheet.service import keep_photo, parse_date, read_sheet, save, summary
from ..db import tenant_session
from ..purchases_ai.service import ScanError
from .bills import _outlet


def _link() -> str:
    return get_settings().public_base_url.rstrip("/") + "/sheets"


async def read_sent_sheet(chosen: tuple, phone10: str, data: bytes, ct: str, caption: str) -> Reply:
    tenant_id, login_id, role, user_id = chosen
    sheet = await read_sheet(data, ct)

    def store() -> Reply:
        with tenant_session(tenant_id) as db:
            out = _outlet(db, caption)
            if out is None:
                return Reply("Add an outlet in the app first (Setup › Outlets).")
            sid = uuid.uuid4()
            d = parse_date(sheet)
            db.add(DailySheet(id=sid, tenant_id=uuid.UUID(tenant_id), outlet_id=out.id, sheet_date=d, data=sheet, sender=phone10,
                              created_by=uuid.UUID(user_id), photo_path=keep_photo(tenant_id, sid, data, ct)))
            text = summary(sheet, d)
            if d is None:
                return Reply(text + f"\n\nNo date on the sheet. Set it in the app and save there: {_link()}", [(f"dropsheet {sid}", "Discard")])
            again = db.scalar(select(DailySheet.id).where(DailySheet.outlet_id == out.id, DailySheet.sheet_date == d, DailySheet.status == "saved"))
            warn = f"\n\n⚠️ A sheet for {d.strftime('%d %b')} is already saved; saving replaces it." if again else ""
            return Reply(text + warn + f"\n\nWrong number somewhere? Fix it in the app: {_link()}",
                         [(f"savesheet {sid}", "Save"), (f"dropsheet {sid}", "Discard")])
    return await run_in_threadpool(store)


def save_or_drop(db: Session, phone10: str, keep: bool, sheet_id: uuid.UUID | None = None) -> Reply | None:
    """None = no sheet waiting from this phone (the caller says so)."""
    if sheet_id is not None:
        s = db.get(DailySheet, sheet_id)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=2)
        s = db.scalar(select(DailySheet).where(DailySheet.status == "pending", DailySheet.sender == phone10, DailySheet.created_at >= since)
                      .order_by(DailySheet.created_at.desc()).limit(1))
    if s is None:
        return None
    if s.status != "pending":
        return Reply("That sheet was already " + ("saved." if s.status in ("saved", "replaced") else "discarded."))
    if not keep:
        s.status = "discarded"
        return Reply("Sheet discarded.")
    try:
        note = save(db, s)
    except ScanError as e:
        return Reply(str(e))
    return Reply(f"✅ Daily sheet for {s.sheet_date.strftime('%d %b %Y')} saved." + (f" {note}" if note else ""),
                 [("expenses", "This month's expenses"), (f"sheet {s.sheet_date.strftime('%d %b')}", "See it")])
