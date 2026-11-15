"""wise-plus: a business signs up for only the services it needs, and can switch more on later.

    pos     POS billing: orders, bills, kitchen display, cash, day close, QR ordering, loyalty
    stock   Inventory: stock, recipes, counts, transfers, spot checks, menu profit
    bills   Bill scan and supplier dues: photo or WhatsApp a bill, it lands in Purchases
    sheets  Daily cash sheet: photo of the sheet, read and saved

Tenant.modules = None means everything (businesses from before modules existed).
The WhatsApp assistant, menu, reports and settings are always there.
"""
from __future__ import annotations

import time
import uuid

import jwt
from fastapi import Header, HTTPException, status
from sqlalchemy import select

MODULES = {
    "pos": "POS billing",
    "stock": "Inventory",
    "bills": "Bill scan & supplier dues",
    "sheets": "Daily cash sheet",
}
ALL = list(MODULES)
_cache: dict[str, tuple[float, list[str]]] = {}


def enabled(tenant_id: uuid.UUID | str) -> list[str]:
    key = str(tenant_id)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    from .db import system_session
    from .models import Tenant
    with system_session() as db:
        mods = db.scalar(select(Tenant.modules).where(Tenant.id == uuid.UUID(key)))
    out = [m for m in (mods if mods is not None else ALL) if m in MODULES]
    _cache[key] = (time.monotonic() + 30, out)
    return out


def forget(tenant_id: uuid.UUID | str) -> None:
    _cache.pop(str(tenant_id), None)


def clean(mods: list[str]) -> list[str]:
    out = [m for m in ALL if m in set(mods)]
    if not out:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pick at least one service")
    return out


def guard(*need: str):
    """Router dependency: the request's business must have one of these services on.
    Requests without a sign-in pass through (the endpoint does its own checks: public QR menu, webhooks)."""
    def check(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return
        from .config import get_settings
        from .core.security import decode_token
        try:
            tid = decode_token(authorization.split(" ", 1)[1], get_settings().jwt_secret)["tid"]
        except (jwt.PyJWTError, KeyError, ValueError):
            return
        if not set(need) & set(enabled(tid)):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"{MODULES[need[0]]} isn't switched on. The owner can turn it on in Settings › Services.")
    return check
