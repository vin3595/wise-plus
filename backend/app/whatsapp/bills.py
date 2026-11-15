"""Supplier bills sent on WhatsApp: read with the Scan bill reader, kept as a pending bill, saved on 'save'.

A bill saves straight from WhatsApp only when nothing needs a person: supplier known, every line matched
to a stock item, not entered before. Otherwise it waits under Stock > Purchases in the app."""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..assistant.answers import rs
from ..config import get_settings
from ..core.permissions import Perm, Role
from ..db import tenant_session
from ..deps import Auth
from ..ledger.balances import statement
from ..models import Outlet
from ..purchases_ai.models import ScannedBill
from ..purchases_ai.service import ScanError, enabled, read_bill, ready_to_save, tenant_draft
from ..services.role_perms import effective
from . import client

log = logging.getLogger("wise.whatsapp")
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf", "image/heic": "heic"}


def _link() -> str:
    return get_settings().public_base_url.rstrip("/") + "/stock"


def _outlet(db: Session, caption: str) -> Outlet | None:
    """The outlet named in the caption ("paota"), else the first one."""
    outs = db.scalars(select(Outlet).order_by(Outlet.created_at)).all()
    words = set(re.findall(r"[a-z0-9]+", (caption or "").lower()))
    for o in outs:
        if words & set(re.findall(r"[a-z0-9]+", f"{o.name} {o.code or ''}".lower())):
            return o
    return outs[0] if outs else None


def summary(d: dict) -> str:
    sup = (d.get("supplier") or {}).get("name") or d.get("vendor") or "Unknown supplier"
    head = f"*{sup}*" + (f" · Bill {d['invoice_no']}" if d.get("invoice_no") else "") + (f" · {d['invoice_date']}" if d.get("invoice_date") else "")
    lines = [head]
    for ln in (d.get("lines") or [])[:12]:
        q = (ln.get("qty_m") or 0) / 1000
        what = ln.get("stock_item_name") or f"{ln['bill_name']} ❓"
        lines.append(f"• {what}: {q:g} {ln.get('unit') or ''} · {rs(ln.get('amount_paise') or 0)}")
    if len(d.get("lines") or []) > 12:
        lines.append(f"…and {len(d['lines']) - 12} more")
    lines.append(f"*Total {rs(d.get('total_paise') or 0)}*")
    if d.get("adjusted"):
        lines.append(f"_{d['adjusted']['why']}_")
    return "\n".join(lines)


async def read_sent_bill(chosen: tuple, phone10: str, media_id: str, mime: str, filename: str, caption: str = "") -> str:
    tenant_id, login_id, role, user_id = chosen
    if Perm.inventory_manage not in effective(uuid.UUID(tenant_id), Role(role)):
        return "Your account can't add purchases. Ask the owner to allow it under Staff › Who can do what."
    if not enabled():
        return "Bill scanning isn't switched on for this restaurant yet."
    data, ct = await run_in_threadpool(client.get_media, media_id)
    ct = (mime or ct or "").split(";")[0].lower()
    try:
        bill, thumb, kind = await read_bill(data, ct, filename or "bill")
    except ScanError as e:
        return str(e)

    def store() -> str:
        with tenant_session(tenant_id) as db:
            out = _outlet(db, caption)
            if out is None:
                return "Add an outlet in the app first (Setup › Outlets)."
            folder = os.path.join(get_settings().upload_dir, "scans", tenant_id)
            os.makedirs(folder, exist_ok=True)
            sid = uuid.uuid4()
            path = os.path.join(folder, f"{sid}.{EXT.get(ct, 'bin')}")
            with open(path, "wb") as f:
                f.write(data)
            d = tenant_draft(db, bill)
            db.add(ScannedBill(id=sid, tenant_id=uuid.UUID(tenant_id), outlet_id=out.id, sender=phone10, user_id=uuid.UUID(user_id),
                               file_path=path, draft={**d, "thumbnail": thumb, "low_confidence": bool(kind.get("should_warn")), "_bill": bill}))
            why = ready_to_save(d)
            if why:
                return (summary(d) + "\n\n⚠️ Needs a look: " + "; ".join(why) + f".\nFinish it in the app under Stock › Purchases: {_link()}"
                        + "\n(or reply *discard*)")
            return summary(d) + "\n\nReply *save* to add it to stock and supplier dues, or *discard*."
    return await run_in_threadpool(store)


def save_or_discard(db: Session, chosen: tuple, phone10: str, save: bool) -> str:
    tenant_id, login_id, role, user_id = chosen
    since = datetime.now(timezone.utc) - timedelta(days=2)
    b = db.scalar(select(ScannedBill).where(ScannedBill.status == "pending", ScannedBill.sender == phone10, ScannedBill.created_at >= since)
                  .order_by(ScannedBill.created_at.desc()).limit(1))
    if b is None:
        return "No bill waiting from you. Send a photo or PDF of a supplier's bill."
    if not save:
        b.status = "discarded"
        return "Discarded. It won't be added."
    if Perm.inventory_manage not in effective(uuid.UUID(tenant_id), Role(role)):
        return "Your account can't add purchases."
    # Match again: a supplier or stock item may have been added since the photo came in.
    d = tenant_draft(db, b.draft.get("_bill") or {}) if b.draft.get("_bill") else b.draft
    why = ready_to_save(d)
    if why:
        b.draft = {**d, "thumbnail": b.draft.get("thumbnail"), "_bill": b.draft.get("_bill")}
        return "Can't save it from here: " + "; ".join(why) + f".\nFinish it in the app under Stock › Purchases: {_link()}"
    from ..routers.inventory import PurchaseIn, PurchaseLineIn, create_purchase
    try:
        inv_date = date.fromisoformat(d["invoice_date"]) if d.get("invoice_date") else date.today()
    except ValueError:
        inv_date = date.today()
    body = PurchaseIn(outlet_id=b.outlet_id, supplier_id=uuid.UUID(d["supplier"]["id"]), invoice_no=d.get("invoice_no"), invoice_date=inv_date,
                      notes="Sent on WhatsApp",
                      lines=[PurchaseLineIn(stock_item_id=uuid.UUID(ln["stock_item_id"]), qty_m=ln["qty_m"], unit=ln["unit"],
                                            amount_paise=ln["amount_paise"], gst_bp=ln["gst_bp"], bill_name=ln["bill_name"][:120])
                             for ln in d["lines"]])
    auth = Auth(user_id=uuid.UUID(user_id), tenant_id=uuid.UUID(tenant_id), role=Role(role), outlet_id=None, via="whatsapp")
    p = create_purchase(body, auth, db)
    b.status, b.purchase_id = "saved", uuid.UUID(p["id"])
    due = statement(db, body.supplier_id)["balance_paise"]
    return (f"✅ Saved: {d['supplier']['name']}, {rs(p['total_paise'])}. Stock updated."
            f"\n{d['supplier']['name']} now: {rs(due)} due.")
