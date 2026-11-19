"""wise-plus growth API: the public landing page's form and demo slots; your Leads page; a restaurant's referral link."""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..billing.admin_auth import require_admin
from ..db import system_session
from ..deps import Auth, get_auth
from . import service as gs
from .models import Lead, Referral

public = APIRouter(prefix="/public/growth", tags=["growth"])
router = APIRouter(tags=["growth"])
_hits: dict[str, deque] = defaultdict(deque)


def _ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()


def _limit(request: Request, n: int = 6, per: int = 600) -> None:
    q = _hits[_ip(request)]
    now = time.time()
    while q and q[0] < now - per:
        q.popleft()
    if len(q) >= n:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many tries. Please wait a few minutes.")
    q.append(now)


class LeadIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=10, max_length=15)
    restaurant: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=60)
    outlets: int | None = Field(default=None, ge=1, le=500)
    uses_now: str | None = Field(default=None, max_length=40)
    consent: bool = False
    demo_at: datetime | None = None
    ref: str | None = Field(default=None, max_length=20)
    utm: dict | None = None
    website: str | None = None          # honeypot: people never fill it


@public.post("/leads", status_code=201)
def new_lead(body: LeadIn, request: Request, background: BackgroundTasks) -> dict:
    _limit(request)
    if body.website:                    # a bot
        return {"ok": True}
    if body.demo_at and body.demo_at.isoformat() not in set(gs.demo_slots(14)):
        raise HTTPException(status.HTTP_409_CONFLICT, "That demo time was just taken. Pick another.")
    try:
        ld, new = gs.save_lead(body.model_dump(exclude={"website"}) | {"source": "referral" if body.ref else "website"})
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    background.add_task(gs.welcome, ld)
    return {"ok": True, "trial_url": f"/signup?phone={ld.phone10}" + (f"&ref={ld.ref_code}" if ld.ref_code else ""),
            "demo_at": ld.demo_at.isoformat() if ld.demo_at else None}


@public.get("/info")
def info() -> dict:
    """Prices and founding places left, for the landing page."""
    from ..billing.plans import BUNDLE, FOUNDING_PAISE, FOUNDING_SLOTS, MSGS_PER_OUTLET
    from ..billing.router import _founding_left
    from ..config import get_settings
    with system_session() as sdb:
        left = _founding_left(sdb)
    s = get_settings()
    return {"bundle_paise": BUNDLE, "founding_paise": FOUNDING_PAISE, "founding_slots": FOUNDING_SLOTS, "founding_left": left,
            "trial_days": s.trial_days, "msgs_per_outlet": MSGS_PER_OUTLET, "pixel_id": s.meta_pixel_id or None,
            "whatsapp": s.wa_display_number or None,
            "legal_name": s.legal_name or None, "email": s.contact_email or None, "address": s.contact_address or None}


@public.get("/demo-slots")
def slots() -> dict:
    return {"slots": gs.demo_slots()}


@public.get("/ref/{code}")
def ref(code: str) -> dict:
    name = gs.referrer_name(code)
    if not name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown code")
    return {"restaurant": name}


# ------------------------------------------------------------------ your Leads page

def _out(ld: Lead) -> dict:
    return {"id": str(ld.id), "name": ld.name, "phone": ld.phone10, "restaurant": ld.restaurant, "city": ld.city, "outlets": ld.outlets,
            "uses_now": ld.uses_now, "source": ld.source, "campaign": (ld.utm or {}).get("utm_campaign"), "ref_code": ld.ref_code,
            "consent": ld.consent, "status": ld.status, "demo_at": ld.demo_at.isoformat() if ld.demo_at else None, "notes": ld.notes,
            "step": ld.step, "next_at": ld.next_at.isoformat() if ld.next_at else None, "stopped": ld.stopped,
            "tenant_id": str(ld.tenant_id) if ld.tenant_id else None, "created_at": ld.created_at.isoformat()}


@router.get("/platform/leads")
def leads(state: str | None = None, q: str | None = None, _adm: str = Depends(require_admin)) -> dict:
    with system_session() as sdb:
        stmt = select(Lead)
        if state:
            stmt = stmt.where(Lead.status == state)
        if q:
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(func.lower(Lead.name).like(like) | func.lower(func.coalesce(Lead.restaurant, "")).like(like) | Lead.phone10.like(like))
        rows = [_out(x) for x in sdb.scalars(stmt.order_by(Lead.created_at.desc()).limit(500))]
        funnel = dict(sdb.execute(select(Lead.status, func.count()).group_by(Lead.status)).all())
        sources: dict = {}
        for src, n in sdb.execute(select(Lead.source, func.count()).group_by(Lead.source)).all():
            sources[src or "other"] = sources.get(src or "other", 0) + n
        refs = sdb.execute(select(Referral.status, func.count()).group_by(Referral.status)).all()
    return {"leads": rows, "funnel": funnel, "sources": sources, "referrals": dict(refs)}


class LeadEdit(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(new|talking|demo|trial|paid|lost)$")
    notes: str | None = Field(default=None, max_length=2000)
    demo_at: datetime | None = None
    stop: bool | None = None


@router.patch("/platform/leads/{lead_id}")
def edit_lead(lead_id: uuid.UUID, body: LeadEdit, _adm: str = Depends(require_admin)) -> dict:
    with system_session() as sdb:
        ld = sdb.get(Lead, lead_id)
        if ld is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        if body.status:
            ld.status = body.status
            if body.status in ("trial", "paid", "lost"):
                ld.next_at = None
        if body.notes is not None:
            ld.notes = body.notes
        if body.demo_at is not None:
            ld.demo_at = body.demo_at
        if body.stop:
            ld.stopped, ld.next_at = True, None
        return _out(ld)


class LeadAdd(LeadIn):
    source: str | None = Field(default="walk-in", max_length=40)


@router.post("/platform/leads", status_code=201)
def add_lead(body: LeadAdd, background: BackgroundTasks, _adm: str = Depends(require_admin)) -> dict:
    """A restaurant you met: added by hand (follow-ups only if they agreed to WhatsApp)."""
    try:
        ld, new = gs.save_lead(body.model_dump(exclude={"website"}))
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    if new and ld.consent:
        background.add_task(gs.welcome, ld)
    return _out(ld)


# ------------------------------------------------------------------ a restaurant's referral link

@router.get("/growth/referral")
def my_referral(auth: Auth = Depends(get_auth)) -> dict:
    return gs.referral_info(auth.tenant_id)
