"""wise-plus: restaurants' own WhatsApp numbers. A restaurant without one sends from the shared number.

Connecting one (Meta Embedded Signup, once you're a Tech Provider) ends in save(): the number's id, its WABA and a
business token. The token is kept encrypted with the server's secret."""
from __future__ import annotations

import base64
import hashlib
import uuid
from functools import lru_cache

from sqlalchemy import select

from ..config import get_settings
from ..db import system_session
from .models import WaNumber


@lru_cache(maxsize=1)
def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(("wa-numbers:" + get_settings().jwt_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def unseal(blob: str | None) -> str | None:
    if not blob:
        return None
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except Exception:  # noqa: BLE001 - the server's secret changed: the restaurant has to connect again
        return None


def sender_for(via: str | None = None, tenant_id: str | None = None) -> tuple[str, str] | None:
    """The number to send from: the one the message came in on (a restaurant's own), else the restaurant's own."""
    with system_session() as db:
        n = None
        if via:
            n = db.scalar(select(WaNumber).where(WaNumber.phone_number_id == via, WaNumber.status == "active"))
        if n is None and tenant_id:
            n = db.scalar(select(WaNumber).where(WaNumber.tenant_id == uuid.UUID(str(tenant_id)), WaNumber.status == "active"))
        if n is None:
            return None
        tok = unseal(n.token_enc)
        return (n.phone_number_id, tok) if tok else None


def tenant_of(phone_number_id: str | None) -> uuid.UUID | None:
    """Which restaurant a number belongs to (None: the shared number)."""
    if not phone_number_id or phone_number_id == get_settings().wa_phone_number_id:
        return None
    with system_session() as db:
        return db.scalar(select(WaNumber.tenant_id).where(WaNumber.phone_number_id == phone_number_id))


def save(tenant_id, phone_number_id: str, token: str, waba_id: str | None = None, display: str | None = None,
         app_secret: str | None = None, how: str = "signup") -> None:
    with system_session() as db:
        n = db.get(WaNumber, uuid.UUID(str(tenant_id)))
        if n is None:
            n = WaNumber(tenant_id=uuid.UUID(str(tenant_id)), phone_number_id=phone_number_id)
            db.add(n)
        n.phone_number_id, n.waba_id, n.display_number, n.token_enc, n.status = phone_number_id, waba_id, display, seal(token), "active"
        n.app_secret_enc, n.how = (seal(app_secret) if app_secret else None), how


def webhook_secrets() -> list[str]:
    """Meta app secrets of restaurants set up with their own app (their webhooks come to us too)."""
    with system_session() as db:
        return [x for x in (unseal(b) for (b,) in db.execute(select(WaNumber.app_secret_enc).where(
            WaNumber.status == "active", WaNumber.app_secret_enc.is_not(None)))) if x]


def remove(tenant_id) -> None:
    with system_session() as db:
        n = db.get(WaNumber, uuid.UUID(str(tenant_id)))
        if n is not None:
            n.status, n.token_enc = "removed", None
