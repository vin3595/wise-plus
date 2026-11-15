"""Reading, checking, saving and summarising daily sheets. Used by the app and by WhatsApp."""
from __future__ import annotations

import base64
import os
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assistant.answers import rs
from ..config import get_settings
from ..purchases_ai.service import ScanError
from .constants import DENOMS
from .models import DailySheet

PAYOUTS = {"MUMMY", "BHAVESH", "VINEET"}        # printed rows on the expenses table: money to family / partners


def enabled() -> bool:
    try:
        from . import extract
    except ImportError:
        return False
    return bool(extract.EMERGENT_LLM_KEY)


async def read_sheet(data: bytes, content_type: str) -> dict:
    """The sheet as EMB's reader returns it. Raises ScanError."""
    try:
        from . import extract
    except ImportError as e:
        raise ScanError(f"Sheet scanning isn't installed on this server ({e.name})", 503) from e
    if not extract.EMERGENT_LLM_KEY:
        raise ScanError("Sheet scanning isn't switched on: the server needs WISE_GEMINI_API_KEY", 503)
    prefix = "data:application/pdf;base64," if (content_type or "").endswith("pdf") or data[:4] == b"%PDF" else ""
    try:
        sheet = await extract.extract_daily_sheet(prefix + base64.b64encode(data).decode())
    except Exception as e:
        raise ScanError("The sheet reader didn't answer. Try again in a minute.", 502) from e
    if sheet.get("parse_error"):
        raise ScanError("Couldn't read that sheet. Try a straighter, brighter photo of the whole page.")
    return sheet


def cash_counted(sec: dict | None) -> int:
    """Rupees from the note counts and coins, whatever was written as the total."""
    sec = sec or {}
    return int(round(sum(d * int(sec.get(f"d{d}") or 0) for d in DENOMS) + float(sec.get("coins") or 0)))


def cash_line(label: str, sec: dict | None) -> str | None:
    sec = sec or {}
    counted, written = cash_counted(sec), int(round(float(sec.get("total") or 0)))
    if not counted and not written:
        return None
    if written and abs(written - counted) > 1:
        return f"{label}: {rs(written * 100)} written, but the notes add up to {rs(counted * 100)} ⚠️"
    return f"{label}: {rs((written or counted) * 100)}" + ("" if written else " (counted)")


def rows(items: list | None) -> list[tuple[str, float]]:
    return [(str(r.get("name", "")).strip(), float(r.get("amount") or 0)) for r in (items or []) if float(r.get("amount") or 0)]


def summary(d: dict, sheet_date: date | None) -> str:
    when = sheet_date.strftime("%d %b %Y") if sheet_date else "no date found ⚠️"
    out = [f"*Daily sheet · {when}*"]
    for label, key in (("Cash in", "cash_in"), ("Cash out", "cash_out")):
        ln = cash_line(label, d.get(key))
        if ln:
            out.append(ln)
    exp = [(n, a) for n, a in rows(d.get("expenses")) if n.upper() not in PAYOUTS]
    pay = [(n, a) for n, a in rows(d.get("expenses")) + rows(d.get("expense_payouts")) if n.upper() in PAYOUTS]
    if exp:
        out.append(f"*Expenses {rs(round(sum(a for _, a in exp) * 100))}*: " + ", ".join(f"{n.title()} {a:g}" for n, a in exp))
    if pay:
        out.append("To family: " + ", ".join(f"{n.title()} {a:g}" for n, a in pay))
    vin, vout = rows(d.get("vijay_ras_bhandar")), rows(d.get("vijay_ras"))
    if vin:
        out.append("Vijay Ras in: " + ", ".join(f"{n.title()} {a:g}" for n, a in vin))
    if vout:
        out.append("Vijay Ras out: " + ", ".join(f"{n.title()} {a:g}" for n, a in vout))
    if d.get("absent"):
        out.append("Absent: " + ", ".join(d["absent"]))
    if d.get("advances"):
        out.append("Advances: " + ", ".join(f"{a['name']} {a['amount']:g}" for a in d["advances"]))
    if d.get("dues"):
        out.append("Dues: " + ", ".join(f"{a['name']} {a['amount']:g}" for a in d["dues"]))
    if d.get("notes"):
        out.append(f"_Notes: {d['notes']}_")
    return "\n".join(out)


def parse_date(d: dict) -> date | None:
    try:
        return date.fromisoformat(d.get("date")) if d.get("date") else None
    except ValueError:
        return None


def keep_photo(tenant_id: str, sheet_id: uuid.UUID, data: bytes, content_type: str) -> str:
    folder = os.path.join(get_settings().upload_dir, "sheets", str(tenant_id))
    os.makedirs(folder, exist_ok=True)
    ext = "pdf" if (content_type or "").endswith("pdf") else "jpg"
    path = os.path.join(folder, f"{sheet_id}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def save(db: Session, sheet: DailySheet) -> str | None:
    """Mark saved; an earlier saved sheet for the same outlet and date becomes 'replaced'. Returns a note."""
    if sheet.sheet_date is None:
        raise ScanError("The sheet has no date. Open it in the app (Money › Daily sheets) and set the date first.")
    note = None
    for old in db.scalars(select(DailySheet).where(DailySheet.outlet_id == sheet.outlet_id, DailySheet.sheet_date == sheet.sheet_date,
                                                   DailySheet.status == "saved", DailySheet.id != sheet.id)):
        old.status = "replaced"
        note = f"It replaces the sheet already saved for {sheet.sheet_date.strftime('%d %b')}."
    sheet.status = "saved"
    return note
