"""wise-plus: the nightly WhatsApp summary across all outlets (from the Shakewallah stock app).

Every night at WISE_NIGHTLY_AT (India time) each subscribed owner/manager gets the day's closing picture:
sales, food cost, wastage, count differences and purchases per outlet, low stock, tomorrow's order for
daily items and transfers not yet received.

WhatsApp only allows a free message within 24 hours of the person's last message to us. Outside that,
an approved template (WISE_WA_NIGHTLY_TEMPLATE, one {{1}} = restaurant name, quick-reply button "Show
summary") is sent instead; tapping it opens the window and brings the full summary.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..assistant.answers import Reply, rs
from ..config import get_settings
from ..core.kds import business_date
from ..core.stock import M
from ..db import system_session, tenant_session
from ..models import Outlet, StockItem, StockLevel, Tenant, User
from .models import NightlyRun, StockTransfer, WaAlert, WaContact

log = logging.getLogger("wise.nightly")
IST = ZoneInfo("Asia/Kolkata")
SUMMARY = {"summary", "closing", "nightly", "night", "show"}


def subscribe(db: Session, tenant_id, user_id, on: bool, topic: str = "nightly") -> None:
    row = db.scalar(select(WaAlert).where(WaAlert.user_id == user_id, WaAlert.topic == topic))
    if on and not row:
        db.add(WaAlert(tenant_id=tenant_id, user_id=user_id, topic=topic))
    elif not on and row:
        db.delete(row)
    db.flush()


def seen(phone10: str) -> None:
    """Remember that this number just wrote to us (the 24-hour window)."""
    if not phone10:
        return
    with system_session() as db:
        now = datetime.now(timezone.utc)
        db.execute(insert(WaContact).values(phone10=phone10, last_in_at=now)
                   .on_conflict_do_update(index_elements=[WaContact.phone10], set_={"last_in_at": now}))


def _qty(m: int, unit: str) -> str:
    v = m / M
    if unit in ("g", "ml") and abs(v) >= 1000:
        return f"{v / 1000:g} {'kg' if unit == 'g' else 'L'}"
    return f"{round(v, 1):g} {unit}"


def outlet_lines(db: Session, o: Outlet, day: date) -> tuple[list[str], dict]:
    from ..routers.dayclose import summary
    from .daily import tomorrow_need
    s = summary(db, o.id, day)
    bits = [f"Sales {rs(s['sales_paise'])} ({s['bills']} bills)"]
    if s.get("food_cost_bp"):
        bits.append(f"food cost {s['food_cost_bp'] / 100:.0f}%")
    if s.get("wastage_paise"):
        bits.append(f"waste {rs(s['wastage_paise'])}")
    if s.get("count_diff_paise", 0) < 0:
        bits.append(f"count short {rs(-s['count_diff_paise'])}")
    if s.get("purchases_paise"):
        bits.append(f"bought {rs(s['purchases_paise'])}")
    out = [f"*{o.name}*: " + " · ".join(bits)]
    levels = {lv.stock_item_id: lv for lv in db.scalars(select(StockLevel).where(StockLevel.outlet_id == o.id))}
    low, order = [], []
    for it in db.scalars(select(StockItem).where(StockItem.is_active.is_(True), StockItem.made_on_sale.is_(False)).order_by(StockItem.name)):
        lv = levels.get(it.id)
        qty = lv.qty_m if lv else 0
        if it.order_daily:
            need = tomorrow_need(db, o.id, it, qty, day)
            if need["order_m"]:
                order.append(f"{it.name} {_qty(need['order_m'], it.unit)}")
            continue
        mn = lv.min_level_m if lv and lv.min_level_m is not None else it.min_level_m
        if lv and mn > 0 and qty <= mn:
            low.append(it.name)
    if low:
        out.append(f"   Low stock ({len(low)}): " + ", ".join(low[:8]) + ("…" if len(low) > 8 else ""))
    if order:
        out.append("   Order for tomorrow: " + ", ".join(order[:10]))
    from ..creditnotes.router import day_refunds
    ref = day_refunds(db, day, o.id)
    if ref:
        out.append(f"   Refunds (credit notes): {rs(ref)}")
    from ..expenses.models import PettyExpense
    pet = db.scalars(select(PettyExpense).where(PettyExpense.outlet_id == o.id, PettyExpense.spent_on == day, PettyExpense.status == "ok")).all()
    if pet:
        noslip = sum(1 for x in pet if not x.file_path and x.source != "vendor")
        out.append(f"   Petty expenses: {rs(sum(x.amount_paise for x in pet))} ({len(pet)})" + (f" · 🔴 {noslip} without a slip" if noslip else ""))
    from ..tableqr.router import day_line
    tql = day_line(db, o.id, day)
    if tql:
        out.append(tql)
    from ..custwa.router import outlet_line as fb_line
    fbl = fb_line(db, o.id, day)
    if fbl:
        out.append(fbl)
    waiting = db.scalar(select(func.count()).select_from(StockTransfer).where(StockTransfer.to_outlet_id == o.id, StockTransfer.status == "sent")) or 0
    if waiting:
        out.append(f"   {waiting} transfer{'s' if waiting > 1 else ''} not received yet")
    return out, s


def summary_text(db: Session, day: date | None = None) -> Reply:
    """The closing summary for every outlet of the restaurant whose session this is."""
    day = day or business_date()
    t = db.scalar(select(Tenant))
    lines = [f"🌙 *{t.name if t else 'Closing'}* · {day.strftime('%a %d %b')}"]
    totals = {"sales_paise": 0, "bills": 0, "purchases_paise": 0, "wastage_paise": 0}
    outlets = db.scalars(select(Outlet).order_by(Outlet.name)).all()
    for o in outlets:
        part, s = outlet_lines(db, o, day)
        lines += part
        for k in totals:
            totals[k] += int(s.get(k) or 0)
    if len(outlets) > 1:
        lines.append(f"\n*All outlets*: Sales {rs(totals['sales_paise'])} ({totals['bills']} bills) · bought {rs(totals['purchases_paise'])}"
                     + (f" · waste {rs(totals['wastage_paise'])}" if totals["wastage_paise"] else ""))
    return Reply("\n".join(lines), [("dues", "Dues"), ("sheet", "Last daily sheet"), ("nightly off", "Stop nightly")])


def _recipients(db: Session) -> list[tuple[uuid.UUID, str, str, str]]:
    """(tenant_id, restaurant name, phone, login_id) for everyone subscribed."""
    return db.execute(select(WaAlert.tenant_id, Tenant.name, User.phone, Tenant.login_id)
                      .join(User, User.id == WaAlert.user_id).join(Tenant, Tenant.id == WaAlert.tenant_id)
                      .where(WaAlert.topic == "nightly", User.is_active.is_(True), User.phone.is_not(None),
                             Tenant.status.not_in(["suspended"]))).all()


def run(day: date | None = None, force: bool = False) -> int:
    """Send tonight's summaries. Returns how many went out. Once per day across all API workers."""
    from ..whatsapp import client
    s = get_settings()
    day = day or business_date()
    with system_session() as db:
        if not force and db.execute(insert(NightlyRun).values(day=day).on_conflict_do_nothing().returning(NightlyRun.day)).first() is None:
            return 0
        people = _recipients(db)
        recent = {c.phone10: c.last_in_at for c in db.scalars(select(WaContact))}
    texts: dict = {}
    sent = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=23, minutes=50)
    for tid, name, phone, login in people:
        p10 = client.last10(phone)
        try:
            if recent.get(p10) and recent[p10] > cutoff:
                if tid not in texts:
                    with tenant_session(tid) as db:
                        texts[tid] = summary_text(db, day)
                r = texts[tid]
                client.send_options(phone, r.text, r.options)
            elif s.wa_nightly_template:
                client.send_template(phone, s.wa_nightly_template, [name])
            else:
                log.info("nightly: …%s hasn't written in 24h and no template is set; skipped", p10[-4:])
                continue
            sent += 1
        except Exception:  # noqa: BLE001 - one bad number mustn't stop the rest
            log.exception("nightly: sending to …%s failed", p10[-4:])
    log.info("nightly: %s summaries sent for %s", sent, day)
    return sent


async def scheduler() -> None:
    """Checks every minute; at WISE_NIGHTLY_AT India time sends the night's summaries (once)."""
    s = get_settings()
    if not (s.wa_token and s.wa_phone_number_id and s.nightly_at):
        return
    hh, mm = (int(x) for x in s.nightly_at.split(":"))
    while True:
        try:
            now = datetime.now(IST)
            if (now.hour, now.minute) >= (hh, mm):
                await asyncio.to_thread(run, now.date())
        except Exception:  # noqa: BLE001
            log.exception("nightly scheduler")
        await asyncio.sleep(60)
