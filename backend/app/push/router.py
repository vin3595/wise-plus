"""Turn phone notifications on/off for the signed-in person, and send a test."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..deps import live_check, Auth, get_auth, get_db
from . import service
from .models import PushSubscription

router = APIRouter(prefix="/push", tags=["push"])


class SubIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=4, max_length=100)


@router.get("/key", dependencies=[Depends(live_check)])
def public_key(_auth: Auth = Depends(get_auth)) -> dict:
    return {"key": service.keys()["public"], "for_role": _auth.role.value in service.ROLES}


@router.post("/subscribe")
def subscribe(body: SubIn, auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    if not body.endpoint.startswith("https://"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Not a push address")
    db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    db.add(PushSubscription(tenant_id=auth.tenant_id, user_id=auth.user_id, endpoint=body.endpoint, p256dh=body.p256dh, auth=body.auth))
    db.flush()
    return {"ok": True}


@router.post("/unsubscribe")
def unsubscribe(body: dict, auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    db.execute(delete(PushSubscription).where(PushSubscription.endpoint == str(body.get("endpoint") or ""),
                                              PushSubscription.user_id == auth.user_id))
    return {"ok": True}


@router.post("/test")
def test(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    subs = db.scalars(select(PushSubscription).where(PushSubscription.user_id == auth.user_id)).all()
    if not subs:
        raise HTTPException(status.HTTP_409_CONFLICT, "Turn notifications on first")
    ok = sum(service.send_one(s.endpoint, s.p256dh, s.auth, {"title": "Ease My Bill", "body": "Notifications are on ✓", "url": "/"}) for s in subs)
    return {"sent": ok}
