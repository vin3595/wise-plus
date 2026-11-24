"""Lock out repeated failed PIN attempts per outlet device."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AuthThrottle


def _now() -> datetime:
    return datetime.now(timezone.utc)


def locked_minutes_left(db: Session, key: str) -> int:
    row = db.get(AuthThrottle, key)
    if row and row.locked_until and row.locked_until > _now():
        return max(1, int((row.locked_until - _now()).total_seconds() // 60) + 1)
    return 0


def record_failure(db: Session, key: str) -> None:
    """Every pin_max_failures misses lock it; each lock in a row lasts twice as long (10, 20, 40 min… up to a day).
    The count only resets after a correct PIN, or a day after the last miss."""
    s = get_settings()
    row = db.get(AuthThrottle, key, with_for_update=True)
    if row is None:
        row = AuthThrottle(key=key, failures=0)
        db.add(row)
    if row.updated_at and row.updated_at < _now() - timedelta(days=1):
        row.failures = 0
    row.failures += 1
    row.updated_at = _now()
    if row.failures % s.pin_max_failures == 0:
        locks = row.failures // s.pin_max_failures
        row.locked_until = _now() + timedelta(minutes=min(s.pin_lock_minutes * 2 ** (locks - 1), 24 * 60))


def reset(db: Session, key: str) -> None:
    row = db.get(AuthThrottle, key)
    if row is not None:
        db.delete(row)


def record_wide(db: Session, key: str, limit: int = 50) -> None:
    """Misses on one outlet from every address: past `limit` within an hour, the outlet's PIN sign-in pauses 15 min."""
    row = db.get(AuthThrottle, key, with_for_update=True)
    if row is None:
        row = AuthThrottle(key=key, failures=0)
        db.add(row)
    if row.updated_at and row.updated_at < _now() - timedelta(hours=1):
        row.failures = 0
    row.failures += 1
    row.updated_at = _now()
    if row.failures >= limit:
        row.failures = 0
        row.locked_until = _now() + timedelta(minutes=15)
