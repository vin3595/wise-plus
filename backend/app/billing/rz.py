"""Razorpay Subscriptions and Orders for wise-plus billing (plain HTTP; one place to fake in tests)."""
from __future__ import annotations

import hashlib
import hmac

import httpx

from ..config import get_settings

API = "https://api.razorpay.com/v1"


def configured() -> bool:
    s = get_settings()
    return bool(s.billing_rz_key_id and s.billing_rz_key_secret)


def call(method: str, path: str, body: dict | None = None) -> dict:
    s = get_settings()
    r = httpx.request(method, API + path, json=body, auth=(s.billing_rz_key_id, s.billing_rz_key_secret), timeout=15)
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
