"""All reports as tables: GET /reports/r/<name>?date_from&date_to&outlet_id[&format=csv].

Every figure comes from paid bills (corrected bills count as corrected), by business date (the day
runs 4 am to 4 am), unless the report says otherwise.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import Select, and_, case, func, literal_column, or_, select
from sqlalchemy.orm import Session

from ..core.permissions import Perm
from ..core.report_table import Col, Table, share_bp, to_csv
from ..deps import live_check, Auth, get_db, require
from ..models import (
    Bill, BillLine, BillRevision, CashMovement, Category, Item, Order, Outlet, Payment, Purchase, Shift, StockItem,
    StockLevel, StockMove, Supplier, User, Variant,
)
from ..core.stock import fmt_qty, value_paise
from .reports import _outlets, _range

router = APIRouter(prefix="/reports", tags=["reports"])
IST = "Asia/Kolkata"
TZ = ZoneInfo(IST)
TYPE = {"dine_in": "Dine-in", "takeaway": "Takeaway", "delivery": "Delivery"}
SOURCE = {"staff": "Counter / captain", "qr": "QR (customer's phone)", "kiosk": "Self-order kiosk"}
MODE = {"cash": "Cash", "upi": "UPI", "card": "Card", "due": "Credit (due)", "complimentary": "Complimentary",
        "online": "Online (Zomato / Swiggy)", "other": "Other", "part": "Part payment (split in old POS)"}
IMPORTED_NOTE = "Includes {n} bills imported from your earlier POS."


class Ctx:
    def __init__(self, db: Session, outlets: list[uuid.UUID], d1: date, d2: date) -> None:
        self.db, self.outlets, self.d1, self.d2 = db, outlets, d1, d2
        self.paid = and_(Bill.outlet_id.in_(outlets), Bill.business_date >= d1, Bill.business_date <= d2,
                         Bill.status == "paid")
        self._names: dict | None = None
        from ..posimport import merge
        self.imp = merge
        self.imported = merge.count(db, outlets, d1, d2)

    def note(self, t: Table) -> Table:
        if self.imported:
            extra = IMPORTED_NOTE.format(n=f"{self.imported:,}")
            t.note = f"{t.note} {extra}" if getattr(t, "note", None) else extra
        return t

    def names(self) -> dict:
        if self._names is None:
            self._names = {u.id: u.name for u in self.db.scalars(select(User))}
        return self._names

    def rows(self, stmt: Select):
        return self.db.execute(stmt).all()


REPORTS: dict[str, tuple[str, str, Callable[[Ctx], Table]]] = {}


def report(name: str, group: str, label: str):
    def wrap(fn: Callable[[Ctx], Table]):
        REPORTS[name] = (group, label, fn)
        return fn
    return wrap


def ist(col):
    return func.timezone(IST, col)


# ------------------------------------------------------------------ sales

@report("days", "Sales", "Day-wise sales")
def days(c: Ctx) -> Table:
    t = Table("Day-wise sales", [
        Col("date", "Date", "date"), Col("bills", "Bills", "int", True), Col("gross_paise", "Gross", "money", True),
        Col("discount_paise", "Discount", "money", True), Col("taxable_paise", "Taxable", "money", True),
        Col("tax_paise", "GST", "money", True), Col("round_off_paise", "Round off", "money", True),
        Col("sales_paise", "Net sales", "money", True), Col("avg_paise", "Average bill", "money")])
    for d, n, g, di, tx, gst, ro, s in c.rows(
        select(Bill.business_date, func.count(), func.sum(Bill.gross_paise), func.sum(Bill.discount_paise),
               func.sum(Bill.taxable_paise), func.sum(Bill.cgst_paise + Bill.sgst_paise + Bill.igst_paise),
               func.sum(Bill.round_off_paise), func.sum(Bill.total_paise))
        .where(c.paid).group_by(Bill.business_date).order_by(Bill.business_date)):
        t.rows.append({"date": str(d), "bills": n, "gross_paise": int(g), "discount_paise": int(di),
                       "taxable_paise": int(tx), "tax_paise": int(gst), "round_off_paise": int(ro),
                       "sales_paise": int(s), "avg_paise": int(s) // n})
    if c.imported:
        rows = {r["date"]: r for r in t.rows}
        keys = ("bills", "gross_paise", "discount_paise", "taxable_paise", "tax_paise", "round_off_paise", "sales_paise")
        for d, vals in c.imp.by_day(c.db, c.outlets, c.d1, c.d2).items():
            r = rows.setdefault(str(d), {"date": str(d), **{k: 0 for k in keys}})
            for k, v in zip(keys, vals):
                r[k] += v
        t.rows = sorted(rows.values(), key=lambda r: r["date"])
        for r in t.rows:
            r["avg_paise"] = r["sales_paise"] // r["bills"] if r["bills"] else 0
    return c.note(t)


def _h(hour: int) -> str:
    hour %= 24
    return f"{hour % 12 or 12} {'am' if hour < 12 else 'pm'}"


@report("hours", "Sales", "Hour-wise sales")
def hours(c: Ctx) -> Table:
    t = Table("Hour-wise sales", [
        Col("hour", "Hour"), Col("bills", "Bills", "int", True), Col("sales_paise", "Net sales", "money", True),
        Col("share_bp", "Share of sales", "percent"), Col("avg_paise", "Average bill", "money")],
        note="When customers come in, summed over the days picked. Shows where you need more staff.")
    h = func.extract("hour", ist(Bill.created_at))
    rows = c.rows(select(h, func.count(), func.sum(Bill.total_paise)).where(c.paid).group_by(h).order_by(h))
    whole = sum(int(s) for _, _, s in rows)
    for hr, n, s in rows:
        hr = int(hr)
        t.rows.append({"hour": f"{_h(hr)} – {_h(hr + 1)}",
                       "bills": n, "sales_paise": int(s), "share_bp": share_bp(int(s), whole), "avg_paise": int(s) // n})
    return t


@report("orders", "Sales", "Order-wise (every bill)")
def orders(c: Ctx) -> Table:
    t = Table("Order-wise report", [
        Col("invoice_no", "Bill no."), Col("date", "Date", "date"), Col("time", "Time", "time"),
        Col("token", "Token", "int"), Col("type", "Type"), Col("where", "Table / car"), Col("source", "Taken via"),
        Col("taken_by", "Taken by"), Col("cashier", "Billed by"), Col("items", "Items", "int", True),
        Col("gross_paise", "Gross", "money", True), Col("discount_paise", "Discount", "money", True),
        Col("tax_paise", "GST", "money", True), Col("total_paise", "Total", "money", True),
        Col("paid_by", "Paid by"), Col("customer", "Customer"), Col("note", "Note")],
        note="Corrected bills show their corrected amounts. Cancelled bills are under Cancellations.")
    items = dict(c.rows(select(BillLine.bill_id, func.sum(BillLine.qty)).join(Bill, Bill.id == BillLine.bill_id)
                        .where(c.paid).group_by(BillLine.bill_id)))
    pays: dict = defaultdict(list)
    for bid, mode, amt in c.rows(select(Payment.bill_id, Payment.mode, Payment.amount_paise)
                                 .join(Bill, Bill.id == Payment.bill_id).where(c.paid)):
        pays[bid].append((mode, int(amt)))
    names = c.names()
    for b, o in c.rows(select(Bill, Order).join(Order, Order.id == Bill.order_id).where(c.paid)
                       .order_by(Bill.created_at).limit(20000)):
        p = pays[b.id]
        t.rows.append({
            "invoice_no": b.invoice_no, "date": str(b.business_date),
            "time": b.created_at.astimezone(TZ).strftime("%H:%M") if b.created_at else "",
            "token": o.token_no, "type": TYPE.get(o.order_type, o.order_type),
            "where": o.table_label or o.car_number or "", "source": SOURCE.get(o.source, o.source),
            "taken_by": names.get(o.created_by, "") if o.created_by else "", "cashier": names.get(b.created_by, ""),
            "items": int(items.get(b.id, 0)), "gross_paise": b.gross_paise, "discount_paise": b.discount_paise,
            "tax_paise": b.cgst_paise + b.sgst_paise + b.igst_paise, "total_paise": b.total_paise,
            "paid_by": " + ".join(MODE.get(m, m) for m, _ in p) if len(p) < 2 else
                       " + ".join(f"{MODE.get(m, m)} {a / 100:.0f}" for m, a in p),
            "customer": b.customer_phone or b.customer_name or "",
            "note": "; ".join(x for x in (b.revision > 1 and f"corrected (v{b.revision})",
                                          b.customer_gstin and f"B2B {b.customer_gstin}") if x)})
    return t


@report("order_types", "Sales", "Dine-in / takeaway / delivery")
def order_types(c: Ctx) -> Table:
    t = Table("Order type and how orders came in", [
        Col("type", "Order type"), Col("source", "Taken via"), Col("bills", "Bills", "int", True),
        Col("sales_paise", "Net sales", "money", True), Col("share_bp", "Share", "percent"),
        Col("avg_paise", "Average bill", "money")])
    rows = c.rows(select(Order.order_type, Order.source, func.count(), func.sum(Bill.total_paise))
                  .join(Order, Order.id == Bill.order_id).where(c.paid).group_by(Order.order_type, Order.source)
                  .order_by(func.sum(Bill.total_paise).desc()))
    whole = sum(int(s) for *_, s in rows)
    for ty, src, n, s in rows:
        t.rows.append({"type": TYPE.get(ty, ty), "source": SOURCE.get(src, src), "bills": n, "sales_paise": int(s),
                       "share_bp": share_bp(int(s), whole), "avg_paise": int(s) // n})
    return t


@report("outlets", "Sales", "Outlet-wise sales")
def outlets(c: Ctx) -> Table:
    t = Table("Outlet-wise sales", [
        Col("outlet", "Outlet"), Col("bills", "Bills", "int", True), Col("discount_paise", "Discount", "money", True),
        Col("tax_paise", "GST", "money", True), Col("sales_paise", "Net sales", "money", True),
        Col("avg_paise", "Average bill", "money")])
    for name, n, d, x, s in c.rows(
        select(Outlet.name, func.count(), func.sum(Bill.discount_paise),
               func.sum(Bill.cgst_paise + Bill.sgst_paise + Bill.igst_paise), func.sum(Bill.total_paise))
        .join(Outlet, Outlet.id == Bill.outlet_id).where(c.paid).group_by(Outlet.name)
        .order_by(func.sum(Bill.total_paise).desc())):
        t.rows.append({"outlet": name, "bills": n, "discount_paise": int(d), "tax_paise": int(x),
                       "sales_paise": int(s), "avg_paise": int(s) // n})
    if c.imported:
        names = dict(c.db.execute(select(Outlet.id, Outlet.name)).all())
        rows = {r["outlet"]: r for r in t.rows}
        for oid, (n, d, x, s) in c.imp.by_outlet(c.db, c.outlets, c.d1, c.d2).items():
            r = rows.setdefault(names.get(oid, "?"), {"outlet": names.get(oid, "?"), "bills": 0, "discount_paise": 0, "tax_paise": 0, "sales_paise": 0})
            r["bills"] += n; r["discount_paise"] += d; r["tax_paise"] += x; r["sales_paise"] += s  # noqa: E702
        t.rows = sorted(rows.values(), key=lambda r: -r["sales_paise"])
        for r in t.rows:
            r["avg_paise"] = r["sales_paise"] // r["bills"] if r["bills"] else 0
    return c.note(t)


@report("imported", "Sales", "Bills imported from your earlier POS")
def imported(c: Ctx) -> Table:
    from ..posimport.models import ImportedBill as IB
    t = Table("Bills imported from your earlier POS", [
        Col("date", "Date", "date"), Col("bill_no", "Bill no"), Col("status", "Status"), Col("paid_by", "Paid by"),
        Col("discount_paise", "Discount", "money", True), Col("tax_paise", "GST", "money", True),
        Col("total_paise", "Total", "money", True), Col("customer", "Customer")],
        note="Sales before EMB, brought in from the other POS's export. Settings › Import from another POS.")
    for b in c.db.scalars(select(IB).where(IB.outlet_id.in_(c.outlets), IB.business_date >= c.d1, IB.business_date <= c.d2)
                          .order_by(IB.business_date, IB.billed_at, IB.bill_no).limit(20000)):
        paidc = b.status == "paid"
        t.rows.append({"date": str(b.business_date), "bill_no": b.bill_no, "status": "Paid" if paidc else "Cancelled",
                       "paid_by": " + ".join(MODE.get(m, m) for m in (b.payments or {})),
                       "discount_paise": b.discount_paise if paidc else 0,
                       "tax_paise": b.cgst_paise + b.sgst_paise + b.igst_paise if paidc else 0,
                       "total_paise": b.total_paise if paidc else 0,
                       "customer": " · ".join(x for x in (b.customer_name, b.customer_phone) if x)})
    return t


# ------------------------------------------------------------------ items

@report("items", "Items", "Item-wise sales")
def items(c: Ctx) -> Table:
    t = Table("Item-wise sales", [
        Col("item", "Item"), Col("variant", "Size / variant"), Col("category", "Category"),
        Col("qty", "Qty sold", "int", True), Col("gross_paise", "Gross", "money", True),
        Col("discount_paise", "Discount", "money", True), Col("taxable_paise", "Taxable", "money", True),
        Col("tax_paise", "GST", "money", True), Col("share_bp", "Share of sales", "percent")])
    rows = c.rows(
        select(BillLine.item_name, BillLine.variant_name, Category.name, func.sum(BillLine.qty),
               func.sum(BillLine.gross_paise), func.sum(BillLine.discount_paise), func.sum(BillLine.taxable_paise),
               func.sum(BillLine.cgst_paise + BillLine.sgst_paise + BillLine.igst_paise))
        .join(Bill, Bill.id == BillLine.bill_id).outerjoin(Variant, Variant.id == BillLine.variant_id)
        .outerjoin(Item, Item.id == Variant.item_id).outerjoin(Category, Category.id == Item.category_id)
        .where(c.paid).group_by(BillLine.item_name, BillLine.variant_name, Category.name)
        .order_by(func.sum(BillLine.qty).desc()))
    if c.imported:
        from ..posimport.models import ImportedLine as IL
        merged: dict = {}
        for i, v, cat, q, g, d, tx, x in list(rows) + [tuple(r) for r in c.imp.lines(c.db, c.outlets, c.d1, c.d2, IL.item_name, IL.variant_name, IL.category)]:
            k = (i.strip().lower(), (v or "Regular").strip().lower())
            a = merged.setdefault(k, [i, v or "Regular", cat, 0, 0, 0, 0, 0])
            a[2] = a[2] or cat
            for j, val in enumerate((q, g, d, tx, x), start=3):
                a[j] += int(val or 0)
        rows = sorted(merged.values(), key=lambda a: -a[3])
        c.note(t)
    whole = sum(int(r[4]) - int(r[5]) for r in rows)
    for i, v, cat, q, g, d, tx, x in rows:
        t.rows.append({"item": i, "variant": "" if v == "Regular" else v, "category": cat or "", "qty": int(q),
                       "gross_paise": int(g), "discount_paise": int(d), "taxable_paise": int(tx), "tax_paise": int(x),
                       "share_bp": share_bp(int(g) - int(d), whole)})
    return t


@report("categories", "Items", "Category-wise sales")
def categories(c: Ctx) -> Table:
    t = Table("Category-wise sales", [
        Col("category", "Category"), Col("items", "Different items", "int"), Col("qty", "Qty sold", "int", True),
        Col("sales_paise", "Sales after discount", "money", True), Col("share_bp", "Share", "percent")],
        note="By each item's category today; an item moved to another category counts there.")
    rows = c.rows(
        select(func.coalesce(Category.name, "No category"), func.count(func.distinct(BillLine.item_name)),
               func.sum(BillLine.qty), func.sum(BillLine.gross_paise - BillLine.discount_paise))
        .join(Bill, Bill.id == BillLine.bill_id).outerjoin(Variant, Variant.id == BillLine.variant_id)
        .outerjoin(Item, Item.id == Variant.item_id).outerjoin(Category, Category.id == Item.category_id)
        .where(c.paid).group_by(Category.name).order_by(func.sum(BillLine.gross_paise - BillLine.discount_paise).desc()))
    if c.imported:
        from ..posimport.models import ImportedLine as IL
        merged: dict = {}
        for cat, n, q, sl in rows:
            merged[cat] = [cat, int(n), int(q), int(sl)]
        for cat, q, g, d, _tx, _x in c.imp.lines(c.db, c.outlets, c.d1, c.d2, func.coalesce(IL.category, "No category")):
            a = merged.setdefault(cat, [cat, 0, 0, 0])
            a[2] += int(q); a[3] += int(g) - int(d)  # noqa: E702
        rows = sorted(merged.values(), key=lambda a: -a[3])
        c.note(t)
    whole = sum(int(r[3]) for r in rows)
    for cat, n, q, s in rows:
        t.rows.append({"category": cat, "items": n, "qty": int(q), "sales_paise": int(s), "share_bp": share_bp(int(s), whole)})
    return t


# ------------------------------------------------------------------ payments

@report("payments", "Payments", "Payment-wise summary")
def payments(c: Ctx) -> Table:
    t = Table("Payment-wise summary", [
        Col("mode", "Paid by"), Col("bills", "Bills", "int"), Col("amount_paise", "Amount", "money", True),
        Col("share_bp", "Share", "percent")],
        note="A split bill counts under each way it was paid.")
    rows = c.rows(select(Payment.mode, func.count(func.distinct(Payment.bill_id)), func.sum(Payment.amount_paise))
                  .join(Bill, Bill.id == Payment.bill_id).where(c.paid).group_by(Payment.mode)
                  .order_by(func.sum(Payment.amount_paise).desc()))
    if c.imported:
        merged = {m: [int(n), int(a)] for m, n, a in rows}
        for m, (n, a) in c.imp.by_mode(c.db, c.outlets, c.d1, c.d2).items():
            x = merged.setdefault(m, [0, 0])
            x[0] += n; x[1] += a  # noqa: E702
        rows = sorted(((m, n, a) for m, (n, a) in merged.items()), key=lambda r: -r[2])
        c.note(t)
    whole = sum(int(a) for *_, a in rows)
    for m, n, a in rows:
        t.rows.append({"mode": MODE.get(m, m), "bills": n, "amount_paise": int(a), "share_bp": share_bp(int(a), whole)})
    return t


@report("payments_daily", "Payments", "Payments by day")
def payments_daily(c: Ctx) -> Table:
    modes = ["cash", "upi", "card", "due", "complimentary"]
    t = Table("Payments by day", [Col("date", "Date", "date")] +
              [Col(m, MODE[m], "money", True) for m in modes] + [Col("total", "Total", "money", True)],
              note="Match the cash column with the cash counted at shift close, and UPI/card with your bank.")
    by: dict = defaultdict(lambda: {m: 0 for m in modes})
    for d, m, a in c.rows(select(Bill.business_date, Payment.mode, func.sum(Payment.amount_paise))
                          .join(Bill, Bill.id == Payment.bill_id).where(c.paid)
                          .group_by(Bill.business_date, Payment.mode)):
        by[d][m] = int(a)
    if c.imported:
        for (d, m), a in c.imp.by_mode(c.db, c.outlets, c.d1, c.d2, per_day=True).items():
            if m not in modes:
                modes.append(m)
                t.columns.insert(len(t.columns) - 1, Col(m, MODE.get(m, m), "money", True))
            by[d][m] = by[d].get(m, 0) + a
        c.note(t)
    for d in sorted(by):
        t.rows.append({"date": str(d), **by[d], "total": sum(by[d].values())})
    return t


@report("dues", "Payments", "Credit (due) bills")
def dues(c: Ctx) -> Table:
    t = Table("Credit (due) bills", [
        Col("invoice_no", "Bill no."), Col("date", "Date", "date"), Col("customer", "Customer"),
        Col("phone", "Mobile"), Col("due_paise", "Due", "money", True), Col("total_paise", "Bill total", "money", True)],
        note="Bills given on credit. Settling dues inside Wise comes in a later version; until then this is the list to follow up.")
    for b, due in c.rows(select(Bill, func.sum(Payment.amount_paise)).join(Payment, Payment.bill_id == Bill.id)
                         .where(c.paid, Payment.mode == "due").group_by(Bill.id).order_by(Bill.created_at)):
        t.rows.append({"invoice_no": b.invoice_no, "date": str(b.business_date), "customer": b.customer_name or "",
                       "phone": b.customer_phone or "", "due_paise": int(due), "total_paise": b.total_paise})
    return t


# ------------------------------------------------------------------ staff and shifts

def _cash_moves(c: Ctx):
    """Cash in / out of every drawer in the dates, oldest first, with outlet, drawer and cashier."""
    from ..models import Drawer
    day = func.date(ist(CashMovement.created_at))
    return c.rows(select(CashMovement, Shift.outlet_id, Shift.drawer_id).join(Shift, Shift.id == CashMovement.shift_id)
                  .where(Shift.outlet_id.in_(c.outlets), day >= c.d1, day <= c.d2).order_by(CashMovement.created_at)), \
        {d.id: d.name for d in c.db.scalars(select(Drawer))}, {o.id: o.name for o in c.db.scalars(select(Outlet))}


@report("expenses", "Payments", "Expenses and drawings (cash out)")
def expenses(c: Ctx) -> Table:
    t = Table("Expenses and drawings", [
        Col("at", "When", "datetime"), Col("outlet", "Outlet"), Col("drawer", "Drawer"), Col("cashier", "Cashier"),
        Col("type", "Type"), Col("detail", "Category / given to"), Col("note", "Note"), Col("amount_paise", "Amount", "money", True)],
        note="Every cash taken out of a drawer. Drawings are cash handed to the owner or deposited in the bank by the cashier. "
             "The summary report shows totals by category.")
    from ..core.cash_purpose import type_label
    rows, drawers, outlets = _cash_moves(c)
    names = c.names()
    for m, oid, did in rows:
        if m.kind != "out":
            continue
        t.rows.append({"at": m.created_at.isoformat(), "outlet": outlets.get(oid, ""), "drawer": drawers.get(did, ""),
                       "cashier": names.get(m.created_by, "?"), "type": type_label(m.kind, m.purpose, m.handed),
                       "detail": (m.category if m.purpose == "expense" else m.paid_to) or "",
                       "note": (m.note or "") if m.purpose else m.reason, "amount_paise": m.amount_paise})
    return t


@report("cash_summary", "Payments", "Cash out summary (by category)")
def cash_summary(c: Ctx) -> Table:
    t = Table("Cash out and in by type", [
        Col("type", "Type"), Col("detail", "Category / given to"), Col("count", "Times", "int", True),
        Col("out_paise", "Taken out", "money", True), Col("in_paise", "Put in", "money", True)],
        note="Expenses by category, drawings by who took them or which bank, and cash put into drawers.")
    from ..core.cash_purpose import type_label
    rows, _, _ = _cash_moves(c)
    agg: dict[tuple[str, str], list[int]] = {}
    for m, _o, _d in rows:
        key = (type_label(m.kind, m.purpose, m.handed), (m.category if m.purpose == "expense" else m.paid_to) or "")
        a = agg.setdefault(key, [0, 0, 0])
        a[0] += 1
        a[1 if m.kind == "out" else 2] += m.amount_paise
    for (ty, de), (n, o, i) in sorted(agg.items(), key=lambda kv: (-kv[1][1], kv[0])):
        t.rows.append({"type": ty, "detail": de, "count": n, "out_paise": o, "in_paise": i})
    return t


@report("captains", "Staff", "Captain-wise (who took the order)")
def captains(c: Ctx) -> Table:
    t = Table("Captain-wise sales", [
        Col("name", "Taken by"), Col("role", "Role"), Col("orders", "Orders", "int", True),
        Col("sales_paise", "Net sales", "money", True), Col("avg_paise", "Average order", "money")])
    for n, r, k, s in c.rows(
        select(User.name, User.role, func.count(), func.sum(Bill.total_paise))
        .select_from(Bill).join(Order, Order.id == Bill.order_id).outerjoin(User, User.id == Order.created_by)
        .where(c.paid).group_by(User.name, User.role).order_by(func.sum(Bill.total_paise).desc())):
        t.rows.append({"name": n or "Customer (QR / kiosk)", "role": (r or "").capitalize(), "orders": k,
                       "sales_paise": int(s), "avg_paise": int(s) // k})
    return t


@report("cashiers", "Staff", "Cashier-wise (who billed)")
def cashiers(c: Ctx) -> Table:
    t = Table("Cashier-wise billing", [
        Col("name", "Billed by"), Col("bills", "Bills", "int", True), Col("discount_paise", "Discounts given", "money", True),
        Col("sales_paise", "Net sales", "money", True), Col("cash_paise", "Of which cash", "money", True)])
    cash = dict(c.rows(select(Bill.created_by, func.sum(Payment.amount_paise)).join(Payment, Payment.bill_id == Bill.id)
                       .where(c.paid, Payment.mode == "cash").group_by(Bill.created_by)))
    names = c.names()
    for uid, n, d, s in c.rows(select(Bill.created_by, func.count(), func.sum(Bill.discount_paise), func.sum(Bill.total_paise))
                               .where(c.paid).group_by(Bill.created_by).order_by(func.sum(Bill.total_paise).desc())):
        t.rows.append({"name": names.get(uid, "?"), "bills": n, "discount_paise": int(d), "sales_paise": int(s),
                       "cash_paise": int(cash.get(uid, 0))})
    return t


@report("tables", "Staff", "Table-wise sales")
def tables(c: Ctx) -> Table:
    t = Table("Table-wise sales", [
        Col("table", "Table"), Col("orders", "Orders", "int", True), Col("sales_paise", "Net sales", "money", True),
        Col("avg_paise", "Average order", "money")])
    for tb, n, s in c.rows(select(Order.table_label, func.count(), func.sum(Bill.total_paise))
                           .join(Order, Order.id == Bill.order_id).where(c.paid, Order.table_label.is_not(None))
                           .group_by(Order.table_label).order_by(func.sum(Bill.total_paise).desc())):
        t.rows.append({"table": tb, "orders": n, "sales_paise": int(s), "avg_paise": int(s) // n})
    return t


@report("shifts", "Staff", "Shifts and cash")
def shifts(c: Ctx) -> Table:
    t = Table("Shifts and cash", [
        Col("cashier", "Cashier"), Col("opened", "Opened", "datetime"), Col("closed", "Closed", "datetime"),
        Col("bills", "Bills", "int", True), Col("sales_paise", "Sales", "money", True),
        Col("carried_paise", "Left by last close", "money"), Col("opening_paise", "Opening count", "money"),
        Col("opening_diff_paise", "Opening short / extra", "money", True), Col("cash_sales_paise", "Cash sales", "money", True),
        Col("cash_in_paise", "Cash in", "money", True), Col("cash_out_paise", "Cash out", "money", True),
        Col("expected_paise", "Expected in drawer", "money"), Col("counted_paise", "Counted", "money"),
        Col("variance_paise", "Short / excess", "money", True)],
        note="Each shift opens with what the last close left in the drawer, plus cash sales and cash in, minus cash out "
             "and expenses. Short/excess = counted minus expected (negative means cash short).")
    names = c.names()
    day = func.date(ist(Shift.opened_at))
    for s in c.db.scalars(select(Shift).where(Shift.outlet_id.in_(c.outlets), day >= c.d1, day <= c.d2)
                          .order_by(Shift.opened_at)):
        bills = c.rows(select(func.count(), func.coalesce(func.sum(Bill.total_paise), 0))
                       .where(Bill.shift_id == s.id, Bill.status == "paid"))[0]
        cash = c.db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0)).join(Bill, Bill.id == Payment.bill_id)
                           .where(Bill.shift_id == s.id, Bill.status == "paid", Payment.mode == "cash")) or 0
        mv = dict(c.rows(select(CashMovement.kind, func.sum(CashMovement.amount_paise))
                         .where(CashMovement.shift_id == s.id).group_by(CashMovement.kind)))
        expected = s.expected_cash_paise if s.expected_cash_paise is not None else \
            s.opening_cash_paise + int(cash) + int(mv.get("in", 0)) - int(mv.get("out", 0))
        t.rows.append({
            "cashier": names.get(s.opened_by, "?"), "opened": s.opened_at.isoformat(),
            "closed": s.closed_at.isoformat() if s.closed_at else "still open", "bills": bills[0],
            "sales_paise": int(bills[1]), "carried_paise": s.expected_opening_paise, "opening_paise": s.opening_cash_paise,
            "opening_diff_paise": None if s.expected_opening_paise is None else s.opening_cash_paise - s.expected_opening_paise, "cash_sales_paise": int(cash),
            "cash_in_paise": int(mv.get("in", 0)), "cash_out_paise": int(mv.get("out", 0)), "expected_paise": expected,
            "counted_paise": s.counted_cash_paise,
            "variance_paise": None if s.counted_cash_paise is None else s.counted_cash_paise - expected})
    return t


# ------------------------------------------------------------------ control

@report("discounts", "Control", "Discounts and complimentary")
def discounts(c: Ctx) -> Table:
    t = Table("Discounts and complimentary bills", [
        Col("invoice_no", "Bill no."), Col("date", "Date", "date"), Col("cashier", "Billed by"),
        Col("approved_by", "Approved by"), Col("gross_paise", "Gross", "money", True),
        Col("discount_paise", "Discount", "money", True), Col("discount_bp", "Discount %", "percent"),
        Col("comp_paise", "Complimentary", "money", True), Col("reason", "Reason")])
    comp = dict(c.rows(select(Payment.bill_id, func.sum(Payment.amount_paise)).join(Bill, Bill.id == Payment.bill_id)
                       .where(c.paid, Payment.mode == "complimentary").group_by(Payment.bill_id)))
    names = c.names()
    for b in c.db.scalars(select(Bill).where(c.paid, or_(Bill.discount_paise > 0, Bill.id.in_(list(comp))))
                          .order_by(Bill.created_at)):
        t.rows.append({"invoice_no": b.invoice_no, "date": str(b.business_date), "cashier": names.get(b.created_by, ""),
                       "approved_by": names.get(b.approved_by, "") if b.approved_by else "",
                       "gross_paise": b.gross_paise, "discount_paise": b.discount_paise,
                       "discount_bp": share_bp(b.discount_paise, b.gross_paise), "comp_paise": int(comp.get(b.id, 0)),
                       "reason": b.discount_reason or ("Complimentary" if b.id in comp else "")})
    return t


@report("cancellations", "Control", "Cancelled bills and orders")
def cancellations(c: Ctx) -> Table:
    t = Table("Cancelled bills and orders", [
        Col("what", "What"), Col("ref", "Bill / token"), Col("date", "Date", "date"),
        Col("amount_paise", "Amount", "money", True), Col("reason", "Reason"), Col("by", "Cancelled by"),
        Col("approved_by", "Approved by"), Col("at", "When", "datetime")])
    names = c.names()
    base = [Bill.outlet_id.in_(c.outlets), Bill.business_date >= c.d1, Bill.business_date <= c.d2]
    for b in c.db.scalars(select(Bill).where(*base, Bill.status == "cancelled")):
        t.rows.append({"what": "Bill", "ref": b.invoice_no, "date": str(b.business_date), "amount_paise": b.total_paise,
                       "reason": b.cancel_reason or "", "by": names.get(b.cancelled_by, "") if b.cancelled_by else "",
                       "approved_by": names.get(b.cancel_approved_by, "") if b.cancel_approved_by else "",
                       "at": b.cancelled_at.isoformat() if b.cancelled_at else ""})
    for o in c.db.scalars(select(Order).where(Order.outlet_id.in_(c.outlets), Order.business_date >= c.d1,
                                              Order.business_date <= c.d2, Order.status == "cancelled")):
        rejected = o.source in ("qr", "kiosk") and o.token_no is None
        t.rows.append({"what": "QR order turned down" if rejected else "Order (before billing)",
                       "ref": f"Token {o.token_no}" if o.token_no else "QR order", "date": str(o.business_date),
                       "amount_paise": None, "reason": o.cancel_reason or "",
                       "by": names.get(o.cancelled_by, "") if o.cancelled_by else "",
                       "approved_by": names.get(o.cancel_approved_by, "") if o.cancel_approved_by else "",
                       "at": o.cancelled_at.isoformat() if o.cancelled_at else ""})
    t.rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return t


@report("bill_edits", "Control", "Bill corrections")
def bill_edits(c: Ctx) -> Table:
    t = Table("Bill corrections", [
        Col("invoice_no", "Bill no."), Col("bill_date", "Bill date", "date"), Col("at", "Corrected", "datetime"),
        Col("changes", "Changed"), Col("old_paise", "Before", "money", True), Col("new_paise", "After", "money", True),
        Col("diff_paise", "Difference", "money", True), Col("reason", "Reason"), Col("by", "By"),
        Col("approved_by", "Approved by")],
        note="Corrections made in the period, by when they were made. Sales reports use the corrected bills.")
    names = c.names()
    day = func.date(ist(BillRevision.created_at))
    for r, inv, d in c.rows(select(BillRevision, Bill.invoice_no, Bill.business_date)
                            .join(Bill, Bill.id == BillRevision.bill_id)
                            .where(Bill.outlet_id.in_(c.outlets), day >= c.d1, day <= c.d2)
                            .order_by(BillRevision.created_at.desc())):
        t.rows.append({"invoice_no": inv, "bill_date": str(d), "at": r.created_at.isoformat(),
                       "changes": ", ".join(r.changes), "old_paise": r.old_total_paise, "new_paise": r.new_total_paise,
                       "diff_paise": r.new_total_paise - r.old_total_paise, "reason": r.reason,
                       "by": names.get(r.edited_by, ""), "approved_by": names.get(r.approved_by, "") if r.approved_by else ""})
    return t


# ------------------------------------------------------------------ GST

@report("gst_rates", "GST", "GST by rate")
def gst_rates(c: Ctx) -> Table:
    t = Table("GST by rate", [
        Col("rate", "GST rate"), Col("taxable_paise", "Taxable value", "money", True),
        Col("cgst_paise", "CGST", "money", True), Col("sgst_paise", "SGST", "money", True),
        Col("igst_paise", "IGST", "money", True), Col("tax_paise", "Total GST", "money", True)])
    for r, tx, cg, sg, ig in c.rows(
        select(BillLine.rate_bp, func.sum(BillLine.taxable_paise), func.sum(BillLine.cgst_paise),
               func.sum(BillLine.sgst_paise), func.sum(BillLine.igst_paise))
        .join(Bill, Bill.id == BillLine.bill_id).where(c.paid).group_by(BillLine.rate_bp).order_by(BillLine.rate_bp)):
        t.rows.append({"rate": f"{int(r) / 100:g}%", "rate_bp": int(r), "taxable_paise": int(tx), "cgst_paise": int(cg),
                       "sgst_paise": int(sg), "igst_paise": int(ig), "tax_paise": int(cg) + int(sg) + int(ig)})
    if c.imported:
        rows = {r["rate_bp"]: r for r in t.rows}
        for rate, (tx, tax) in c.imp.by_rate(c.db, c.outlets, c.d1, c.d2).items():
            r = rows.setdefault(rate, {"rate": f"{rate / 100:g}%", "rate_bp": rate, "taxable_paise": 0, "cgst_paise": 0,
                                       "sgst_paise": 0, "igst_paise": 0, "tax_paise": 0})
            r["taxable_paise"] += tx; r["cgst_paise"] += tax // 2; r["sgst_paise"] += tax - tax // 2; r["tax_paise"] += tax  # noqa: E702
        t.rows = [rows[k] for k in sorted(rows)]
        c.note(t)
    for r in t.rows:
        r.pop("rate_bp", None)
    return t


@report("hsn", "GST", "HSN / SAC summary (GSTR-1)")
def hsn(c: Ctx) -> Table:
    t = Table("HSN / SAC summary", [
        Col("hsn", "HSN / SAC"), Col("rate", "Rate"), Col("qty", "Quantity", "int", True),
        Col("value_paise", "Total value", "money", True), Col("taxable_paise", "Taxable value", "money", True),
        Col("cgst_paise", "CGST", "money", True), Col("sgst_paise", "SGST", "money", True),
        Col("igst_paise", "IGST", "money", True)],
        note="For the HSN table of GSTR-1. Restaurant service is usually SAC 9963; give your CA this sheet.")
    # One expression, with the default written into the SQL: separate bound "9963"s make Postgres treat the
    # SELECT and GROUP BY as different columns and refuse the query.
    code = func.coalesce(BillLine.hsn_sac, literal_column("'9963'"))
    for h, r, q, tx, cg, sg, ig in c.rows(
        select(code, BillLine.rate_bp, func.sum(BillLine.qty),
               func.sum(BillLine.taxable_paise), func.sum(BillLine.cgst_paise), func.sum(BillLine.sgst_paise),
               func.sum(BillLine.igst_paise))
        .join(Bill, Bill.id == BillLine.bill_id).where(c.paid)
        .group_by(code, BillLine.rate_bp).order_by(code)):
        t.rows.append({"hsn": h, "rate": f"{int(r) / 100:g}%", "qty": int(q),
                       "value_paise": int(tx) + int(cg) + int(sg) + int(ig), "taxable_paise": int(tx),
                       "cgst_paise": int(cg), "sgst_paise": int(sg), "igst_paise": int(ig)})
    if c.imported:
        from ..posimport.models import ImportedLine as IL
        rows = {(r["hsn"], r["rate"]): r for r in t.rows}
        for h, r, q, _g, _d, tx, tax in c.imp.lines(c.db, c.outlets, c.d1, c.d2, func.coalesce(IL.hsn_sac, literal_column("'9963'")), IL.rate_bp):
            k = (h, f"{int(r) / 100:g}%")
            row = rows.setdefault(k, {"hsn": h, "rate": k[1], "qty": 0, "value_paise": 0, "taxable_paise": 0,
                                      "cgst_paise": 0, "sgst_paise": 0, "igst_paise": 0})
            row["qty"] += int(q); row["taxable_paise"] += int(tx); row["value_paise"] += int(tx) + int(tax)  # noqa: E702
            row["cgst_paise"] += int(tax) // 2; row["sgst_paise"] += int(tax) - int(tax) // 2  # noqa: E702
        t.rows = [rows[k] for k in sorted(rows)]
        c.note(t)
    return t


@report("b2b", "GST", "B2B bills (with GSTIN)")
def b2b(c: Ctx) -> Table:
    t = Table("B2B bills", [
        Col("invoice_no", "Bill no."), Col("date", "Date", "date"), Col("gstin", "Customer GSTIN"),
        Col("customer", "Customer"), Col("taxable_paise", "Taxable", "money", True),
        Col("cgst_paise", "CGST", "money", True), Col("sgst_paise", "SGST", "money", True),
        Col("igst_paise", "IGST", "money", True), Col("total_paise", "Invoice value", "money", True)],
        note="Bills with the customer's GSTIN: these go in the B2B section of GSTR-1.")
    for b in c.db.scalars(select(Bill).where(c.paid, Bill.customer_gstin.is_not(None)).order_by(Bill.created_at)):
        t.rows.append({"invoice_no": b.invoice_no, "date": str(b.business_date), "gstin": b.customer_gstin,
                       "customer": b.customer_name or "", "taxable_paise": b.taxable_paise, "cgst_paise": b.cgst_paise,
                       "sgst_paise": b.sgst_paise, "igst_paise": b.igst_paise, "total_paise": b.total_paise})
    return t


@report("invoice_series", "GST", "Invoice number series")
def invoice_series(c: Ctx) -> Table:
    t = Table("Invoice number series", [
        Col("outlet", "Outlet"), Col("first", "From"), Col("last", "To"), Col("issued", "Numbers used", "int", True),
        Col("cancelled", "Cancelled", "int", True)],
        note="For the documents-issued table of GSTR-1: first and last bill number of each series in the period. "
             "Counters that billed while the box was down have their own series.")
    base = and_(Bill.outlet_id.in_(c.outlets), Bill.business_date >= c.d1, Bill.business_date <= c.d2)
    for name, first, last, n, k in c.rows(
        select(Outlet.name, func.min(Bill.invoice_no), func.max(Bill.invoice_no), func.count(),
               func.sum(case((Bill.status == "cancelled", 1), else_=0)))
        .join(Outlet, Outlet.id == Bill.outlet_id).where(base).group_by(Outlet.name, Bill.series, Bill.fy_start)
        .order_by(Outlet.name, Bill.series)):
        t.rows.append({"outlet": name, "first": first, "last": last, "issued": n, "cancelled": int(k)})
    return t


# ------------------------------------------------------------------ customers

@report("customers", "Customers", "Customers (by mobile)")
def customers(c: Ctx) -> Table:
    t = Table("Customers", [
        Col("phone", "Mobile"), Col("name", "Name"), Col("visits", "Bills", "int", True),
        Col("spent_paise", "Spent", "money", True), Col("avg_paise", "Average bill", "money"),
        Col("first", "First visit", "date"), Col("last", "Last visit", "date")],
        note="Customers who gave their number, best first. Bills without a number aren't counted here.")
    for ph, name, n, s, f, l in c.rows(
        select(Bill.customer_phone, func.max(Bill.customer_name), func.count(), func.sum(Bill.total_paise),
               func.min(Bill.business_date), func.max(Bill.business_date))
        .where(c.paid, Bill.customer_phone.is_not(None)).group_by(Bill.customer_phone)
        .order_by(func.sum(Bill.total_paise).desc()).limit(5000)):
        t.rows.append({"phone": ph, "name": name or "", "visits": n, "spent_paise": int(s), "avg_paise": int(s) // n,
                       "first": str(f), "last": str(l)})
    return t


@report("phone_capture", "Customers", "Mobile number capture")
def phone_capture(c: Ctx) -> Table:
    t = Table("Mobile number capture by cashier", [
        Col("name", "Cashier"), Col("bills", "Bills", "int", True), Col("with_phone", "With mobile", "int", True),
        Col("declined", "Customer declined", "int", True), Col("rate_bp", "Capture rate", "percent")],
        note="How often each cashier gets the customer's number. Good for loyalty and SMS bills.")
    names = c.names()
    for uid, n, wp, dc in c.rows(
        select(Bill.created_by, func.count(), func.sum(case((Bill.customer_phone.is_not(None), 1), else_=0)),
               func.sum(case((Bill.customer_declined_phone.is_(True), 1), else_=0)))
        .where(c.paid).group_by(Bill.created_by)):
        t.rows.append({"name": names.get(uid, "?"), "bills": n, "with_phone": int(wp), "declined": int(dc),
                       "rate_bp": share_bp(int(wp), n)})
    return t


# ------------------------------------------------------------------ stock (inventory)

def _names_units(c: Ctx) -> dict:
    return {i.id: (i.name, i.unit, i.category) for i in c.db.scalars(select(StockItem))}


@report("stock_summary", "Stock", "Stock movement (opening to closing)")
def stock_summary(c: Ctx) -> Table:
    t = Table("Stock movement", [
        Col("item", "Item"), Col("unit", "Unit"), Col("opening", "Opening"), Col("purchased", "Bought"),
        Col("made", "Made / used in prep"), Col("sold", "Used in sales"), Col("wasted", "Wasted"),
        Col("count_diff", "Count difference"), Col("closing", "Closing"), Col("closing_value_paise", "Closing value", "money", True),
        Col("count_diff_paise", "Count difference value", "money", True)],
        note="Opening + bought + made − used − wasted ± count difference = closing. A count difference below zero "
             "means more was used than the recipes say, wasted without an entry, or missing.")
    names = _names_units(c)
    base = and_(StockMove.outlet_id.in_(c.outlets))
    opening = {i: int(q) for i, q in c.rows(select(StockMove.stock_item_id, func.sum(StockMove.qty_m))
                                           .where(base, StockMove.business_date < c.d1).group_by(StockMove.stock_item_id))}
    per: dict = defaultdict(lambda: defaultdict(int))
    vals: dict = defaultdict(int)
    for i, k, q, v in c.rows(select(StockMove.stock_item_id, StockMove.kind, func.sum(StockMove.qty_m), func.sum(StockMove.value_paise))
                             .where(base, StockMove.business_date >= c.d1, StockMove.business_date <= c.d2)
                             .group_by(StockMove.stock_item_id, StockMove.kind)):
        per[i][k] += int(q)
        if k == "count":
            vals[i] += int(v)
    costs = {lv.stock_item_id: lv.cost_k for lv in c.db.scalars(select(StockLevel).where(StockLevel.outlet_id.in_(c.outlets)))}
    for i in sorted(set(opening) | set(per), key=lambda x: names.get(x, ("",))[0]):
        if i not in names:
            continue
        name, unit, _cat = names[i]
        p = per[i]
        op = opening.get(i, 0)
        close = op + sum(p.values())
        f = lambda m: fmt_qty(m, unit) if m else ""  # noqa: E731
        t.rows.append({"item": name, "unit": unit, "opening": fmt_qty(op, unit), "purchased": f(p["purchase"]),
                       "made": f(p["prep_in"] + p["prep_out"]), "sold": f(-p["sale"]), "wasted": f(-p["waste"]),
                       "count_diff": f(p["count"]), "closing": fmt_qty(close, unit),
                       "closing_value_paise": value_paise(max(close, 0), costs.get(i, 0)), "count_diff_paise": vals[i]})
    return t


@report("food_cost", "Stock", "Food cost % by day")
def food_cost(c: Ctx) -> Table:
    t = Table("Food cost", [
        Col("date", "Date", "date"), Col("sales_paise", "Sales before GST", "money", True),
        Col("cost_paise", "Cost of what was sold (recipes)", "money", True), Col("food_cost_bp", "Food cost", "percent"),
        Col("waste_paise", "Wastage", "money", True), Col("count_paise", "Count differences", "money", True),
        Col("actual_bp", "Food cost incl. wastage and differences", "percent")],
        note="Cost comes from the recipes at average purchase prices. Targets: beverages 30–35%, food 25–30%. "
             "Items without a recipe count as zero cost, so add recipes first.")
    sales = {d: int(x) for d, x in c.rows(select(Bill.business_date, func.sum(Bill.taxable_paise)).where(c.paid).group_by(Bill.business_date))}
    cost: dict = defaultdict(lambda: defaultdict(int))
    for d, k, v in c.rows(select(StockMove.business_date, StockMove.kind, func.sum(StockMove.value_paise))
                          .where(StockMove.outlet_id.in_(c.outlets), StockMove.business_date >= c.d1, StockMove.business_date <= c.d2,
                                 StockMove.kind.in_(["sale", "waste", "count"]))
                          .group_by(StockMove.business_date, StockMove.kind)):
        cost[d][k] += int(v)
    for d in sorted(set(sales) | set(cost)):
        s_, cs = sales.get(d, 0), -cost[d]["sale"]
        w, cnt = -cost[d]["waste"], -cost[d]["count"]
        t.rows.append({"date": str(d), "sales_paise": s_, "cost_paise": cs, "food_cost_bp": share_bp(cs, s_),
                       "waste_paise": w, "count_paise": cnt, "actual_bp": share_bp(cs + w + cnt, s_)})
    return t


@report("purchases", "Stock", "Purchase register")
def purchases(c: Ctx) -> Table:
    t = Table("Purchase register", [
        Col("date", "Bill date", "date"), Col("supplier", "Supplier"), Col("gstin", "Supplier GSTIN"), Col("invoice_no", "Bill no."),
        Col("items", "Items", "int"), Col("amount_paise", "Before GST", "money", True), Col("tax_paise", "GST", "money", True),
        Col("total_paise", "Total", "money", True)],
        note="Supplier bills entered under Stock > Purchases, by bill date. Cancelled entries are left out.")
    sups = {s.id: s for s in c.db.scalars(select(Supplier))}
    for p in c.db.scalars(select(Purchase).where(Purchase.outlet_id.in_(c.outlets), Purchase.invoice_date >= c.d1,
                                                 Purchase.invoice_date <= c.d2, Purchase.status == "posted")
                          .order_by(Purchase.invoice_date)):
        s_ = sups.get(p.supplier_id) if p.supplier_id else None
        t.rows.append({"date": str(p.invoice_date), "supplier": s_.name if s_ else "", "gstin": (s_.gstin if s_ else "") or "",
                       "invoice_no": p.invoice_no or "", "items": len(p.lines), "amount_paise": p.amount_paise,
                       "tax_paise": p.tax_paise, "total_paise": p.total_paise})
    return t


@report("wastage", "Stock", "Wastage")
def wastage(c: Ctx) -> Table:
    t = Table("Wastage", [Col("date", "Date", "date"), Col("item", "Item"), Col("qty", "Quantity"), Col("reason", "Reason"),
                          Col("by", "Entered by"), Col("value_paise", "Value", "money", True)])
    names = _names_units(c)
    for m in c.db.scalars(select(StockMove).where(StockMove.outlet_id.in_(c.outlets), StockMove.kind == "waste",
                                                  StockMove.business_date >= c.d1, StockMove.business_date <= c.d2)
                          .order_by(StockMove.business_date, StockMove.created_at)):
        n, u, _ = names.get(m.stock_item_id, ("?", "g", None))
        t.rows.append({"date": str(m.business_date), "item": n, "qty": fmt_qty(-m.qty_m, u), "reason": m.note or "",
                       "by": c.names().get(m.created_by, ""), "value_paise": -m.value_paise})
    return t


# ------------------------------------------------------------------ endpoints

@router.get("/list", dependencies=[Depends(live_check)])
def report_list(auth: Auth = Depends(require(Perm.reports_view))) -> list[dict]:
    return [{"name": k, "group": g, "label": l} for k, (g, l, _) in REPORTS.items()]


@router.get("/r/{name}")
def run_report(name: str, outlet_id: uuid.UUID | None = None, date_from: date | None = None,
               date_to: date | None = None, format: str = Query(default="json", pattern=r"^(json|csv)$"),
               auth: Auth = Depends(require(Perm.reports_view)), db: Session = Depends(get_db)):
    if name not in REPORTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such report")
    outlets = _outlets(db, auth, outlet_id)
    d1, d2 = _range(date_from, date_to)
    table = REPORTS[name][2](Ctx(db, outlets, d1, d2))
    if format == "csv":
        span = f"{d1:%d %b %Y}" + ("" if d1 == d2 else f" to {d2:%d %b %Y}")
        return Response(to_csv(table, span), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}-{d1}{"" if d1 == d2 else "-" + str(d2)}.csv"'})
    return {**table.as_dict(), "date_from": str(d1), "date_to": str(d2)}
