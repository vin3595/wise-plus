"""wise-plus: a restaurant connecting its own WhatsApp number (Meta Embedded Signup, once you're a Tech Provider).

The app opens Meta's sign-up popup (WISE_WA_APP_ID + WISE_WA_ES_CONFIG_ID); it hands back a code, the new number's
id and its WhatsApp Business Account. Here: code → business token, the number registered for the Cloud API, our app
subscribed to its webhooks, and our message templates copied onto their account. Until then they use the shared number."""
from __future__ import annotations

import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import Auth, get_auth, get_db
from . import numbers
from .models import WaNumber

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])
G = "https://graph.facebook.com"


def _owner(auth: Auth) -> None:
    if auth.role.value != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can change the WhatsApp number")


@router.get("/connection")
def connection(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    s = get_settings()
    n = db.get(WaNumber, auth.tenant_id)
    own = n is not None and n.status == "active"
    return {"mode": "own" if own else "shared", "display_number": (n.display_number if own else s.wa_display_number) or None,
            "how": n.how if own else None, "webhook_url": f"{s.public_base_url.rstrip('/')}/api/whatsapp/webhook",
            "can_connect": bool(s.wa_app_id and s.wa_es_config_id and s.wa_app_secret),
            "app_id": s.wa_app_id or None, "config_id": s.wa_es_config_id or None, "graph_version": s.wa_graph_version}


class ConnectIn(BaseModel):
    code: str = Field(min_length=10, max_length=2000)
    phone_number_id: str = Field(pattern=r"^\d{6,25}$")
    waba_id: str = Field(pattern=r"^\d{6,25}$")


def _g(method: str, path: str, token: str | None = None, **kw) -> dict:
    s = get_settings()
    r = httpx.request(method, f"{G}/{s.wa_graph_version}/{path.lstrip('/')}", timeout=20,
                      headers={"Authorization": f"Bearer {token}"} if token else None, **kw)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 300:
        from .client import reason
        e = data.get("error") or {}
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"WhatsApp said: {reason(e.get('code'), e.get('message') or '')}")
    return data


def copy_templates(waba_id: str, token: str) -> list[str]:
    """Our approved templates (bill, update, broadcasts, sign-in code) created on their account, same wording."""
    s = get_settings()
    if not (s.wa_business_id and s.wa_token):
        return []
    want = {t for t in (s.wa_bill_template, s.wa_update_template, s.wa_promo_template, s.wa_promo_offer_template,
                        s.wa_otp_template, s.wa_nightly_template) if t}
    ours = _g("GET", f"{s.wa_business_id}/message_templates", s.wa_token, params={"limit": 100,
              "fields": "name,language,category,components,status"}).get("data", [])
    made = []
    for t in ours:
        if t.get("name") not in want or t.get("status") != "APPROVED":
            continue
        comps = []
        for c in t.get("components") or []:
            c = {k: v for k, v in c.items() if k != "id"}
            comps.append(c)
        try:
            _g("POST", f"{waba_id}/message_templates", token, json={"name": t["name"], "language": t["language"],
                                                                   "category": t["category"], "components": comps})
            made.append(t["name"])
        except HTTPException:
            continue                                  # already there, or Meta wants a change: they still work on the shared one
    return made


@router.post("/connect")
def connect(body: ConnectIn, auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    _owner(auth)
    s = get_settings()
    if not (s.wa_app_id and s.wa_app_secret):
        raise HTTPException(status.HTTP_409_CONFLICT, "Connecting your own number isn't switched on yet")
    tok = _g("GET", "oauth/access_token", params={"client_id": s.wa_app_id, "client_secret": s.wa_app_secret, "code": body.code}).get("access_token")
    if not tok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "WhatsApp didn't give access. Try connecting again.")
    _g("POST", f"{body.phone_number_id}/register", tok, json={"messaging_product": "whatsapp", "pin": f"{secrets.randbelow(10**6):06d}"})
    _g("POST", f"{body.waba_id}/subscribed_apps", tok)
    info = _g("GET", body.phone_number_id, tok, params={"fields": "display_phone_number,verified_name"})
    numbers.save(auth.tenant_id, body.phone_number_id, tok, body.waba_id, info.get("display_phone_number"))
    made = copy_templates(body.waba_id, tok)
    from ..services import audit
    audit.record(db, auth.tenant_id, auth.user_id, "connect", "whatsapp", None,
                 after={"number": info.get("display_phone_number"), "templates": made})
    return {"mode": "own", "display_number": info.get("display_phone_number"), "name": info.get("verified_name"), "templates": made}


class ManualIn(BaseModel):
    phone_number_id: str = Field(pattern=r"^\d{6,25}$")
    token: str = Field(min_length=20, max_length=1000)
    waba_id: str | None = Field(default=None, pattern=r"^\d{6,25}$")
    app_secret: str | None = Field(default=None, min_length=16, max_length=100)


def manual_connect(tenant_id, body: ManualIn) -> dict:
    """Their own WhatsApp API, set up by hand (by them, or by you for them): checked with Meta before it's saved."""
    info = _g("GET", body.phone_number_id, body.token, params={"fields": "display_phone_number,verified_name,quality_rating"})
    numbers.save(tenant_id, body.phone_number_id, body.token, body.waba_id, info.get("display_phone_number"), body.app_secret, "manual")
    made = copy_templates(body.waba_id, body.token) if body.waba_id else []
    s = get_settings()
    return {"mode": "own", "display_number": info.get("display_phone_number"), "name": info.get("verified_name"), "templates": made,
            "webhook_url": f"{s.public_base_url.rstrip('/')}/api/whatsapp/webhook", "verify_token": s.wa_verify_token or None}


@router.post("/connect-manual")
def connect_manual(body: ManualIn, auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    _owner(auth)
    out = manual_connect(auth.tenant_id, body)
    from ..services import audit
    audit.record(db, auth.tenant_id, auth.user_id, "connect", "whatsapp", None, after={"number": out["display_number"], "how": "manual"})
    return out


@router.post("/disconnect")
def disconnect(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)) -> dict:
    _owner(auth)
    numbers.remove(auth.tenant_id)
    return {"mode": "shared"}
