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


# ------------------------------------------------------------------ bill messages to customers (WhatsApp)

def outlets_paid(db: Session, tenant_id) -> int:
    from ..models import Outlet
    from sqlalchemy import func
    return int(db.scalar(select(func.count()).select_from(Outlet).where(
        Outlet.tenant_id == tenant_id, Outlet.is_active.is_(True), Outlet.is_training.is_(False))) or 0) or 1


def msgs(db: Session, tenant: Tenant) -> dict:
    """{used, allowance, bonus, left, unlimited}. House accounts, and servers without payments set up, aren't limited."""
    from .plans import MSGS_PER_OUTLET
    s = db.get(Subscription, tenant.id)
    used = s.msgs_used if s and s.msg_month == _month() else 0
    allowance = MSGS_PER_OUTLET * outlets_paid(db, tenant.id)
    bonus = s.bonus_msgs if s else 0
    unlimited = access(db, tenant)["state"] in ("house", "free")
    return {"used": used, "allowance": allowance, "bonus": bonus, "per_outlet": MSGS_PER_OUTLET,
            "left": None if unlimited else max(allowance - used, 0) + bonus, "unlimited": unlimited}


def take_msg(db: Session, tenant: Tenant) -> tuple[bool, str | None]:
    """Use one bill message. Returns (may send, alert for the owner or None)."""
    m = msgs(db, tenant)
    s = db.scalar(select(Subscription).where(Subscription.tenant_id == tenant.id).with_for_update()) or sub_for(db, tenant.id, create=True)
    if s.msg_month != _month():
        s.msg_month, s.msgs_used, s.msg_alert = _month(), 0, None
    if m["unlimited"]:
        s.msgs_used += 1
        return True, None
    if s.msgs_used < m["allowance"]:
        s.msgs_used += 1
    elif s.bonus_msgs > 0:
        s.bonus_msgs -= 1
    else:
        if s.msg_alert != f"{_month()}:0":
            s.msg_alert = f"{_month()}:0"
            return False, ("📵 Bills on WhatsApp have stopped: this month's free messages and bought packs are used up. "
                           "Customers still get printed bills. Buy more under Settings › Billing.")
        return False, None
    left = max(m["allowance"] - s.msgs_used, 0) + s.bonus_msgs
    if s.msg_alert is None and left <= m["allowance"] // 5:      # 80% of the month's free messages gone, little bought
        s.msg_alert = f"{_month()}:80"
        return True, (f"📨 {left} bill messages left this month ({s.msgs_used} used of {m['allowance']} free"
                      + (f", {s.bonus_msgs} bought" if s.bonus_msgs else "") + "). Buy a pack so bills keep going on WhatsApp.")
    return True, None


def take_credits(db: Session, tenant: Tenant, credits: int) -> bool:
    """Bulk messages (broadcasts) come out of bought message credits only, never the free monthly bill messages."""
    if access(db, tenant)["state"] in ("house", "free"):
        return True
    s = db.scalar(select(Subscription).where(Subscription.tenant_id == tenant.id).with_for_update()) or sub_for(db, tenant.id, create=True)
    if s.bonus_msgs < credits:
        return False
    s.bonus_msgs -= credits
    return True
