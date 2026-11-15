"""wise-plus stock extras, from the Shakewallah stock app: transfers from the central kitchen, surprise
spot checks, menu profit and trends, and the WhatsApp nightly summary switch."""
from __future__ import annotations

import random
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.kds import business_date
from ..core.permissions import Perm
from ..core.stock import recipe_cost, rolled_cost_k, value_paise
from ..deps import Auth, ensure_outlet_access, get_db, require
from ..models import Bill, BillLine, Outlet, Purchase, StockCount, StockItem, StockLevel, StockMove, User
from ..routers.common import get_or_404
from ..services import audit
from ..services import stock as st
from .models import StockTransfer, WaAlert

router = APIRouter(prefix="/inventory", tags=["inventory"])
alerts = APIRouter(prefix="/whatsapp-alerts", tags=["whatsapp"])


# ------------------------------------------------------------------ transfers

class XferLineIn(BaseModel):
    stock_item_id: uuid.UUID
    qty_m: int = Field(gt=0)


class XferIn(BaseModel):
    from_outlet_id: uuid.UUID
    to_outlet_id: uuid.UUID
    lines: list[XferLineIn] = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=200)


class ReceiveLineIn(BaseModel):
    stock_item_id: uuid.UUID
    received_m: int = Field(ge=0)


class ReceiveIn(BaseModel):
    lines: list[ReceiveLineIn] | None = None     # left out = everything arrived as sent


def _xfer_out(db: Session, t: StockTransfer, show_cost: bool, names: dict) -> dict:
    lines = []
    for ln in t.lines:
        row = {k: ln[k] for k in ("stock_item_id", "name", "unit", "sent_m")}
        if "received_m" in ln:
            row["received_m"] = ln["received_m"]
            row["short_m"] = ln["sent_m"] - ln["received_m"]
        if show_cost:
            row["value_paise"] = value_paise(ln["sent_m"], ln["cost_k"])
            if row.get("short_m"):
                row["short_paise"] = value_paise(row["short_m"], ln["cost_k"])
        lines.append(row)
    out = {"id": str(t.id), "from_outlet_id": str(t.from_outlet_id), "to_outlet_id": str(t.to_outlet_id),
           "from": names.get(t.from_outlet_id, "?"), "to": names.get(t.to_outlet_id, "?"), "date": str(t.business_date),
           "status": t.status, "note": t.note, "lines": lines,
           "received_at": t.received_at.isoformat() if t.received_at else None}
    if show_cost:
        out["value_paise"] = sum(x["value_paise"] for x in lines)
        out["short_paise"] = sum(x.get("short_paise", 0) for x in lines)
    return out


def _outlet_names(db: Session) -> dict:
    return {o.id: o.name for o in db.scalars(select(Outlet))}


@router.post("/transfers", status_code=201)
def send_transfer(body: XferIn, auth: Auth = Depends(require(Perm.inventory_manage)), db: Session = Depends(get_db)) -> dict:
    """Send stock to another outlet: it leaves this outlet now, and reaches the other one when they receive it."""
    if body.from_outlet_id == body.to_outlet_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pick a different outlet to send to")
    ensure_outlet_access(db, auth, body.from_outlet_id)
    get_or_404(db, Outlet, body.to_outlet_id, "Outlet")
    tid = uuid.uuid4()
    lines, seen = [], set()
    for ln in sorted(body.lines, key=lambda x: str(x.stock_item_id)):
        if ln.stock_item_id in seen:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "An item is on the transfer twice")
        seen.add(ln.stock_item_id)
        it = get_or_404(db, StockItem, ln.stock_item_id, "Stock item")
        if it.made_on_sale:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{it.name} is made fresh when sold, so it has no stock to send")
        mv = st.post(db, tenant_id=auth.tenant_id, outlet_id=body.from_outlet_id, item=it, qty_m=-ln.qty_m, kind="xfer_out",
                     ref_kind="transfer", ref_id=tid, user_id=auth.user_id)
        lines.append({"stock_item_id": str(it.id), "name": it.name, "unit": it.unit, "sent_m": ln.qty_m, "cost_k": mv.cost_k})
    t = StockTransfer(id=tid, tenant_id=auth.tenant_id, from_outlet_id=body.from_outlet_id, to_outlet_id=body.to_outlet_id,
                      business_date=business_date(), lines=lines, note=(body.note or "").strip() or None, created_by=auth.user_id)
    db.add(t)
    db.flush()
    audit.record(db, auth.tenant_id, auth.user_id, "create", "transfer", t.id)
    return _xfer_out(db, t, auth.can(Perm.costing_view), _outlet_names(db))


@router.get("/transfers")
def list_transfers(outlet_id: uuid.UUID, auth: Auth = Depends(require(Perm.inventory_manage)), db: Session = Depends(get_db)) -> list[dict]:
    """Transfers sent from or to this outlet; ones still on the way first."""
    ensure_outlet_access(db, auth, outlet_id)
    names = _outlet_names(db)
    rows = db.scalars(select(StockTransfer).where((StockTransfer.from_outlet_id == outlet_id) | (StockTransfer.to_outlet_id == outlet_id))
                      .order_by(StockTransfer.created_at.desc()).limit(100))
    show = auth.can(Perm.costing_view)
    out = [_xfer_out(db, t, show, names) | {"incoming": t.to_outlet_id == outlet_id} for t in rows]
    return sorted(out, key=lambda x: (not (x["status"] == "sent" and x["incoming"]), x["status"] != "sent"))


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: uuid.UUID, body: ReceiveIn, auth: Auth = Depends(require(Perm.inventory_manage)),
                     db: Session = Depends(get_db)) -> dict:
    t = db.scalar(select(StockTransfer).where(StockTransfer.id == transfer_id).with_for_update())
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    ensure_outlet_access(db, auth, t.to_outlet_id)
    if t.status != "sent":
        raise HTTPException(status.HTTP_409_CONFLICT, f"This transfer is already {t.status}")
    got = {str(x.stock_item_id): x.received_m for x in body.lines or []}
    lines = []
    for ln in t.lines:
        rec = got.get(ln["stock_item_id"], ln["sent_m"])
        if rec > ln["sent_m"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{ln['name']}: more received than was sent")
        it = db.get(StockItem, uuid.UUID(ln["stock_item_id"]))
        if rec and it:
            st.post(db, tenant_id=t.tenant_id, outlet_id=t.to_outlet_id, item=it, qty_m=rec, kind="xfer_in", ref_kind="transfer",
                    ref_id=t.id, user_id=auth.user_id, in_cost_k=ln["cost_k"])
        lines.append({**ln, "received_m": rec})
    t.lines = lines
    t.status, t.received_by, t.received_at = "received", auth.user_id, datetime.now(timezone.utc)
    db.flush()
    audit.record(db, auth.tenant_id, auth.user_id, "receive", "transfer", t.id)
    return _xfer_out(db, t, auth.can(Perm.costing_view), _outlet_names(db))


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(transfer_id: uuid.UUID, auth: Auth = Depends(require(Perm.inventory_manage)), db: Session = Depends(get_db)) -> dict:
    """Not received yet: the stock goes back to the outlet that sent it."""
    t = db.scalar(select(StockTransfer).where(StockTransfer.id == transfer_id).with_for_update())
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    ensure_outlet_access(db, auth, t.from_outlet_id)
    if t.status != "sent":
        raise HTTPException(status.HTTP_409_CONFLICT, f"This transfer is already {t.status}")
    for ln in t.lines:
        it = db.get(StockItem, uuid.UUID(ln["stock_item_id"]))
        if it:
            st.post(db, tenant_id=t.tenant_id, outlet_id=t.from_outlet_id, item=it, qty_m=ln["sent_m"], kind="xfer_in", ref_kind="transfer",
                    ref_id=t.id, user_id=auth.user_id, in_cost_k=ln["cost_k"], note="transfer cancelled")
    t.status = "cancelled"
    db.flush()
    audit.record(db, auth.tenant_id, auth.user_id, "cancel", "transfer", t.id)
    return _xfer_out(db, t, auth.can(Perm.costing_view), _outlet_names(db))


# ------------------------------------------------------------------ surprise spot checks

def _used_value(db: Session, outlet_id: uuid.UUID, since: date) -> dict[uuid.UUID, int]:
    return {i: -int(v) for i, v in db.execute(select(StockMove.stock_item_id, func.sum(StockMove.value_paise)).where(
        StockMove.outlet_id == outlet_id, StockMove.business_date >= since,
        StockMove.kind.in_(["sale", "prep_out", "waste", "xfer_out"])).group_by(StockMove.stock_item_id)) if v and v < 0}


@router.get("/spot-check")
def pick_spot_check(outlet_id: uuid.UUID, n: int = 5, auth: Auth = Depends(require(Perm.stock_count)), db: Session = Depends(get_db)) -> dict:
    """A few items to count right now, picked at random. Costly and fast-moving items come up more often.
    Nobody is told beforehand which ones; the expected stock is never shown to the person counting."""
    ensure_outlet_access(db, auth, outlet_id)
    n = max(1, min(n, 15))
    used = _used_value(db, outlet_id, business_date() - timedelta(days=14))
    items = list(db.scalars(select(StockItem).where(StockItem.is_active.is_(True), StockItem.made_on_sale.is_(False))))
    stocked = set(db.scalars(select(StockLevel.stock_item_id).where(StockLevel.outlet_id == outlet_id)))
    pool = [it for it in items if it.id in used or it.id in stocked]
    picked: list[StockItem] = []
    rng = random.SystemRandom()
    while pool and len(picked) < n:
        weights = [1 + used.get(it.id, 0) ** 0.5 for it in pool]
        it = rng.choices(pool, weights=weights)[0]
        picked.append(it)
        pool.remove(it)
    return {"items": [{"id": str(it.id), "name": it.name, "unit": it.unit, "category": it.category, "locations": it.locations or []}
                      for it in sorted(picked, key=lambda x: x.name)]}


@router.get("/spot-checks")
def spot_checks(outlet_id: uuid.UUID, auth: Auth = Depends(require(Perm.inventory_manage)), db: Session = Depends(get_db)) -> list[dict]:
    """Past spot checks: what was expected, what was found."""
    ensure_outlet_access(db, auth, outlet_id)
    show = auth.can(Perm.costing_view)
    names = {u.id: u.name for u in db.scalars(select(User))}
    out = []
    for c in db.scalars(select(StockCount).where(StockCount.outlet_id == outlet_id, StockCount.kind == "spot")
                        .order_by(StockCount.created_at.desc()).limit(30)):
        lines = [{k: v for k, v in ln.items() if show or k != "value_paise"} for ln in c.lines]
        row = {"id": str(c.id), "date": str(c.business_date), "at": c.created_at.isoformat() if c.created_at else None,
               "by": names.get(c.counted_by, "?"), "lines": lines, "off": sum(1 for ln in c.lines if ln.get("diff_m"))}
        if show:
            row["short_paise"] = -sum(ln.get("value_paise", 0) for ln in c.lines if ln.get("diff_m", 0) < 0)
        out.append(row)
    return out


# ------------------------------------------------------------------ menu profit and trends

def _unit_costs(db: Session, outlet_id: uuid.UUID) -> dict[str, int]:
    costs: dict[str, int] = {str(i.id): i.last_cost_k for i in db.scalars(select(StockItem))}
    for lv in db.scalars(select(StockLevel).where(StockLevel.outlet_id == outlet_id)):
        if lv.cost_k:
            costs[str(lv.stock_item_id)] = lv.cost_k
    book, yields, on_sale = st.prep_book(db)
    for sid in book:
        if sid in on_sale or not costs.get(sid):
            costs[sid] = rolled_cost_k(sid, book, yields, {k: v for k, v in costs.items() if k not in on_sale})
    return costs


def _range(d1: date | None, d2: date | None, days: int = 30) -> tuple[date, date]:
    d2 = d2 or business_date()
    return d1 or d2 - timedelta(days=days - 1), d2


@router.get("/menu-profit")
def menu_profit(outlet_id: uuid.UUID, d1: date | None = None, d2: date | None = None,
                auth: Auth = Depends(require(Perm.costing_view)), db: Session = Depends(get_db)) -> dict:
    """Each item size sold in the period: how many, sales before GST, recipe cost at today's costs, profit.
    Shows which items earn the money, and which sell a lot but earn little."""
    ensure_outlet_access(db, auth, outlet_id)
    d1, d2 = _range(d1, d2)
    rows = db.execute(select(BillLine.variant_id, BillLine.item_name, BillLine.variant_name, func.sum(BillLine.qty),
                             func.sum(BillLine.taxable_paise)).join(Bill, Bill.id == BillLine.bill_id).where(
        Bill.outlet_id == outlet_id, Bill.status == "paid", Bill.business_date >= d1, Bill.business_date <= d2)
        .group_by(BillLine.variant_id, BillLine.item_name, BillLine.variant_name)).all()
    costs = _unit_costs(db, outlet_id)
    rec = st.recipes(db, "variant", {r[0] for r in rows})
    merged: dict = {}
    for vid, item, var, qty, sales in rows:          # same size renamed over time: one row
        m = merged.setdefault(vid, {"variant_id": str(vid), "item": item, "variant": var, "qty": 0, "sales_paise": 0})
        m["qty"] += int(qty or 0)
        m["sales_paise"] += int(sales or 0)
    out, tot = [], {"qty": 0, "sales_paise": 0, "cost_paise": 0, "profit_paise": 0, "no_recipe_sales_paise": 0}
    for m in merged.values():
        r = rec.get(uuid.UUID(m["variant_id"]), [])
        unit = recipe_cost(r, costs) if r else None
        m["unit_cost_paise"] = unit
        m["cost_paise"] = unit * m["qty"] if unit is not None else None
        m["profit_paise"] = m["sales_paise"] - m["cost_paise"] if unit is not None else None
        m["margin_bp"] = (m["profit_paise"] * 10000 // m["sales_paise"]) if unit is not None and m["sales_paise"] else None
        tot["qty"] += m["qty"]
        tot["sales_paise"] += m["sales_paise"]
        if unit is None:
            tot["no_recipe_sales_paise"] += m["sales_paise"]
        else:
            tot["cost_paise"] += m["cost_paise"]
            tot["profit_paise"] += m["profit_paise"]
        out.append(m)
    out.sort(key=lambda m: (m["profit_paise"] is None, -(m["profit_paise"] or 0), -m["sales_paise"]))
    return {"from": str(d1), "to": str(d2), "items": out, "total": tot}


@router.get("/trends")
def trends(outlet_id: uuid.UUID, days: int = 30, auth: Auth = Depends(require(Perm.costing_view)), db: Session = Depends(get_db)) -> dict:
    """Day by day: sales, cost of sales (food cost %), wastage, count differences and purchases;
    and the stock items whose use changed most this week against last week."""
    ensure_outlet_access(db, auth, outlet_id)
    days = max(7, min(days, 120))
    d1, d2 = _range(None, None, days)
    sales = dict(db.execute(select(Bill.business_date, func.sum(Bill.taxable_paise)).where(
        Bill.outlet_id == outlet_id, Bill.status == "paid", Bill.business_date >= d1).group_by(Bill.business_date)).all())
    bought = dict(db.execute(select(Purchase.business_date, func.sum(Purchase.amount_paise)).where(
        Purchase.outlet_id == outlet_id, Purchase.status == "posted", Purchase.business_date >= d1).group_by(Purchase.business_date)).all())
    mv: dict = defaultdict(dict)
    for d, kind, v in db.execute(select(StockMove.business_date, StockMove.kind, func.sum(StockMove.value_paise)).where(
            StockMove.outlet_id == outlet_id, StockMove.business_date >= d1).group_by(StockMove.business_date, StockMove.kind)):
        mv[d][kind] = int(v or 0)
    series = []
    for k in range(days):
        d = d1 + timedelta(days=k)
        s, cost = int(sales.get(d) or 0), -mv[d].get("sale", 0)
        series.append({"date": str(d), "sales_paise": s, "cost_paise": cost, "food_cost_bp": cost * 10000 // s if s and cost else None,
                       "waste_paise": -mv[d].get("waste", 0), "count_diff_paise": mv[d].get("count", 0),
                       "purchases_paise": int(bought.get(d) or 0)})
    this = _used_value(db, outlet_id, d2 - timedelta(days=6))
    both = _used_value(db, outlet_id, d2 - timedelta(days=13))
    names = {i.id: i.name for i in db.scalars(select(StockItem))}
    movers = []
    for iid, v in both.items():
        now, before = this.get(iid, 0), v - this.get(iid, 0)
        movers.append({"stock_item_id": str(iid), "name": names.get(iid, "?"), "this_week_paise": now, "last_week_paise": before,
                       "change_bp": (now - before) * 10000 // before if before else None})
    movers.sort(key=lambda x: -abs(x["this_week_paise"] - x["last_week_paise"]))
    return {"days": series, "movers": movers[:12]}


# ------------------------------------------------------------------ nightly summary on WhatsApp

class AlertsIn(BaseModel):
    nightly: bool


@alerts.get("/me")
def my_alerts(auth: Auth = Depends(require(Perm.reports_view)), db: Session = Depends(get_db)) -> dict:
    u = db.get(User, auth.user_id)
    on = bool(db.scalar(select(WaAlert.id).where(WaAlert.user_id == auth.user_id, WaAlert.topic == "nightly")))
    return {"nightly": on, "phone": u.phone if u else None}


@alerts.put("/me")
def set_my_alerts(body: AlertsIn, auth: Auth = Depends(require(Perm.reports_view)), db: Session = Depends(get_db)) -> dict:
    from .nightly import subscribe
    u = db.get(User, auth.user_id)
    if body.nightly and not (u and u.phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Add your mobile number to your account first")
    subscribe(db, auth.tenant_id, auth.user_id, body.nightly)
    return {"nightly": body.nightly, "phone": u.phone if u else None}
