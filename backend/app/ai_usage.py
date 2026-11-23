"""wise-plus: what every AI read costs (bill scans, petty slips, daily sheets, bank and supplier statements).
Each model call is stored with the restaurant, what was read, tokens and US$ cost, so admin can see cost per scan."""
from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid

log = logging.getLogger("wise.ai_usage")
SCOPE: contextvars.ContextVar = contextvars.ContextVar("ai_scope", default=(None, None))

# /api/<path> -> what kind of read it is
_PURPOSE = (("/api/purchases", "bill"), ("/api/expenses", "slip"), ("/api/daily-sheet", "sheet"), ("/api/daily_sheet", "sheet"),
            ("/api/banking", "bank"), ("/api/ledger", "statement"), ("/api/suppliers", "statement"))


@contextlib.contextmanager
def scope(tenant_id, purpose: str):
    tok = SCOPE.set((str(tenant_id) if tenant_id else None, purpose))
    try:
        yield
    finally:
        SCOPE.reset(tok)


def record(model: str, tokens_in: int, tokens_out: int, usd: float) -> None:
    """Best effort: never breaks a scan."""
    tid, purpose = SCOPE.get()
    try:
        from sqlalchemy import text

        from .db import system_session
        with system_session() as db:
            db.execute(text("insert into ai_usage (id, tenant_id, model, purpose, tokens_in, tokens_out, usd) "
                            "values (:i, :t, :m, :p, :a, :b, :u)"),
                       {"i": uuid.uuid4(), "t": uuid.UUID(tid) if tid else None, "m": model[:40], "p": (purpose or "other")[:20],
                        "a": int(tokens_in), "b": int(tokens_out), "u": round(float(usd), 6)})
    except Exception:  # noqa: BLE001
        log.warning("ai usage: couldn't record", exc_info=True)


def install(app) -> None:
    """Tag AI calls made while answering an API request with the signed-in restaurant and the kind of read."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class AiScope(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            purpose = next((p for pre, p in _PURPOSE if path.startswith(pre)), None)
            h = request.headers.get("authorization") or ""
            if request.method != "POST" or not purpose or not h.lower().startswith("bearer "):
                return await call_next(request)
            try:
                from .config import get_settings
                from .core.security import decode_token
                tid = decode_token(h.split(" ", 1)[1], get_settings().jwt_secret)["tid"]
            except Exception:  # noqa: BLE001
                return await call_next(request)
            with scope(tid if tid != "platform" else None, purpose):
                return await call_next(request)

    app.add_middleware(AiScope)
