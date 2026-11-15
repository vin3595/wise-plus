"""wise-plus: table service from the table QR.

The same sticker on each table (…/q/<outlet key>?t=T4) opens a page with four big buttons:
Order food (the existing QR ordering) · Water · Call waiter · Ask for bill.
A request pops up on every POS and waiter phone of that outlet at once, with a sound, and stays
until someone taps Done. Asking again bumps it (at most once a minute) instead of adding another.
Ask for bill: the customer sees the running total of the table and can pay by UPI on the phone;
the cashier makes the bill as usual, and the customer's page then shows the bill.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.billing import compute_bill
from ..core.kds import business_date
from ..core.permissions import Perm
from ..db import system_session, tenant_session
from ..deps import Auth, ensure_outlet_access, get_db, require
from ..models import Bill, Order, Outlet, Tenant, User
from ..services.billing import billable_rows, upi_link
from ..services.orders import open_table_order
from .models import TableRequest

router = APIRouter(tags=["table-service"])
KINDS = {"water": "Water", "waiter": "Waiter", "bill": "Bill"}
BUMP_EVERY = timedelta(seconds=60)
MAX_OPEN_PER_TABLE = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _outlet(qr_key: str) -> tuple[uuid.UUID, uuid.UUID]:
    with system_session() as db:
        o = db.scalar(select(Outlet).where(Outlet.qr_key == qr_key))
        t = db.get(Tenant, o.tenant_id) if o else None
        if o is None or not o.is_active or not o.table_service or t is None or t.status != "active":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Table service isn't available here")
        return o.id, o.tenant_id


def _table(outlet: Outlet, t: str | None) -> str:
    table = (t or "").strip().upper()
    if not table or table not in (outlet.table_labels or []):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan the QR on your table")
    return table


def tab(db: Session, outlet: Outlet, table: str) -> dict | None:
    """What the table has ordered so far and what it comes to (before any discount at the counter)."""
    order = open_table_order(db, outlet.id, table)
    if order is None:
        return None
    rows = billable_rows(db, order)
    if not rows:
        return None
    calc = compute_bill([r.line for r in rows], None)
    items: dict[str, int] = {}
    for r in rows:
        oi = r.order_item
        name = f"{oi.item_name}" + (f" ({oi.variant_name})" if oi.variant_name else "")
        items[name] = items.get(name, 0) + int(oi.qty)
    return {"order_id": str(order.id), "items": [{"name": k, "qty": v} for k, v in items.items()],
            "total_paise": calc.total_paise}


def _req_out(r: TableRequest, now: datetime | None = None) -> dict:
    now = now or _now()
    return {"id": str(r.id), "table": r.table_label, "kind": r.kind, "label": KINDS[r.kind], "status": r.status,
            "times_asked": r.times_asked, "note": r.note, "since": r.created_at.isoformat(),
            "waiting_secs": int(((r.done_at or now) - r.created_at).total_seconds()),
            "last_asked": r.last_asked_at.isoformat()}


# ------------------------------------------------------------------ the customer's phone

class RequestIn(BaseModel):
    table: str = Field(min_length=1, max_length=20)
    kind: str = Field(pattern=r"^(water|waiter|bill)$")
    device: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, pattern=r"^(paid_upi|pay_counter)$")


@router.get("/public/table/{qr_key}")
def table_page(qr_key: str, t: str | None = None, device: str | None = None) -> dict:
    outlet_id, tenant_id = _outlet(qr_key)
    with tenant_session(tenant_id) as db:
        outlet, tenant = db.get(Outlet, outlet_id), db.get(Tenant, tenant_id)
        table = _table(outlet, t)
        open_reqs = db.scalars(select(TableRequest).where(TableRequest.outlet_id == outlet_id, TableRequest.table_label == table,
                                                          TableRequest.created_at >= _now() - timedelta(hours=3))
                               .order_by(TableRequest.created_at.desc())).all()
        latest = {}
        for r in open_reqs:
            latest.setdefault(r.kind, _req_out(r))
        return {"outlet_name": outlet.name, "app_name": tenant.app_name or tenant.name, "logo_url": tenant.logo_url,
                "primary_color": tenant.primary_color, "table": table, "ordering": outlet.qr_ordering,
                "tab": tab(db, outlet, table), "requests": latest, "upi": bool(outlet.upi_vpa),
                "bill": _bill_view(db, outlet, tenant, table)}


def _bill_view(db: Session, outlet: Outlet, tenant: Tenant, table: str) -> dict | None:
    """For a table that asked for the bill: the amount and UPI link now, the bill itself once it's made."""
    r = db.scalar(select(TableRequest).where(TableRequest.outlet_id == outlet.id, TableRequest.table_label == table,
                                             TableRequest.kind == "bill", TableRequest.created_at >= _now() - timedelta(hours=3))
                  .order_by(TableRequest.created_at.desc()))
    if r is None:
        return None
    out: dict = {"request_id": str(r.id), "status": r.status, "note": r.note}
    if r.order_id:
        bill = db.scalar(select(Bill).where(Bill.order_id == r.order_id, Bill.status == "paid"))
        if bill is not None:
            from ..services.sms import app_base_url, ensure_share_key
            out.update({"paid": True, "invoice_no": bill.invoice_no, "total_paise": bill.total_paise,
                        "bill_url": f"{app_base_url(tenant)}/b/{ensure_share_key(bill)}"})
            return out
    t = tab(db, outlet, table)
    if t:
        out.update({"paid": False, "total_paise": t["total_paise"],
                    "upi_link": upi_link(outlet, tenant, t["total_paise"], f"Table {table}")})
    return out


@router.post("/public/table/{qr_key}/requests", status_code=201)
def ask(qr_key: str, body: RequestIn) -> dict:
    outlet_id, tenant_id = _outlet(qr_key)
    with tenant_session(tenant_id) as db:
        outlet = db.get(Outlet, outlet_id)
        table = _table(outlet, body.table)
        now = _now()
        mine = db.scalars(select(TableRequest).where(TableRequest.outlet_id == outlet_id, TableRequest.table_label == table,
                                                     TableRequest.status == "open").with_for_update()).all()
        same = next((r for r in mine if r.kind == body.kind), None)
        if same is not None:          # asking again: bump it (not more than once a minute) so it rises to the top
            if body.note:
                same.note = body.note
            if now - same.last_asked_at >= BUMP_EVERY and same.times_asked < 10:
                same.times_asked += 1
                same.last_asked_at = now
            db.flush()
            return _req_out(same, now) | {"again": True}
        if len(mine) >= MAX_OPEN_PER_TABLE:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Someone is on the way to your table")
        order = open_table_order(db, outlet_id, table)
        if body.kind == "bill" and order is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "There's nothing to bill on this table yet")
        r = TableRequest(tenant_id=tenant_id, outlet_id=outlet_id, table_label=table, kind=body.kind, note=body.note,
                         order_id=order.id if order else None, device=body.device, created_at=now, last_asked_at=now)
        db.add(r)
        db.flush()
        return _req_out(r, now)


# ------------------------------------------------------------------ the POS and waiter phones

@router.get("/table-requests")
def open_requests(outlet_id: uuid.UUID, auth: Auth = Depends(require(Perm.kot_send)), db: Session = Depends(get_db)) -> dict:
    """Open requests (oldest first) and the ones done in the last 10 minutes."""
    ensure_outlet_access(db, auth, outlet_id)
    now = _now()
    rows = db.scalars(select(TableRequest).where(TableRequest.outlet_id == outlet_id, (TableRequest.status == "open") | (
        (TableRequest.status == "done") & (TableRequest.done_at >= now - timedelta(minutes=10)))).order_by(TableRequest.created_at)).all()
    totals = {}
    for r in rows:
        if r.kind == "bill" and r.status == "open" and r.order_id:
            o = db.get(Order, r.order_id)
            if o is not None:
                rws = billable_rows(db, o)
                totals[r.id] = compute_bill([x.line for x in rws], None).total_paise if rws else 0
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_({r.done_by for r in rows if r.done_by})))}
    return {"open": [_req_out(r, now) | {"total_paise": totals.get(r.id), "order_id": str(r.order_id) if r.order_id else None}
                     for r in rows if r.status == "open"],
            "done": [_req_out(r, now) | {"done_by": names.get(r.done_by)} for r in reversed(rows) if r.status == "done"]}


@router.post("/table-requests/{request_id}/done")
def done(request_id: uuid.UUID, auth: Auth = Depends(require(Perm.kot_send)), db: Session = Depends(get_db)) -> dict:
    r = db.get(TableRequest, request_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    ensure_outlet_access(db, auth, r.outlet_id)
    if r.status == "open":
        r.status, r.done_at, r.done_by = "done", _now(), auth.user_id
        db.flush()
    return _req_out(r)


def day_line(db: Session, outlet_id, day) -> str | None:
    """For the nightly summary: how many table calls, and how fast they were answered."""
    # The business day starts at 4am IST (same as bills), so late-night calls count for the evening they belong to.
    start = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone(timedelta(hours=5, minutes=30))) + timedelta(hours=4)
    rows = db.execute(select(TableRequest.created_at, TableRequest.done_at).where(
        TableRequest.outlet_id == outlet_id, TableRequest.created_at >= start, TableRequest.created_at < start + timedelta(days=1))).all()
    if not rows:
        return None
    waits = sorted((d - c).total_seconds() for c, d in rows if d)
    avg = sum(waits) / len(waits) if waits else 0
    slow = sum(1 for w in waits if w > 300)
    return (f"   Table calls: {len(rows)}, answered in {int(avg // 60)}m {int(avg % 60):02d}s on average"
            + (f" ({slow} took over 5 min)" if slow else ""))
