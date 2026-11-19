"""Sends phone notifications to the owner and managers of a business (Web Push, VAPID).

The key pair is made once and kept sealed in platform_secrets. Sending happens on a background thread
after the request, so a slow push service never slows billing. Phones that unsubscribed (404/410) are removed."""
from __future__ import annotations

import base64
import json
import logging
import threading
import uuid

from sqlalchemy import delete, select, text

from ..db import system_session

log = logging.getLogger("wise.push")
_KEYS: dict = {}
_LOCK = threading.Lock()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def keys() -> dict[str, str]:
    """{"public": applicationServerKey (base64url), "private_pem": ...} — made on first use."""
    if _KEYS:
        return _KEYS
    with _LOCK:
        if _KEYS:
            return _KEYS
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from ..whatsapp.numbers import seal, unseal
        with system_session() as db:
            row = db.execute(text("select value_enc from platform_secrets where key = 'push_vapid'")).first()
            if row:
                pem = unseal(row[0])
            else:
                k = ec.generate_private_key(ec.SECP256R1())
                pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()).decode()
                db.execute(text("insert into platform_secrets (key, value_enc) values ('push_vapid', :v) "
                                "on conflict (key) do nothing"), {"v": seal(pem)})
                pem = unseal(db.execute(text("select value_enc from platform_secrets where key = 'push_vapid'")).scalar_one())
        priv = serialization.load_pem_private_key(pem.encode(), password=None)
        pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        _KEYS.update({"public": _b64(pub), "private_pem": pem})
        return _KEYS


ROLES = ("owner", "manager")


def notify(tenant_id: uuid.UUID, title: str, body: str, url: str = "/", tag: str | None = None,
           roles: tuple[str, ...] = ROLES) -> None:
    """Queue a notification to this business's owner and managers (every phone they turned it on)."""
    threading.Thread(target=_send_all, args=(tenant_id, title, body, url, tag, roles), daemon=True).start()


def _send_all(tenant_id, title, body, url, tag, roles) -> None:
    try:
        from ..models import User
        from .models import PushSubscription as P
        with system_session() as db:
            subs = db.execute(select(P.id, P.endpoint, P.p256dh, P.auth).join(User, User.id == P.user_id)
                              .where(P.tenant_id == tenant_id, User.is_active, User.role.in_(roles))).all()
        if not subs:
            return
        gone = [sid for sid, ep, p, a in subs if not send_one(ep, p, a, {"title": title, "body": body, "url": url, "tag": tag})]
        if gone:
            with system_session() as db:
                db.execute(delete(P).where(P.id.in_(gone)))
    except Exception:  # noqa: BLE001 - a notification never breaks anything else
        log.exception("push: sending failed")


def send_one(endpoint: str, p256dh: str, auth: str, payload: dict) -> bool:
    """False when the phone has unsubscribed (the row should go)."""
    from pywebpush import WebPushException, webpush

    from ..config import get_settings
    if "vapid" not in _KEYS:
        from py_vapid import Vapid
        _KEYS["vapid"] = Vapid.from_pem(keys()["private_pem"].encode())
    try:
        webpush({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}, json.dumps(payload),
                vapid_private_key=_KEYS["vapid"], ttl=12 * 3600, timeout=10,
                vapid_claims={"sub": "mailto:" + (get_settings().contact_email or "easemybills@gmail.com")})
        return True
    except WebPushException as e:
        code = getattr(e.response, "status_code", None)
        if code in (404, 410):
            return False
        log.warning("push: %s %s", code, str(e)[:200])
        return True
