"""Activity log: who changed what, when — the owner and managers can see every change in their business.

Most changes already write a detailed entry (before → after) through services.audit. Anything else a
signed-in person changes (a write request that succeeds) is caught here and logged with what was sent,
so nothing slips through: prices, stock, expenses, bills, staff, settings. PINs, codes and keys are never kept."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .core.permissions import Perm
from .deps import Auth, get_db, require
from .models import AuditLog, Outlet, User

log = logging.getLogger("wise.activity")

# set per request by the middleware; services.audit.record() marks it so the request isn't logged twice
CURRENT: contextvars.ContextVar[list | None] = contextvars.ContextVar("wise_activity", default=None)

WRITE = {"POST", "PUT", "PATCH", "DELETE"}
SKIP = re.compile(r"^/api/(auth|push|admin|platform|public|whatsapp/webhook|assistant|activity|growth|qr/public|health)(/|$)")
SECRET = re.compile(r"pin|otp|password|passwd|token|secret|key|code_hash|auth", re.I)


def _clean(v, depth: int = 0):
    if depth > 4:
        return "…"
    if isinstance(v, dict):
        return {k: ("•••" if SECRET.search(str(k)) else _clean(x, depth + 1)) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return [_clean(x, depth + 1) for x in v[:30]]
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "…"
    return v


def _entity(path: str) -> tuple[str, str | None]:
    parts = [p for p in path.split("/")[2:] if p]          # drop "", "api"
    ids = [p for p in parts if re.fullmatch(r"[0-9a-fA-F-]{32,36}", p)]
    words = [p for p in parts if p not in ids]
    return ("_".join(words)[:40] or "app"), (ids[-1] if ids else None)


def install(app) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    class ActivityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method not in WRITE or not request.url.path.startswith("/api/") or SKIP.match(request.url.path):
                return await call_next(request)
            marks: list = []
            tok = CURRENT.set(marks)
            body = None
            if "json" in (request.headers.get("content-type") or ""):
                try:
                    raw = await request.body()
                    body = json.loads(raw) if raw and len(raw) < 20000 else None
                except Exception:  # noqa: BLE001
                    body = None
            try:
                resp = await call_next(request)
            finally:
                CURRENT.reset(tok)
            if resp.status_code < 400 and not marks:
                try:
                    from starlette.concurrency import run_in_threadpool
                    await run_in_threadpool(_log_request, request, body)
                except Exception:  # noqa: BLE001 - the log never breaks the change itself
                    log.exception("activity log")
            return resp

    app.add_middleware(ActivityMiddleware)


def _log_request(request: Request, body) -> None:
    from .core.security import decode_token
    from .db import tenant_session
    h = request.headers.get("authorization") or ""
    if not h.lower().startswith("bearer "):
        return
    try:
        c = decode_token(h.split(" ", 1)[1], get_settings().jwt_secret)
        tid, uid = uuid.UUID(c["tid"]), uuid.UUID(c["sub"])
    except Exception:  # noqa: BLE001 - admin tokens and expired ones aren't a business's change
        return
    entity, eid = _entity(request.url.path)
    action = {"POST": "save", "PUT": "update", "PATCH": "update", "DELETE": "delete"}[request.method]
    after = {"path": request.url.path}
    if request.url.query:
        after["query"] = request.url.query[:200]
    if body is not None:
        after["sent"] = _clean(body)
    with tenant_session(tid) as db:
        db.add(AuditLog(tenant_id=tid, actor_user_id=uid, action=action, entity=entity, entity_id=eid, after=after))


# ------------------------------------------------------------------ reading it

router = APIRouter(prefix="/activity", tags=["activity"])

LABEL = {
    "item": "Menu item", "variant": "Price", "category": "Menu category", "tax_group": "Tax", "addon_group": "Add-ons",
    "override": "Outlet price", "menu": "Menu", "item_image": "Item photo", "bill": "Bill", "order": "Order",
    "user": "Staff", "outlet": "Outlet", "tenant": "Business settings", "day": "Day close", "shift": "Cash shift",
    "cash": "Cash", "stock": "Stock", "purchase": "Purchase", "recipe": "Recipe", "expenses": "Expense",
    "payment": "Payment", "loyalty": "Loyalty", "offer": "Offer", "role": "Roles", "bank": "Bank",
}


def _money(k: str, v):
    if isinstance(v, int) and k.endswith("_paise"):
        return f"₹{v / 100:,.2f}".replace(".00", "")
    return v


def _flat(d, pre: str = "") -> dict:
    out: dict = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if k in ("updated_at", "created_at", "id", "tenant_id"):
                continue
            out.update(_flat(v, f"{pre}{k}."))
    elif isinstance(d, list) and d and all(isinstance(x, dict) for x in d):
        for i, x in enumerate(d):
            name = x.get("name") or x.get("label") or str(i + 1)
            out.update(_flat(x, f"{pre}{name}."))
    else:
        out[pre[:-1]] = d
    return out


def changes(before: dict | None, after: dict | None) -> list[str]:
    """'Regular · price: ₹120 → ₹140' lines for what changed."""
    if not before or not after or "path" in (after or {}):
        return []
    b, a = _flat(before), _flat(after)
    lines = []
    for k in sorted(set(b) | set(a)):
        if b.get(k) != a.get(k) and not SECRET.search(k):
            key = k.replace("_paise", "").replace("_", " ").replace(".", " · ")
            lines.append(f"{key}: {_money(k, b.get(k)) if k in b else '—'} → {_money(k, a.get(k)) if k in a else '—'}")
    return lines[:12]


def describe(e: AuditLog) -> str:
    what = LABEL.get(e.entity, e.entity.replace("_", " ").capitalize())
    name = None
    for d in (e.after, e.before):
        if isinstance(d, dict):
            name = d.get("name") or d.get("invoice_no") or name
    verb = {"create": "added", "update": "changed", "delete": "deleted", "restore": "restored", "save": "saved",
            "cancel": "cancelled", "login_otp": "signed in", "login_pin": "signed in"}.get(e.action, e.action.replace("_", " "))
    return f"{what}{f' “{name}”' if name else ''} {verb}"


@router.get("")
def activity(entity: str | None = None, user_id: uuid.UUID | None = None, q: str | None = Query(default=None, max_length=60),
             before: datetime | None = None, limit: int = Query(default=50, ge=1, le=200),
             auth: Auth = Depends(require(Perm.approve)), db: Session = Depends(get_db)) -> dict:
    """Newest first. Owners and managers see everything in their business (the database keeps each business apart)."""
    return rows_for(db, auth.tenant_id, entity, user_id, q, before, limit)


def rows_for(db: Session, tenant_id, entity=None, user_id=None, q=None, before=None, limit: int = 50) -> dict:
    stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if entity:
        stmt = stmt.where(AuditLog.entity == entity)
    if user_id:
        stmt = stmt.where(AuditLog.actor_user_id == user_id)
    if before:
        stmt = stmt.where(AuditLog.created_at < before)
    if q:
        like = f"%{q}%"
        from sqlalchemy import String, cast
        stmt = stmt.where(or_(cast(AuditLog.after, String).ilike(like), cast(AuditLog.before, String).ilike(like),
                              AuditLog.entity.ilike(like)))
    rows = db.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(limit)).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.tenant_id == tenant_id))}
    outlets = {str(o.id): o.name for o in db.scalars(select(Outlet).where(Outlet.tenant_id == tenant_id))}

    def outlet_of(e: AuditLog):
        for d in (e.after, e.before):
            if isinstance(d, dict):
                for k in ("outlet_id", "outlet"):
                    v = d.get(k)
                    if isinstance(v, str) and v in outlets:
                        return outlets[v]
                    if isinstance(v, str) and k == "outlet":
                        return v
        return None

    out = []
    for e in rows:
        u = users.get(e.actor_user_id) if e.actor_user_id else None
        out.append({"id": str(e.id), "at": e.created_at.isoformat(), "entity": e.entity, "action": e.action,
                    "who": u.name if u else ("Customer / system" if not e.actor_user_id else "Removed user"),
                    "role": u.role if u else None, "user_id": str(u.id) if u else None,
                    "what": describe(e), "changes": changes(e.before, e.after), "outlet": outlet_of(e),
                    "details": None if changes(e.before, e.after) else _clean(e.after or e.before)})
    people = [{"id": str(u.id), "name": u.name, "role": u.role} for u in sorted(users.values(), key=lambda x: x.name)]
    return {"rows": out, "people": people, "more": len(rows) == limit}
