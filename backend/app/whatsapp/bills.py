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

from ..assistant.answers import Reply, rs
from ..config import get_settings
from ..core.permissions import Perm, Role
from ..db import tenant_session
from ..deps import Auth
from ..ledger.balances import statement
from ..models import Outlet
from ..purchases_ai.models import ScannedBill
from ..purchases_ai.service import ScanError, classify, enabled, read_bill, ready_to_save, tenant_draft
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


_reading = None


def _gate():
    """At most 3 bills read at once; the rest wait their turn (a batch of photos would otherwise all hit
    Gemini in the same second and run into its per-minute limit)."""
    global _reading
    import asyncio
    if _reading is None:
        _reading = asyncio.Semaphore(3)
    return _reading


async def read_sent_bill(chosen: tuple, phone10: str, media_id: str, mime: str, filename: str, caption: str = "") -> Reply:
    async with _gate():
        return await _read_sent_bill(chosen, phone10, media_id, mime, filename, caption)


def _allowance(tenant_id) -> str | None:
    """wise-plus billing: trial over, or this month's scans used up."""
    from ..billing.service import ScanLimit, check_scan, locked
    from ..models import Tenant
    with tenant_session(tenant_id) as db:
        t = db.get(Tenant, tenant_id)
        if locked(db, t):
            return "Your free trial has ended. Choose a plan in the app (Settings › Billing) to keep sending bills."
        try:
            check_scan(db, t)
        except ScanLimit as e:
            return str(e)
    return None


def _counted(tenant_id) -> None:
    from ..billing.service import count_scan
    from ..models import Tenant
    with tenant_session(tenant_id) as db:
        count_scan(db, db.get(Tenant, tenant_id))


async def _read_sent_bill(chosen: tuple, phone10: str, media_id: str, mime: str, filename: str, caption: str = "") -> Reply:
    tenant_id, login_id, role, user_id = chosen
    data, ct = await run_in_threadpool(client.get_media, media_id)
    ct = (mime or ct or "").split(";")[0].lower()
    from ..banking.parse import looks_like_bank_statement
    # A bank statement from net banking (Excel / CSV) is read without the AI, so it works even with bill scanning off.
    if not re.search(r"\bsheet\b", caption or "", re.I) and await run_in_threadpool(looks_like_bank_statement, data, ct, filename or "", caption or ""):
        if Perm.banks not in effective(uuid.UUID(tenant_id), Role(role)):
            return Reply("Bank statements are for the owner. Ask them to allow it under Staff › Who can do what.")
        return await _read_bank(tenant_id, user_id, data, ct, filename or "statement")
    if not enabled():
        return Reply("Bill scanning isn't switched on for this restaurant yet.")
    stop = await run_in_threadpool(_allowance, tenant_id)
    if stop:
        return Reply(stop)
    try:
        from ..ledger import statement as stm
        if re.search(r"\bsheet\b", caption or "", re.I):
            kind = {"type": "LEDGER_PAGE"}
        elif await run_in_threadpool(stm.looks_like_statement, data, ct, filename or "", caption or ""):
            kind = {"type": "SUPPLIER_STATEMENT"}
        elif (filename or "").lower().endswith((".xlsx", ".xls", ".csv")):
            return Reply("Couldn't find bank transactions in that file. Send the statement as downloaded from net banking.")
        else:
            kind = await classify(data, ct, filename or "bill")
        if kind.get("type") == "BANK_STATEMENT":
            if Perm.banks not in effective(uuid.UUID(tenant_id), Role(role)):
                return Reply("Bank statements are for the owner. Ask them to allow it under Staff › Who can do what.")
            return await _read_bank(tenant_id, user_id, data, ct, filename or "statement")
        if kind.get("type") == "SUPPLIER_STATEMENT":
            # A supplier's ledger: which of their bills are in the POS, and their payments into supplier dues.
            if Perm.inventory_manage not in effective(uuid.UUID(tenant_id), Role(role)):
                return Reply("Your account can't check supplier statements. Ask the owner to allow it under Staff › Who can do what.")
            return await _read_statement(tenant_id, user_id, data, ct, filename or "statement")
        if kind.get("type") == "LEDGER_PAGE":
            # The daily cash sheet, not a supplier's bill.
            from . import sheets
            return await sheets.read_sent_sheet(chosen, phone10, data, ct, caption)
        if Perm.inventory_manage not in effective(uuid.UUID(tenant_id), Role(role)):
            return Reply("Your account can't add purchases. Ask the owner to allow it under Staff › Who can do what.")
        bill, thumb, kind = await read_bill(data, ct, filename or "bill", kind=kind)
    except ScanError as e:
        return Reply(str(e))
    await run_in_threadpool(_counted, tenant_id)

    def store() -> Reply:
        with tenant_session(tenant_id) as db:
            out = _outlet(db, caption)
            if out is None:
                return Reply("Add an outlet in the app first (Setup › Outlets).")
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
            if why and _addable(d):
                return Reply(summary(d) + "\n\n🆕 New in Wise: " + "; ".join(why) + "."
                             + f"\nAdd them and save the bill, or finish it in the app: {_link()}",
                             [(f"add {sid}", "Add & save"), (f"discard {sid}", "Discard")])
            if why:
                return Reply(summary(d) + "\n\n⚠️ Needs a look: " + "; ".join(why) + f".\nFinish it in the app under Stock › Purchases: {_link()}",
                             [(f"discard {sid}", "Discard")])
            return Reply(summary(d) + "\n\nAdd it to stock and supplier dues?", [(f"save {sid}", "Save"), (f"discard {sid}", "Discard")])
    return await run_in_threadpool(store)


async def _read_bank(tenant_id: str, user_id: str, data: bytes, ct: str, filename: str) -> Reply:
    from ..banking import parse, service as bsvc
    from ..banking.router import run_import
    try:
        st = await parse.read_statement(data, ct, filename)
    except parse.BankParseError as e:
        return Reply(str(e))
    if data[:4] == b"%PDF" or ct.startswith("image/"):      # the AI read it: counts like a scan
        await run_in_threadpool(_counted, tenant_id)
    res = await run_in_threadpool(run_import, uuid.UUID(tenant_id), uuid.UUID(user_id), data, st, filename)
    return Reply(bsvc.summary_text(res), [("bank check", f"Check {res['to_check']}")] if res["to_check"] else [("bank", "Balances")])


async def _read_statement(tenant_id: str, user_id: str, data: bytes, ct: str, filename: str) -> Reply:
    from ..ledger import statement as stm
    try:
        st = await stm.read_statement(data, ct, filename)
    except stm.StatementError as e:
        return Reply(str(e))
    if not st["rows"]:
        return Reply("No bills or payments found on that statement. Try the PDF, or a straighter photo.")
    await run_in_threadpool(_counted, tenant_id)

    def go() -> Reply:
        with tenant_session(tenant_id) as db:
            res = stm.check(db, uuid.UUID(tenant_id), st)
            added = stm.auto_apply(db, uuid.UUID(tenant_id), uuid.UUID(user_id), res)
            return Reply(stm.summary_text(res, added) + f"\n\nFull check in the app: Money › Supplier dues › Check a statement {_link()}")
    return await run_in_threadpool(go)


def _addable(d: dict) -> bool:
    """Only new things are missing (supplier and/or items), nothing the person must judge."""
    return (not d.get("duplicate") and bool(d.get("lines"))
            and all(ln.get("qty_m") for ln in d["lines"])
            and (not d.get("supplier") or any(not ln.get("stock_item_id") for ln in d["lines"])))


def _nice(name: str) -> str:
    """'VEEBA PIZZA PASTA SAUCE 1KG' -> 'Veeba Pizza Pasta Sauce 1kg'."""
    out = []
    for w in re.sub(r"\s+", " ", name).strip().split(" "):
        out.append(w.lower() if re.match(r"^\(?\d", w) else w[:1].upper() + w[1:].lower())
    return " ".join(out)[:120]


def add_new(db: Session, d: dict, auth: Auth) -> list[str]:
    """Create the supplier and stock items this bill needs, through Wise's own create functions (same checks and
    audit log as the app). Items with a pack size on the line ('1KG') are kept in g / ml and bought by that pack;
    others are counted in pieces."""
    from fastapi import HTTPException
    from sqlalchemy import func
    from ..core.stock import STANDARD_UNITS
    from ..models import StockItem, Supplier
    from ..purchases_ai.match import pack_size
    from ..routers.inventory import StockItemIn, SupplierIn, UnitIn, create_item, create_supplier
    made = []
    if not d.get("supplier") and d.get("vendor"):
        name = _nice(d["vendor"])
        if db.scalar(select(Supplier.id).where(func.lower(Supplier.name) == name.lower())) is None:
            gstin = (d.get("gstin") or "").strip().upper()[:15] or None
            try:
                create_supplier(SupplierIn(name=name, gstin=gstin), auth, db)
            except Exception:       # a GSTIN the form wouldn't accept: keep the supplier, drop the GSTIN
                create_supplier(SupplierIn(name=name), auth, db)
            made.append(f"supplier {name}")
    for ln in d.get("lines") or []:
        if ln.get("stock_item_id"):
            continue
        name = _nice(ln["bill_name"])
        if db.scalar(select(StockItem.id).where(func.lower(StockItem.name) == name.lower())) is not None:
            continue
        ps = pack_size(ln["bill_name"])
        unit, packs = "pc", []
        if ps:
            base_unit, per = STANDARD_UNITS.get(ps[1], ("", 0))
            if base_unit in ("g", "ml") and per:
                unit = base_unit
                packs = [UnitIn(name=f"pack {ps[0]:g}{ps[1]}", factor=int(round(ps[0] * per)))]
        try:
            create_item(StockItemIn(name=name, kind="raw", unit=unit, purchase_units=packs), auth, db)
            made.append(name)
        except HTTPException:
            pass
    return made


def save_or_discard(db: Session, chosen: tuple, phone10: str, save: bool, add_new_items: bool = False,
                    bill_id: uuid.UUID | None = None, **kw) -> Reply:
    """bill_id: from a button under that bill's message. Typed 'save' with several bills waiting asks which."""
    add_new_items = add_new_items or kw.get("add_new", False)
    tenant_id, login_id, role, user_id = chosen
    since = datetime.now(timezone.utc) - timedelta(days=2)
    if bill_id is not None:
        b = db.get(ScannedBill, bill_id)
        if b is None:
            return Reply("That bill isn't here any more.")
        if b.status != "pending":
            return Reply("That bill was already " + ("saved." if b.status == "saved" else "discarded."))
    else:
        waiting = db.scalars(select(ScannedBill).where(ScannedBill.status == "pending", ScannedBill.sender == phone10,
                                                       ScannedBill.created_at >= since).order_by(ScannedBill.created_at.desc())).all()
        if not waiting:
            return Reply("No bill waiting from you. Send a photo or PDF of a supplier's bill.")
        if len(waiting) > 1:
            verb = "add" if add_new_items else "save" if save else "discard"
            def label(x: ScannedBill) -> str:
                dd = x.draft or {}
                return f"{((dd.get('supplier') or {}).get('name') or dd.get('vendor') or 'Bill')[:12]} {rs(dd.get('total_paise') or 0)}"
            return Reply(f"{len(waiting)} bills are waiting. Which one? (Or use the buttons under each bill.)",
                         [(f"{verb} {x.id}", label(x)) for x in waiting[:10]])
        b = waiting[0]
    if not save:
        b.status = "discarded"
        return Reply("Discarded. It won't be added.")
    if Perm.inventory_manage not in effective(uuid.UUID(tenant_id), Role(role)):
        return Reply("Your account can't add purchases.")
    # Match again: a supplier or stock item may have been added since the photo came in.
    d = tenant_draft(db, b.draft.get("_bill") or {}) if b.draft.get("_bill") else b.draft
    made: list[str] = []
    if add_new_items and ready_to_save(d) and _addable(d):
        made = add_new(db, d, Auth(user_id=uuid.UUID(user_id), tenant_id=uuid.UUID(tenant_id), role=Role(role), outlet_id=None, via="whatsapp"))
        d = tenant_draft(db, b.draft.get("_bill") or {}) if b.draft.get("_bill") else d
    why = ready_to_save(d)
    if why:
        b.draft = {**d, "thumbnail": b.draft.get("thumbnail"), "_bill": b.draft.get("_bill")}
        opts = [("add", "Add & save"), ("discard", "Discard")] if _addable(d) else [("discard", "Discard")]
        return Reply("Can't save it from here: " + "; ".join(why) + f".\nFinish it in the app under Stock › Purchases: {_link()}", opts)
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
    added = ("Added " + ", ".join(made) + ".\n") if made else ""
    return Reply(f"{added}✅ Saved: {d['supplier']['name']}, {rs(p['total_paise'])}. Stock updated."
                 f"\n{d['supplier']['name']} now: {rs(due)} due.",
                 [(f"dues {d['supplier']['name']}", "Their account"), ("dues", "All dues")])
