"""Request limits per client address, shared by every API worker (kept in the auth_throttle table).

The real client address: Caddy (the public edge) does not trust an X-Forwarded-For sent by the client, so it
sets the header to the client's address only; nginx then appends Caddy's. So the API sees
"client, caddy" and the client is the second-to-last entry. A client can't forge it.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import delete

from ..db import system_session
from ..models import AuthThrottle


def client_ip(request: Request) -> str:
    parts = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-2]
    if parts:
        return parts[0]
    return request.client.host if request.client else "?"


def hit(request: Request, name: str, limit: int, per_seconds: int, msg: str = "Too many tries. Please wait a few minutes.") -> None:
    """Count one request from this address for `name`; refuse (429) past `limit` in the window."""
    from ..config import get_settings
    if not get_settings().rate_limits:
        return
    ip = client_ip(request)
    now = datetime.now(timezone.utc)
    window = int(now.timestamp()) // per_seconds
    key = f"rl:{name}:{ip}:{window}"[:200]
    with system_session() as db:
        row = db.get(AuthThrottle, key, with_for_update=True)
        if row is None:
            row = AuthThrottle(key=key, failures=0)
            db.add(row)
        row.failures += 1
        row.updated_at = now
        over = row.failures > limit
        if random.random() < 0.02:            # tidy old windows now and then
            db.execute(delete(AuthThrottle).where(AuthThrottle.key.like("rl:%"), AuthThrottle.updated_at < now - timedelta(days=2)))
    if over:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg)
