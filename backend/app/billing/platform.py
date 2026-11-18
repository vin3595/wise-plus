"""wise-plus: for you, the platform owner — every restaurant's WhatsApp messages and bill scans in one place.

Who sees it: the owner of one of your own businesses (WISE_HOUSE_TENANTS). Nobody else, whatever their role."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..db import system_session
from ..deps import Auth, get_auth, get_db
from ..models import Outlet, Tenant
from .models import Subscription
from .plans import MSGS_PER_OUTLET, SCANS_PER_MONTH
from .service import _month, access, house

# What Meta charges us per message in India (rate card), to show the cost behind each restaurant's sending.
META_PAISE = {"bill": 11.5, "otp": 11.5, "promo": 86.31}

router = APIRouter(prefix="/platform", tags=["platform"])


def is_admin(db: Session, auth: Auth) -> bool:
    t = db.get(Tenant, auth.tenant_id)
    return bool(t and house(t.login_id) and auth.role.value == "owner")


def usage(month: str | None = None) -> dict:
    """Every restaurant, this month (or YYYY-MM): bill messages used of the free allowance, bought credits left,
    what actually went out on WhatsApp by kind and what Meta charged for it, and bill scans."""
    month = month or _month()
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    rows = []
    with system_session() as db:
        sent: dict = {}
        for tid, kind, st, n in db.execute(text(
                "select tenant_id, kind, status, count(*) from wa_outbox where created_at >= :a and created_at < :b group by 1, 2, 3"),
                {"a": start, "b": end}):
            sent.setdefault(tid, {}).setdefault(kind, {})[st] = n
        outlets = dict(db.execute(select(Outlet.tenant_id, func.count()).where(Outlet.is_active.is_(True), Outlet.is_training.is_(False))
                                  .group_by(Outlet.tenant_id)).all())
        for t in db.scalars(select(Tenant).order_by(Tenant.name)):
            s = db.get(Subscription, t.id)
            st = access(db, t)["state"]
            n_out = outlets.get(t.id, 0) or 1
            cur = month == _month()
            used = (s.msgs_used if s and s.msg_month == month else 0) if cur else None
            allow = MSGS_PER_OUTLET * n_out
            by = sent.get(t.id, {})
            out_sent = {k: v.get("sent", 0) for k, v in by.items()}
            failed = sum(v.get("failed", 0) for v in by.values())
            cost = sum(META_PAISE.get(k, META_PAISE["bill"]) * n for k, n in out_sent.items())
            unlimited = st in ("house", "free")
            rows.append({
                "tenant_id": str(t.id), "name": t.app_name or t.name, "login_id": t.login_id, "state": st, "outlets": n_out,
                "bill_msgs_used": used if used is not None else out_sent.get("bill", 0), "bill_msgs_free": None if unlimited else allow,
                "credits_left": None if unlimited else (s.bonus_msgs if s else 0),
                "left_this_month": None if unlimited else (max(allow - (used or 0), 0) + (s.bonus_msgs if s else 0)),
                "sent": out_sent, "failed": failed, "meta_cost_paise": round(cost),
                "scans_used": (s.scans_used if s and s.scan_month == month else 0), "scans_free": None if house(t.login_id) else SCANS_PER_MONTH,
                "scans_bought_left": s.bonus_scans if s else 0,
                "packs_bought": sum(1 for p in (s.packs or []) if p.get("messages")) if s else 0,
                "alert": (s.msg_alert or "").split(":")[-1] if s and s.msg_alert and s.msg_alert.startswith(month) else None,
            })
    tot = {"sent": sum(sum(r["sent"].values()) for r in rows), "meta_cost_paise": sum(r["meta_cost_paise"] for r in rows),
           "credits_left": sum(r["credits_left"] or 0 for r in rows), "restaurants": len(rows),
           "running_out": sum(1 for r in rows if r["left_this_month"] is not None and r["left_this_month"] <= max(1, (r["bill_msgs_free"] or 0) // 5))}
    return {"month": month, "restaurants": rows, "total": tot}


@router.get("/usage")
def platform_usage(month: str | None = None, auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    if not is_admin(db, auth):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if month and not (len(month) == 7 and month[4] == "-" and month.replace("-", "").isdigit()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Month as YYYY-MM")
    return usage(month)


def whatsapp_text(month: str | None = None) -> str:
    u = usage(month)
    t = u["total"]
    lines = [f"*WhatsApp across all restaurants, {u['month']}*",
             f"{t['sent']} messages sent · Meta cost ₹{t['meta_cost_paise'] / 100:,.0f} · {t['credits_left']} bought credits unused"
             + (f" · ⚠️ {t['running_out']} running out" if t["running_out"] else ""), ""]
    for r in sorted(u["restaurants"], key=lambda r: -sum(r["sent"].values()))[:25]:
        if r["bill_msgs_free"] is None:
            left = "unlimited"
        else:
            left = f"{r['bill_msgs_used']}/{r['bill_msgs_free']} free used · {r['credits_left']} bought left"
            if r["left_this_month"] is not None and r["left_this_month"] <= max(1, r["bill_msgs_free"] // 5):
                left += " ⚠️"
        sent = sum(r["sent"].values())
        lines.append(f"• *{r['name']}* ({r['state']}): {left} · sent {sent}" + (f" ({r['sent'].get('promo', 0)} broadcast)" if r["sent"].get("promo") else ""))
    return "\n".join(lines)


def admin_by_chosen(chosen) -> bool:
    with system_session() as db:
        t = db.get(Tenant, uuid.UUID(chosen[0]))
        return bool(t and house(t.login_id) and chosen[2] == "owner")
