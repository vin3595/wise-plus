"""wise-plus billing rules: who can use the app, and bill-scan allowances.

* House accounts (WISE_HOUSE_TENANTS, your own businesses) never pay and alone get Daily cash sheet.
* Everyone else: free trial, then a paid plan. With no paid time left the app is read-only (nothing is
  deleted) until they pay; Billing itself always works.
* Bill scan: 150 scans a month, plus packs bought (they never expire).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Tenant
from .models import Subscription
from .plans import SCANS_PER_MONTH

GRACE = timedelta(days=3)       # a failed renewal gets three days before the app locks


def house(login_id: str | None) -> bool:
    return bool(login_id) and login_id.lower() in {x.strip().lower() for x in get_settings().house_tenants.split(",") if x.strip()}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sub_for(db: Session, tenant_id, create: bool = False) -> Subscription | None:
    s = db.get(Subscription, tenant_id)
    if s is None and create:
        s = Subscription(tenant_id=tenant_id, status="none", scans_used=0, bonus_scans=0, amount_paise=0)
        db.add(s)
        db.flush()
    return s


def access(db: Session, tenant: Tenant) -> dict:
    """{state: house | trial | paid | locked, until}"""
    if house(tenant.login_id):
        return {"state": "house", "until": None}
    s = db.get(Subscription, tenant.id)
    now = _now()
    if s and s.paid_until and s.status in ("authenticated", "active", "cancelled", "halted", "completed", "pending") and s.paid_until + GRACE > now:
        return {"state": "paid", "until": s.paid_until}
    if tenant.trial_ends_at and tenant.trial_ends_at > now:
        return {"state": "trial", "until": tenant.trial_ends_at}
    from . import rz
    if not rz.configured():      # payments not set up on this server: nothing to pay with, so nothing locks
        return {"state": "free", "until": None}
    return {"state": "locked", "until": tenant.trial_ends_at}


def locked(db: Session, tenant: Tenant) -> bool:
    return access(db, tenant)["state"] == "locked"


class ScanLimit(Exception):
    pass


def _month() -> str:
    return _now().astimezone().strftime("%Y-%m")


def scans(db: Session, tenant: Tenant) -> dict:
    s = db.get(Subscription, tenant.id)
    used = s.scans_used if s and s.scan_month == _month() else 0
    return {"used": used, "limit": None if house(tenant.login_id) else SCANS_PER_MONTH, "bonus": s.bonus_scans if s else 0}


def check_scan(db: Session, tenant: Tenant) -> None:
    """Before reading a bill: is there a scan left this month?"""
    if house(tenant.login_id):
        return
    sc = scans(db, tenant)
    if sc["used"] >= SCANS_PER_MONTH and sc["bonus"] <= 0:
        raise ScanLimit(f"All {SCANS_PER_MONTH} bill scans for this month are used. Buy a scan pack in Settings › Billing, "
                        "or type the bill in.")


def count_scan(db: Session, tenant: Tenant) -> None:
    """After a bill was read: use one scan (this month's first, then bought ones)."""
    if house(tenant.login_id):
        return
    s = db.scalar(select(Subscription).where(Subscription.tenant_id == tenant.id).with_for_update()) or sub_for(db, tenant.id, create=True)
    if s.scan_month != _month():
        s.scan_month, s.scans_used = _month(), 0
    if s.scans_used < SCANS_PER_MONTH:
        s.scans_used += 1
    elif s.bonus_scans > 0:
        s.bonus_scans -= 1
    db.flush()


def scan_or_402(db: Session, tenant: Tenant) -> None:
    try:
        check_scan(db, tenant)
    except ScanLimit as e:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(e)) from None
