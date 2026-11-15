"""wise-plus: one business's data, on its own: download it, and put it back without touching anyone else.

export_zip(): a ZIP with
  - data.json.gz   every row of every table that belongs to this business (the restore file)
  - readable.xlsx  bills, items sold, menu, customers, stock, purchases, suppliers, dues, bank lines, credit notes
  - files/         bill photos, daily sheets, bank statements, logos
restore_zip(): server side (wiseplus-cloud restore-tenant). In one transaction: this business's rows are removed
(children first) and the saved ones put back (parents first). Other businesses' rows are never read or changed.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import uuid
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, select, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Base, Tenant

VERSION = 1
APPEND_ONLY = {"audit_log"}    # never deleted (not even by a restore): rows from the backup are added back if missing
SKIP = {"auth_throttle", "billing_rz_plans", "nightly_runs", "otp_codes", "wa_contacts", "wa_state"}   # not one business's data
FOLDERS = ["{t}", "scans/{t}", "sheets/{t}", "statements/{t}"]
READABLE = [  # (sheet, table, columns or None for all)
    ("Bills", "bills", ["business_date", "invoice_no", "status", "customer_name", "customer_phone", "customer_gstin", "gross_paise",
                        "discount_paise", "taxable_paise", "cgst_paise", "sgst_paise", "igst_paise", "round_off_paise", "total_paise"]),
    ("Items sold", "bill_lines", ["bill_id", "item_name", "variant_name", "qty", "unit_paise", "taxable_paise", "cgst_paise", "sgst_paise", "rate_bp"]),
    ("Payments", "payments", ["bill_id", "mode", "amount_paise", "reference"]),
    ("Credit notes", "credit_notes", ["note_date", "number", "bill_id", "reason", "total_paise", "refund_mode", "status"]),
    ("Menu items", "menu_items", None), ("Variants", "variants", None), ("Customers", "customers", None),
    ("Stock items", "stock_items", ["name", "unit", "category", "kind", "last_cost_k", "min_level_m"]),
    ("Purchases", "purchases", ["invoice_date", "invoice_no", "supplier_id", "amount_paise", "tax_paise", "total_paise", "status"]),
    ("Suppliers", "suppliers", ["id", "name", "phone", "gstin"]),
    ("Supplier dues", "supplier_entries", ["entry_date", "supplier_id", "kind", "amount_paise", "mode", "reference", "note", "status"]),
    ("Bank lines", "bank_txns", ["txn_date", "narration", "ref", "amount_paise", "kind", "category", "status"]),
    ("Cash in-out", "cash_movements", ["created_at", "kind", "amount_paise", "purpose", "category", "paid_to", "reason"]),
]


def tables():
    return [t for t in Base.metadata.sorted_tables if t.name not in SKIP and ("tenant_id" in t.c or t.name == "tenants")]


def _js(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, bytes):
        return v.hex()
    return v


def dump(db: Session, tenant_id) -> dict:
    out = {"version": VERSION, "tenant_id": str(tenant_id), "made_at": datetime.now(timezone.utc).isoformat(), "tables": {}}
    for t in tables():
        where = (t.c.id == tenant_id) if t.name == "tenants" else (t.c.tenant_id == tenant_id)
        rows = db.execute(select(t).where(where)).mappings().all()
        out["tables"][t.name] = [{k: _js(v) for k, v in r.items()} for r in rows]
    return out


def _files(tenant_id) -> list[tuple[str, str]]:
    base = get_settings().upload_dir
    out = []
    for f in FOLDERS:
        d = os.path.join(base, f.format(t=tenant_id))
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                out.append((p, "files/" + f.format(t="business") + "/" + n))
    return out


def readable(data: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "About"
    t = (data["tables"].get("tenants") or [{}])[0]
    ws.append([f"{t.get('name', '')}: all data, {data['made_at'][:10]}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Amounts ending in _paise are in paise (divide by 100 for rupees). The full restore file is data.json.gz."])
    for sheet, table, cols in READABLE:
        rows = data["tables"].get(table) or []
        s = wb.create_sheet(sheet[:31])
        names = cols or (list(rows[0].keys()) if rows else [])
        s.append(names)
        for c in s[1]:
            c.font = Font(bold=True)
        for r in rows[:200000]:
            s.append([(json.dumps(r.get(c)) if isinstance(r.get(c), (dict, list)) else r.get(c)) for c in names])
        s.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_zip(db: Session, tenant_id) -> tuple[bytes, str]:
    data = dump(db, tenant_id)
    login = (data["tables"].get("tenants") or [{}])[0].get("login_id", "business")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json.gz", gzip.compress(json.dumps(data, separators=(",", ":")).encode()))
        z.writestr("readable.xlsx", readable(data))
        z.writestr("README.txt", f"Backup of {login}, {data['made_at'][:16].replace('T', ' ')} UTC.\r\n"
                                 "readable.xlsx: open in Excel. data.json.gz: the full restore file (keep it; it puts everything back).\r\n"
                                 "files/: bill photos, daily sheets, bank statements and logos.\r\n")
        for p, n in _files(tenant_id):
            z.write(p, n)
    return buf.getvalue(), f"{login}-backup-{data['made_at'][:10]}.zip"


# ------------------------------------------------------------------ restore (server only)

def _typed(table, row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k not in table.c:
            continue                     # a column that no longer exists
        col = table.c[k]
        if v is not None:
            if isinstance(col.type, UUID):
                v = uuid.UUID(v)
            elif isinstance(col.type, DateTime):
                v = datetime.fromisoformat(v)
            elif isinstance(col.type, Date):
                v = date.fromisoformat(v)
        out[k] = v
    return out


def restore_zip(db: Session, blob: bytes, expect_login: str | None = None, files: bool = True) -> dict:
    """Put one business back as it was in the backup. db must be a session that may see every row (system session).
    Returns rows per table. Raises ValueError on a backup of another business than expected."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    data = json.loads(gzip.decompress(z.read("data.json.gz")))
    if data.get("version") != VERSION:
        raise ValueError("This backup was made by a different version")
    tid = uuid.UUID(data["tenant_id"])
    trow = (data["tables"].get("tenants") or [None])[0]
    if trow is None:
        raise ValueError("No business in this backup")
    if expect_login and trow["login_id"] != expect_login:
        raise ValueError(f"This backup is of {trow['login_id']}, not {expect_login}")
    db.execute(text("set local app.bypass_rls = 'on'"))
    ts = tables()
    for t in reversed(ts):                           # children first
        if t.name == "tenants" or t.name in APPEND_ONLY:
            continue
        db.execute(t.delete().where(t.c.tenant_id == tid))
    tenants = next(t for t in ts if t.name == "tenants")
    if db.get(Tenant, tid) is None:
        db.execute(tenants.insert().values(**_typed(tenants, trow)))
    else:
        db.execute(tenants.update().where(tenants.c.id == tid).values(**_typed(tenants, trow)))
    counts = {}
    for t in ts:                                     # parents first
        if t.name == "tenants":
            continue
        rows = [_typed(t, r) for r in data["tables"].get(t.name, [])]
        if rows:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            for i in range(0, len(rows), 1000):
                stmt = pg_insert(t).on_conflict_do_nothing() if t.name in APPEND_ONLY else t.insert()
                db.execute(stmt, rows[i:i + 1000])
        counts[t.name] = len(rows)
    if files:
        base = get_settings().upload_dir
        for n in z.namelist():
            if not n.startswith("files/") or n.endswith("/") or ".." in n:
                continue
            rel = n[len("files/"):].replace("business", str(tid), 1)
            dest = os.path.join(base, rel)
            if os.path.realpath(dest).startswith(os.path.realpath(base) + os.sep):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(z.read(n))
    return counts


def main() -> None:
    """python -m app.tenant_backup export <login> <out.zip> | restore <login> <in.zip>"""
    import sys
    from .db import system_session
    cmd, login, path = sys.argv[1], sys.argv[2], sys.argv[3]
    with system_session() as db:
        t = db.scalar(select(Tenant).where(Tenant.login_id == login))
        if cmd == "export":
            if t is None:
                raise SystemExit(f"No business called {login}")
            blob, _ = export_zip(db, t.id)
            open(path, "wb").write(blob)
            print(f"Saved {path} ({len(blob) // 1024} KB)")
        elif cmd == "restore":
            counts = restore_zip(db, open(path, "rb").read(), expect_login=login)
            print("Restored", login + ":", sum(counts.values()), "rows in", sum(1 for v in counts.values() if v), "tables")
        else:
            raise SystemExit("export | restore")


if __name__ == "__main__":
    main()


# ------------------------------------------------------------------ the owner's download

from fastapi import APIRouter, Depends, HTTPException, status  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from .deps import get_auth  # noqa: E402

router = APIRouter(tags=["backup"])


def _owner(auth):
    from .core.permissions import Role
    if auth.role != Role.owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can download the business's data")
    return auth


@router.get("/tenant/backup")
def download(auth=Depends(get_auth)) -> Response:
    _owner(auth)
    from .db import tenant_session
    with tenant_session(auth.tenant_id) as db:
        blob, name = export_zip(db, auth.tenant_id)
        from .services import audit
        audit.record(db, auth.tenant_id, auth.user_id, "export", "tenant_backup", auth.tenant_id, after={"bytes": len(blob)})
    return Response(blob, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})
