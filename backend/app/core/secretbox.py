"""Restaurant secrets kept encrypted in the database (payment and SMS keys).

EncryptedText is a column type: the app reads and writes plain values, the database only ever holds
Fernet ciphertext (key derived from the server's JWT secret, kept outside the database). A value saved
before encryption was switched on is still read correctly until migration 0029 rewrites it."""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

PREFIX = "gAAAA"          # every Fernet token starts like this


@lru_cache(maxsize=4)
def _fernet(secret: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("tenant-secrets:" + secret).encode()).digest()))


def _f():
    from ..config import get_settings
    return _fernet(get_settings().jwt_secret)


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    if value.startswith(PREFIX):
        try:
            _f().decrypt(value.encode())
            return value                       # already sealed
        except Exception:  # noqa: BLE001
            pass
    return _f().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value or not value.startswith(PREFIX):
        return value                           # empty, or saved before encryption
    try:
        return _f().decrypt(value.encode()).decode()
    except Exception:  # noqa: BLE001 - the server's secret changed: the restaurant enters the key again
        return None


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
