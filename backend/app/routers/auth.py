"""Signup (new restaurant), owner/manager OTP login, staff PIN login."""
from __future__ import annotations

import logging

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import get_settings
from ..core.contact import mask_identifier, normalize_identifier
from ..core.login_id import normalize_login_id, validate_login_id, new_login_code
from ..core.permissions import OTP_ROLES, Role
from ..core.security import pin_lookup_key, verify_secret
from ..db import system_session
from ..models import Brand, Outlet, TaxGroup, Tenant, User, UserOutlet
from ..schemas import GoogleLogin, GoogleSignup, OtpRequest, OtpSent, OtpVerify, PinLogin, SignupStart, SignupVerify, TokenOut
from ..modules import clean as clean_modules
from ..core import ratelimit
from ..services import audit, otp, throttle
from ..services.first_outlet import first_outlet
from .common import issue_token


router = APIRouter(prefix="/auth", tags=["auth"])


def _send_limit(request: Request) -> None:
    """Codes cost money to send: a few per address every 15 minutes, and a daily ceiling."""
    ratelimit.hit(request, "send", 8, 900)
    ratelimit.hit(request, "send-day", 30, 86400, "Too many codes from here today. Try again tomorrow.")


def _dev_code(code: str) -> str | None:
    # Only in local development. The Wise Box uses provider "box": the code goes to the box's log
    # (sudo wise login-code) and is never sent back to the browser.
    return code if get_settings().otp_provider == "console" else None


def _check_signup_open(db) -> None:
    """signup_mode: open (hosted SaaS) | first (Wise Box: only the first restaurant) | closed."""
    mode = get_settings().signup_mode
    if mode == "closed" or (mode == "first" and db.scalar(select(Tenant.id).limit(1)) is not None):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign-up is closed on this Wise Box")


@router.get("/login-id-available")
def login_id_available(login_id: str) -> dict:
    try:
        value = validate_login_id(login_id)
    except ValueError as e:
        return {"available": False, "reason": str(e)}
    with system_session() as db:
        taken = db.scalar(select(Tenant.id).where(Tenant.login_id == value)) is not None
    return {"available": not taken, "reason": "That login ID is taken" if taken else None}


@router.post("/signup/start", response_model=OtpSent)
def signup_start(body: SignupStart, request: Request) -> OtpSent:
    _send_limit(request)
    error = None
    with system_session() as db:
        _check_signup_open(db)
        if body.login_id and db.scalar(select(Tenant.id).where(Tenant.login_id == body.login_id)) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "That login ID is taken")
        try:
            code = otp.issue(db, body.email if body.via == "email" else body.phone, "signup")
        except otp.OtpError as e:
            error = e
    if error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(error))
    return OtpSent(sent_to=mask_identifier(body.via, body.email if body.via == "email" else body.phone), dev_otp=_dev_code(code))


@router.post("/signup/verify", response_model=TokenOut)
def signup_verify(body: SignupVerify, request: Request) -> TokenOut:
    ratelimit.hit(request, "verify", 30, 900)
    s = get_settings()
    error = None
    result = None
    with system_session() as db:
        _check_signup_open(db)
        try:
            otp.verify(db, body.email if body.via == "email" else body.phone, "signup", body.otp)
        except otp.OtpError as e:
            error = e  # keep the session committing so the failed attempt is counted
        if error is None:
            login_id = body.login_id
            if login_id and db.scalar(select(Tenant.id).where(Tenant.login_id == login_id)) is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "That login ID was just taken")
            tenant, owner = _create_business(db, login_id, body.restaurant_name, body.owner_name, body.email, body.phone, body.modules)
            login_id = tenant.login_id
            result = issue_token(db, owner, tenant)
            new_id = login_id
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error))
    assert result is not None
    try:                                    # wise-plus growth: the lead becomes a trial; referral noted
        from ..growth.service import on_signup
        with system_session() as db:
            tid = db.scalar(select(Tenant.id).where(Tenant.login_id == new_id))
        if tid:
            on_signup(tid, body.phone, body.ref)
    except Exception:  # noqa: BLE001 - never block a sign-up on this
        logging.getLogger("wise.growth").exception("growth: sign-up hook")
    return result


def _create_business(db, login_id: str | None, name: str, owner_name: str, email: str, phone: str,
                     modules: list[str] | None) -> tuple[Tenant, User]:
    """A new restaurant / business with its owner (sign-up by code or with Google)."""
    s = get_settings()
    while not login_id:
        c = new_login_code()
        if db.scalar(select(Tenant.id).where(Tenant.login_id == c)) is None:
            login_id = c
    tenant = Tenant(login_id=login_id, name=name, owner_email=email, owner_phone=phone, app_name=name,
                    trial_ends_at=datetime.now(timezone.utc) + timedelta(days=s.trial_days),
                    modules=clean_modules(modules, login_id) if modules is not None else None)
    db.add(tenant)
    db.flush()
    brand = Brand(tenant_id=tenant.id, name=name)
    db.add(brand)
    db.add(TaxGroup(tenant_id=tenant.id, name="GST 5%", cgst_bp=250, sgst_bp=250, is_default=True))
    owner = User(tenant_id=tenant.id, name=owner_name, email=email, phone=phone,
                 role=Role.owner.value, last_login_at=datetime.now(timezone.utc))
    db.add(owner)
    db.flush()
    if tenant.modules is not None and "pos" not in tenant.modules:
        first_outlet(db, tenant, brand)   # wise-plus: stock / bills / sheets work straight away
    audit.record(db, tenant.id, owner.id, "signup", "tenant", tenant.id, after=audit.snapshot(tenant))
    return tenant, owner


def _accounts(db, kind: str, ident: str) -> list[tuple[Tenant, User]]:
    """Every business where this email / mobile is an active owner or manager."""
    col = User.email if kind == "email" else User.phone
    rows = db.execute(select(Tenant, User).join(User, User.tenant_id == Tenant.id)
                      .where(col == ident, User.is_active, Tenant.status != "suspended")).all()
    return [(t, u) for t, u in rows if Role(u.role) in OTP_ROLES]


def _find_tenant(db, login_id: str | None, kind: str | None = None, ident: str | None = None) -> Tenant | None:
    if login_id:
        return db.scalar(select(Tenant).where(Tenant.login_id == normalize_login_id(login_id)))
    found = _accounts(db, kind, ident) if ident else []
    if len(found) > 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "This runs more than one business. Type the business login ID too.")
    return found[0][0] if found else None


@router.post("/otp/request", response_model=OtpSent)
def otp_request(body: OtpRequest, request: Request) -> OtpSent:
    _send_limit(request)
    try:
        kind, ident = normalize_identifier(body.identifier)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    error = None
    code = None
    with system_session() as db:
        tenant = _find_tenant(db, body.login_id, kind, ident)
        user = None
        if tenant is not None:
            col = User.email if kind == "email" else User.phone
            user = db.scalar(select(User).where(User.tenant_id == tenant.id, col == ident, User.is_active))
        # Same response whether or not the account exists, so login IDs/numbers can't be probed.
        if tenant is not None and user is not None and Role(user.role) in OTP_ROLES \
                and tenant.status != "suspended":
            try:
                code = otp.issue(db, f"{tenant.id}:{ident}", "login")
            except otp.OtpError as e:
                error = e
    if error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(error))
    return OtpSent(sent_to=mask_identifier(kind, ident), dev_otp=_dev_code(code) if code else None)


@router.post("/otp/verify", response_model=TokenOut)
def otp_verify(body: OtpVerify, request: Request) -> TokenOut:
    ratelimit.hit(request, "verify", 30, 900)
    try:
        kind, ident = normalize_identifier(body.identifier)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    error = None
    result = None
    with system_session() as db:
        tenant = _find_tenant(db, body.login_id, kind, ident)
        if tenant is None:
            error = otp.OtpError("Wrong code")
        else:
            try:
                otp.verify(db, f"{tenant.id}:{ident}", "login", body.otp)
            except otp.OtpError as e:
                error = e
        if error is None and tenant is not None:
            col = User.email if kind == "email" else User.phone
            user = db.scalar(select(User).where(User.tenant_id == tenant.id, col == ident, User.is_active))
            if user is None or Role(user.role) not in OTP_ROLES:
                error = otp.OtpError("Account not found")
            else:
                user.last_login_at = datetime.now(timezone.utc)
                audit.record(db, tenant.id, user.id, "login_otp", "user", user.id)
                result = issue_token(db, user, tenant)
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error))
    assert result is not None
    return result


# ---------------------------------------------------------------- Sign in with Google

@router.get("/google")
def google_config() -> dict:
    return {"client_id": get_settings().google_client_id or None}


def _google_email(credential: str) -> tuple[str, str]:
    """Check Google's ID token (signature, expiry and audience, by Google's tokeninfo) -> (email, name)."""
    import httpx
    s = get_settings()
    if not s.google_client_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Sign in with Google isn't switched on yet")
    try:
        r = httpx.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": credential}, timeout=10)
        info = r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't reach Google. Try again.") from None
    if info.get("aud") != s.google_client_id or info.get("iss") not in ("accounts.google.com", "https://accounts.google.com") \
            or str(info.get("email_verified")).lower() != "true" or not info.get("email"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google sign-in didn't work. Try again.")
    return normalize_identifier(info["email"])[1], str(info.get("name") or "")[:120]


@router.post("/google", response_model=TokenOut)
def google_login(body: GoogleLogin, request: Request) -> TokenOut:
    ratelimit.hit(request, "google", 20, 900)
    email, _ = _google_email(body.credential)
    with system_session() as db:
        found = _accounts(db, "email", email)
        if body.login_id:
            found = [(t, u) for t, u in found if t.login_id == normalize_login_id(body.login_id)]
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No business uses {email} yet. Create your account first.")
        if len(found) > 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "choose:" + ",".join(f"{t.login_id}={t.name}" for t, _ in found))
        tenant, user = found[0]
        user.last_login_at = datetime.now(timezone.utc)
        audit.record(db, tenant.id, user.id, "login_google", "user", user.id)
        return issue_token(db, user, tenant)


@router.post("/signup/google", response_model=TokenOut)
def google_signup(body: GoogleSignup, request: Request) -> TokenOut:
    ratelimit.hit(request, "google", 20, 900)
    ratelimit.hit(request, "signup-day", 10, 86400, "Too many new accounts from here today. Try again tomorrow.")
    email, gname = _google_email(body.credential)
    with system_session() as db:
        _check_signup_open(db)
        tenant, owner = _create_business(db, None, body.restaurant_name, (body.owner_name or gname or body.restaurant_name)[:120],
                                         email, body.phone, body.modules)
        result = issue_token(db, owner, tenant)
        tid = tenant.id
    try:
        from ..growth.service import on_signup
        on_signup(tid, body.phone, body.ref)
    except Exception:  # noqa: BLE001
        logging.getLogger("wise.growth").exception("growth: sign-up hook")
    return result


@router.post("/pin", response_model=TokenOut)
def pin_login(body: PinLogin) -> TokenOut:
    s = get_settings()
    error: tuple[int, str] | None = None
    result = None
    with system_session() as db:
        tenant = _find_tenant(db, body.login_id)
        outlet = db.get(Outlet, body.outlet_id) if tenant else None
        if tenant is None or outlet is None or outlet.tenant_id != tenant.id or not outlet.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Outlet not found")
        key = f"pin:{tenant.id}:{outlet.id}"
        mins = throttle.locked_minutes_left(db, key)
        if mins:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"Too many wrong PINs. Try again in {mins} min.")
        user = db.scalar(select(User).where(
            User.tenant_id == tenant.id,
            User.pin_lookup == pin_lookup_key(body.pin, str(tenant.id), s.jwt_secret),
            User.is_active,
        ))
        assigned = user is not None and (
            user.role == Role.owner.value
            or db.get(UserOutlet, {"user_id": user.id, "outlet_id": outlet.id}) is not None
        )
        if not assigned or not verify_secret(body.pin, user.pin_hash):
            throttle.record_failure(db, key)
            error = (status.HTTP_401_UNAUTHORIZED, "Wrong PIN")
        elif tenant.status == "suspended":
            error = (status.HTTP_403_FORBIDDEN, "This restaurant's account is suspended")
        else:
            throttle.reset(db, key)
            user.last_login_at = datetime.now(timezone.utc)
            audit.record(db, tenant.id, user.id, "login_pin", "user", user.id, after={"outlet_id": str(outlet.id)})
            result = issue_token(db, user, tenant, outlet_id=outlet.id)
    if error:
        raise HTTPException(*error)
    assert result is not None
    return result


class ScreenLogin(BaseModel):
    key: str = Field(min_length=10, max_length=40)


@router.post("/kitchen-screen", response_model=TokenOut)
def kitchen_screen_login(body: ScreenLogin) -> TokenOut:
    """The kitchen display signs in with its outlet's kitchen link: no PIN, no daily login.
    It acts as the outlet's 'Kitchen screen' (kitchen role permissions, this outlet only)."""
    with system_session() as db:
        outlet = db.scalar(select(Outlet).where(Outlet.kitchen_key == body.key, Outlet.is_active))
        tenant = db.get(Tenant, outlet.tenant_id) if outlet else None
        if outlet is None or tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "This kitchen link isn't valid. Ask the manager for the outlet's kitchen link.")
        if tenant.status == "suspended":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This restaurant's account is suspended")
        user = db.scalar(select(User).join(UserOutlet, UserOutlet.user_id == User.id)
                         .where(User.tenant_id == tenant.id, User.is_screen, User.is_active, UserOutlet.outlet_id == outlet.id))
        if user is None:
            user = User(tenant_id=tenant.id, name=f"Kitchen screen ({outlet.name})"[:120], role=Role.kitchen.value, is_screen=True)
            db.add(user)
            db.flush()
            db.add(UserOutlet(user_id=user.id, outlet_id=outlet.id, tenant_id=tenant.id))
            db.flush()
        user.last_login_at = datetime.now(timezone.utc)
        return issue_token(db, user, tenant, outlet_id=outlet.id)


@router.get("/outlets")
def outlets_for_device(login_id: str) -> list[dict]:
    """Outlet list for setting up a counter device. Names only; no sensitive data."""
    with system_session() as db:
        tenant = _find_tenant(db, login_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Restaurant not found")
        rows = db.execute(
            select(Outlet.id, Outlet.name).where(Outlet.tenant_id == tenant.id, Outlet.is_active)
            .order_by(Outlet.name)
        ).all()
    return [{"id": str(r.id), "name": r.name} for r in rows]

