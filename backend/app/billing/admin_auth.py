"""wise-plus: the Ease My Bill admin sign-in, separate from every restaurant (your own included).

Who: WISE_PLATFORM_ADMIN_PHONES. Until that's set, the owners of your own businesses (WISE_HOUSE_TENANTS) are let in,
so you're never locked out. Sign-in is a code on WhatsApp/SMS, like restaurants; the token carries no restaurant.
"""
from __future__ import annotations

import jwt
from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..config import get_settings
from ..core.security import create_token, decode_token
from ..db import system_session
from ..services import otp

router = APIRouter(prefix="/admin", tags=["admin"])
PURPOSE = "admin"


def admin_phones() -> set[str]:
    s = get_settings()
    phones = {p.strip()[-10:] for p in s.platform_admin_phones.split(",") if p.strip()}
    if phones:
        return phones
    from ..whatsapp.health import platform_phones
    with system_session() as db:
        return set(platform_phones(db))


def is_admin_phone(phone: str | None) -> bool:
    return bool(phone) and str(phone)[-10:] in admin_phones()


def require_admin(authorization: str | None = Header(default=None)) -> str:
    """The admin's phone, or 404 (nobody else learns these pages exist)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    try:
        c = decode_token(authorization.split(" ", 1)[1], get_settings().jwt_secret)
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, sign in again") from None
    phone = str(c.get("sub", ""))[6:] if c.get("adm") else ""
    if not is_admin_phone(phone):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return phone


class StartIn(BaseModel):
    phone: str = Field(min_length=10, max_length=15)


class VerifyIn(StartIn):
    code: str = Field(min_length=4, max_length=8)


def _ten(p: str) -> str:
    d = "".join(ch for ch in p if ch.isdigit())[-10:]
    if len(d) != 10:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A 10-digit mobile number")
    return d


@router.post("/login/start")
def start(body: StartIn, request: Request) -> dict:
    from ..core import ratelimit
    ratelimit.hit(request, "admin", 5, 900)
    phone = _ten(body.phone)
    if is_admin_phone(phone):           # same answer either way: no hint who the admin is
        with system_session() as db:
            try:
                otp.issue(db, f"admin:{phone}", PURPOSE)
            except otp.OtpError as e:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e)) from None
    return {"sent": True}


@router.post("/login/verify")
def verify(body: VerifyIn, request: Request) -> dict:
    from ..core import ratelimit
    ratelimit.hit(request, "admin-verify", 10, 900)
    phone = _ten(body.phone)
    if not is_admin_phone(phone):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Wrong code")
    with system_session() as db:
        try:
            otp.verify(db, f"admin:{phone}", PURPOSE, body.code.strip())
        except otp.OtpError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    token = create_token({"sub": f"admin:{phone}", "tid": "platform", "adm": True}, get_settings().jwt_secret, 12)
    return {"token": token, "phone": phone}


@router.get("/me")
def me(authorization: str | None = Header(default=None)) -> dict:
    return {"phone": require_admin(authorization)}


class LinkIn(BaseModel):
    code: str = Field(min_length=20, max_length=80)


@router.post("/login/link")
def login_link(body: LinkIn) -> dict:
    """One-time sign-in link made on the server (wiseplus admin-link): for when the admin phone can't get a code.
    Single use, 15 minutes."""
    import hashlib
    import hmac
    import time

    from sqlalchemy import text
    with system_session() as db:
        row = db.execute(text("select value_enc from platform_secrets where key = 'admin_link'")).first()
        db.execute(text("delete from platform_secrets where key = 'admin_link'"))
    if not row:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link has been used or has expired")
    try:
        digest, until = row[0].split(":")
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link has been used or has expired") from None
    ok = hmac.compare_digest(digest, hashlib.sha256(body.code.encode()).hexdigest()) and time.time() < float(until)
    phones = sorted(admin_phones())
    if not ok or not phones:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This link has been used or has expired")
    token = create_token({"sub": f"admin:{phones[0]}", "tid": "platform", "adm": True}, get_settings().jwt_secret, 12)
    return {"token": token, "phone": phones[0]}
