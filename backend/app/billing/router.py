"""wise-plus billing: Settings > Billing, Razorpay checkout, scan packs and the Razorpay webhook."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import modules as mods
from ..core.permissions import Perm
from ..db import system_session
from ..deps import Auth, get_auth, get_db, require
from ..models import Tenant
from ..services import audit
from . import rz
from .models import RzPlan, Subscription
from .plans import ALL_PACKS, BUNDLE, FOUNDING_PAISE, FOUNDING_SLOTS, MSG_PACKS, PACKS, PAID, PRICES, SCANS_PER_MONTH, price
from .service import msgs, access, house, outlets_paid, scans, sub_for

log = logging.getLogger("wise.billing")
router = APIRouter(prefix="/billing", tags=["billing"])
public = APIRouter(prefix="/public/billing", tags=["billing"])


class PlanIn(BaseModel):
    modules: list[str] = Field(min_length=1)
    period: str = Field(pattern=r"^(monthly|yearly|founding)$")


class SubVerifyIn(BaseModel):
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


class PackVerifyIn(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _tenant(db: Session, auth: Auth) -> Tenant:
    return db.get(Tenant, auth.tenant_id)


def _quote(body: PlanIn, outlets: int = 1) -> dict:
    try:
        return price(body.modules, body.period, outlets)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None


@router.get("")
def billing(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    t = _tenant(db, auth)
    s = db.get(Subscription, t.id)
    a = access(db, t)
    return {
        "state": a["state"], "until": _ts(a["until"]), "trial_ends_at": _ts(t.trial_ends_at), "house": house(t.login_id),
        "plan": {"modules": s.modules, "period": s.period, "amount_paise": s.amount_paise, "status": s.status,
                 "paid_until": _ts(s.paid_until), "outlets": s.outlets} if s and s.modules else None,
        "outlets": outlets_paid(db, t.id),
        "prices": {"services": [{"key": k, "name": mods.MODULES[k], "monthly_paise": v} for k, v in PRICES.items()],
                   "bundle_monthly_paise": BUNDLE, "yearly": "Pay 10 months, get 12. Bundle: pay 9, get 12."},
        "founding": {"price_paise": FOUNDING_PAISE, "left": _founding_left(db), "slots": FOUNDING_SLOTS, "member": bool(s and s.founding)},
        "scans": scans(db, t) | {"per_month": SCANS_PER_MONTH},
        "packs": [{"key": k, **v} for k, v in PACKS.items()],
        "messages": msgs(db, t), "msg_packs": [{"key": k, **v} for k, v in MSG_PACKS.items()],
        "platform_admin": __import__("app.billing.platform", fromlist=["is_admin"]).is_admin(db, auth),
        "ready": rz.configured(), "key_id": rz.keys()["key_id"] or None,
    }


def _founding_left(db: Session) -> int:
    taken = db.scalar(select(func.count()).select_from(Subscription).where(Subscription.founding.is_(True))) or 0
    return max(FOUNDING_SLOTS - taken, 0)


@router.post("/quote")
def quote(body: PlanIn, auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    return _quote(body, outlets_paid(db, auth.tenant_id))


def _plan_id(amount: int, period: str, name: str) -> str:
    key = f"{amount}:{period}"
    with system_session() as db:
        have = db.get(RzPlan, key)
        if have:
            return have.plan_id
    p = rz.call("POST", "/plans", {"period": period, "interval": 1,
                                   "item": {"name": name[:60], "amount": amount, "currency": "INR"}})
    with system_session() as db:
        if not db.get(RzPlan, key):
            db.add(RzPlan(key=key, plan_id=p["id"]))
    return p["id"]


@router.post("/subscribe")
def subscribe(body: PlanIn, auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    """Start Razorpay checkout for these services. Nothing is charged before the trial (or the time
    already paid for) ends."""
    t = _tenant(db, auth)
    if house(t.login_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This account is never billed")
    if not rz.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Payments aren't set up yet. Try again later.")
    q = _quote(body, outlets_paid(db, t.id))
    s = sub_for(db, t.id, create=True)
    if q["period"] == "founding" and _founding_left(db) <= 0 and not s.founding:
        raise HTTPException(status.HTTP_409_CONFLICT, "All 50 founding places are taken. The yearly bundle is the next best price.")
    period = q["period"]
    per = f" · {q['outlets']} outlets" if q["outlets"] > 1 else ""
    plan = _plan_id(q["amount_paise"], period, "Ease My Bill · everything" + per)
    now = _now()
    start = max([d for d in (t.trial_ends_at, s.paid_until if s.status in ("authenticated", "active") else None) if d and d > now + timedelta(minutes=5)],
                default=None)
    # founding: one year at the founding price; after that they pick a regular plan
    req = {"plan_id": plan, "total_count": 1 if q["period"] == "founding" else 10 if q["period"] == "yearly" else 120, "customer_notify": 1,
           "notes": {"tenant_id": str(t.id), "login_id": t.login_id, "modules": ",".join(q["modules"])}}
    if start:
        req["start_at"] = int(start.timestamp())
    r = rz.call("POST", "/subscriptions", req)
    s.pending = {"subscription_id": r["id"], "modules": q["modules"], "period": q["period"], "amount_paise": q["amount_paise"],
                 "start_at": _ts(start), "outlets": q["outlets"]}
    db.flush()
    return {"subscription_id": r["id"], "key_id": rz.keys()["key_id"], "quote": q, "starts": _ts(start),
            "name": t.name, "email": t.owner_email, "phone": t.owner_phone}


def _activate(db: Session, t: Tenant, s: Subscription, paid_until: datetime | None = None) -> None:
    p = s.pending or {}
    old = s.rz_subscription_id if s.rz_subscription_id != p.get("subscription_id") else None
    s.rz_subscription_id = p["subscription_id"]
    s.modules, s.period, s.amount_paise = p["modules"], p["period"], p["amount_paise"]
    s.outlets = int(p.get("outlets") or 1)
    s.status = "authenticated"
    if s.period == "founding":
        db.execute(text("select pg_advisory_xact_lock(4999)"))     # 50 places, no more
        if not s.founding and _founding_left(db) <= 0:
            log.warning("billing: founding place for %s paid after all were taken; honoured", t.id)
        s.founding = True
    start = datetime.fromisoformat(p["start_at"]) if p.get("start_at") else _now()
    s.paid_until = paid_until or start + (timedelta(days=366) if s.period in ("yearly", "founding") else timedelta(days=31))
    s.pending = None
    t.modules = list(s.modules) + (["sheets"] if house(t.login_id) else [])
    t.plan = s.period[:20]
    try:                                    # wise-plus growth: lead → paid; referral rewards (once)
        from ..growth.service import on_first_payment
        on_first_payment(db, t)
    except Exception:  # noqa: BLE001
        log.exception("growth: payment hook")
    if t.status == "read_only":
        t.status = "active"
    mods.forget(t.id)
    db.flush()
    if old:   # the earlier plan stops renewing; it still runs to the end of what was paid
        try:
            rz.call("POST", f"/subscriptions/{old}/cancel", {"cancel_at_cycle_end": 1})
        except Exception:  # noqa: BLE001
            log.exception("billing: couldn't stop the old subscription %s", old)


@router.post("/verify")
def verify(body: SubVerifyIn, auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    """Checkout finished: check Razorpay's signature, then the plan is on straight away."""
    t = _tenant(db, auth)
    s = sub_for(db, t.id)
    msg = f"{body.razorpay_payment_id}|{body.razorpay_subscription_id}".encode()
    if not rz.hmac_ok(msg, body.razorpay_signature, rz.keys()["key_secret"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payment couldn't be verified")
    if s and s.rz_subscription_id == body.razorpay_subscription_id and s.status in ("authenticated", "active"):
        return billing(auth, db)          # Razorpay's webhook got here first and already switched the plan on
    if not s or not s.pending or s.pending.get("subscription_id") != body.razorpay_subscription_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "That payment isn't for the plan being set up. Try again from Billing.")
    _activate(db, t, s)
    audit.record(db, t.id, auth.user_id, "subscribe", "billing", t.id, after={"modules": s.modules, "period": s.period})
    return billing(auth, db)


@router.post("/cancel")
def cancel(auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    """Stop renewing. Everything keeps working until the paid time ends."""
    t = _tenant(db, auth)
    s = sub_for(db, t.id)
    if not s or not s.rz_subscription_id or s.status in ("cancelled", "completed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "There's no plan renewing")
    rz.call("POST", f"/subscriptions/{s.rz_subscription_id}/cancel", {"cancel_at_cycle_end": 1})
    s.status = "cancelled"
    db.flush()
    audit.record(db, t.id, auth.user_id, "cancel", "billing", t.id)
    return billing(auth, db)


@router.post("/packs/{pack}/order")
def buy_pack(pack: str, auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    p = ALL_PACKS.get(pack)
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such pack")
    if not rz.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Payments aren't set up yet. Try again later.")
    t = _tenant(db, auth)
    o = rz.call("POST", "/orders", {"amount": p["paise"], "currency": "INR", "receipt": f"{'msg' if 'messages' in p else 'scan'}-{uuid.uuid4().hex[:12]}",
                                    "notes": {"tenant_id": str(t.id), "pack": pack}})
    return {"order_id": o["id"], "key_id": rz.keys()["key_id"], "amount_paise": p["paise"], "scans": p.get("scans"),
            "messages": p.get("messages"),
            "name": t.name, "email": t.owner_email, "phone": t.owner_phone}


def _credit_pack(db: Session, tenant_id, order_id: str, payment_id: str, pack: str) -> bool:
    s = db.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id).with_for_update()) or sub_for(db, tenant_id, create=True)
    done = s.packs or []
    if any(x["order_id"] == order_id for x in done):
        return False
    p = ALL_PACKS[pack]
    if "messages" in p:
        s.bonus_msgs += p["messages"]
        s.msg_alert = None
    else:
        s.bonus_scans += p["scans"]
    s.packs = done + [{"order_id": order_id, "payment_id": payment_id, **{k: v for k, v in p.items() if k != "paise"}, "at": _now().isoformat()}]
    db.flush()
    return True


@router.post("/packs/verify")
def verify_pack(body: PackVerifyIn, auth: Auth = Depends(require(Perm.tenant_settings)), db: Session = Depends(get_db)) -> dict:
    msg = f"{body.razorpay_order_id}|{body.razorpay_payment_id}".encode()
    if not rz.hmac_ok(msg, body.razorpay_signature, rz.keys()["key_secret"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payment couldn't be verified")
    o = rz.call("GET", f"/orders/{body.razorpay_order_id}")
    notes = o.get("notes") or {}
    if notes.get("tenant_id") != str(auth.tenant_id) or notes.get("pack") not in ALL_PACKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That payment isn't for this account")
    _credit_pack(db, auth.tenant_id, body.razorpay_order_id, body.razorpay_payment_id, notes["pack"])
    return billing(auth, db)


@public.post("/razorpay")
async def webhook(request: Request) -> dict:
    """Razorpay tells us about renewals, failures and cancellations (and pack payments, if checkout
    closed before the app could confirm)."""
    raw = await request.body()
    if not rz.hmac_ok(raw, request.headers.get("x-razorpay-signature"), rz.keys()["webhook_secret"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad signature")
    ev = json.loads(raw)
    kind = ev.get("event", "")
    pl = ev.get("payload") or {}
    with system_session() as db:
        if kind.startswith("subscription."):
            e = (pl.get("subscription") or {}).get("entity") or {}
            s = db.scalar(select(Subscription).where(Subscription.rz_subscription_id == e.get("id")))
            if s is None:
                tid = (e.get("notes") or {}).get("tenant_id")
                s = db.get(Subscription, uuid.UUID(tid)) if tid else None
                if s and s.pending and s.pending.get("subscription_id") == e.get("id") and kind in ("subscription.authenticated", "subscription.activated", "subscription.charged"):
                    _activate(db, db.get(Tenant, s.tenant_id), s)     # paid, but the app never heard back
                else:
                    return {"ok": True}
            st = kind.split(".", 1)[1]
            if st in ("authenticated", "activated", "charged", "resumed"):
                s.status = "active" if st != "authenticated" else s.status
            elif st in ("halted", "cancelled", "completed", "paused"):
                s.status = {"paused": "halted"}.get(st, st)
            if e.get("current_end") and st in ("activated", "charged"):
                s.paid_until = datetime.fromtimestamp(int(e["current_end"]), timezone.utc)
            log.info("billing: %s for %s", kind, s.tenant_id)
        elif kind in ("order.paid", "payment.captured"):
            pay = (pl.get("payment") or {}).get("entity") or {}
            notes = pay.get("notes") or ((pl.get("order") or {}).get("entity") or {}).get("notes") or {}
            if notes.get("pack") in ALL_PACKS and notes.get("tenant_id") and pay.get("order_id"):
                _credit_pack(db, uuid.UUID(notes["tenant_id"]), pay["order_id"], pay.get("id", ""), notes["pack"])
    return {"ok": True}


def paid_modules(db: Session, tenant: Tenant) -> list[str] | None:
    """Services the current plan covers (None while on trial or for your own accounts: anything goes)."""
    a = access(db, tenant)
    if a["state"] != "paid":
        return None
    s = db.get(Subscription, tenant.id)
    return list(s.modules or PAID)
