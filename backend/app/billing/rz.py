"""Razorpay Subscriptions and Orders for wise-plus billing (plain HTTP; one place to fake in tests)."""
from __future__ import annotations

import hashlib
import hmac

import httpx

from ..config import get_settings

API = "https://api.razorpay.com/v1"


NAMES = ("key_id", "key_secret", "webhook_secret")


def keys() -> dict[str, str]:
    """Keys pasted in the app (Billing › Your Razorpay) win over the server file."""
    s = get_settings()
    out = {"key_id": s.billing_rz_key_id, "key_secret": s.billing_rz_key_secret, "webhook_secret": s.billing_rz_webhook_secret}
    try:
        from sqlalchemy import select
        from ..db import system_session
        from ..whatsapp.numbers import unseal
        from .models import PlatformSecret
        with system_session() as db:
            for row in db.scalars(select(PlatformSecret).where(PlatformSecret.key.in_([f"rz_{n}" for n in NAMES]))):
                v = unseal(row.value_enc)
                if v:
                    out[row.key[3:]] = v
    except Exception:  # noqa: BLE001 - table not there yet: the server file still works
        pass
    return out


def save_keys(values: dict[str, str]) -> None:
    from ..db import system_session
    from ..whatsapp.numbers import seal
    from .models import PlatformSecret
    with system_session() as db:
        for n, v in values.items():
            row = db.get(PlatformSecret, f"rz_{n}")
            if row:
                row.value_enc = seal(v)
            else:
                db.add(PlatformSecret(key=f"rz_{n}", value_enc=seal(v)))


def configured() -> bool:
    k = keys()
    return bool(k["key_id"] and k["key_secret"])


def call(method: str, path: str, body: dict | None = None, auth: tuple[str, str] | None = None) -> dict:
    k = keys()
    r = httpx.request(method, API + path, json=body, auth=auth or (k["key_id"], k["key_secret"]), timeout=15)
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["description"]
        except Exception:  # noqa: BLE001
            msg = r.text[:200]
        raise RuntimeError(f"Razorpay: {msg}")
    return r.json()


def hmac_ok(message: bytes, signature: str | None, secret: str | None) -> bool:
    if not signature or not secret:
        return False
    return hmac.compare_digest(hmac.new(secret.encode(), message, hashlib.sha256).hexdigest(), signature.strip())
