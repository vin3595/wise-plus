from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .db import system_session
from .purchases_ai import router as bill_scan  # wise-plus: Ease My Bill bill scanning
from .ledger import router as ledger  # wise-plus: supplier dues
from .assistant import router as assistant  # wise-plus: owner keyword answers
from .whatsapp import router as whatsapp  # wise-plus: WhatsApp Cloud API
from .daily_sheet import router as daily_sheets  # wise-plus: daily cash sheets (from Ease My Bill)
from .stockplus import router as stockplus  # wise-plus: stock app extras (transfers, spot checks, profit, nightly)
from .routers import accounts, ads, roles, events, cash, dayclose, inventory, loyalty, offline, payments, sms, report_tables, bill_edits, auth, billing, menu, menu_import, orders, printing, public, qr, reports, tenant, users

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    s = get_settings()
    if len(s.jwt_secret) < 32 or s.jwt_secret == "change-me-in-production":
        if s.otp_provider != "console":
            raise RuntimeError("Set WISE_JWT_SECRET to a random string of at least 32 characters")
        logging.getLogger("wise").warning("Using an insecure development JWT secret")
    app = FastAPI(title="Wise POS API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(IntegrityError)
    async def integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        msg = str(exc.orig).lower() if exc.orig else ""
        detail = "That conflicts with an existing record"
        if "unique" in msg or "duplicate" in msg:
            detail = "That name or code is already used"
        elif "foreign key" in msg:
            detail = "A linked record doesn't exist"
        elif "check constraint" in msg:
            detail = "A value is out of the allowed range"
        return JSONResponse({"detail": detail}, status_code=status.HTTP_409_CONFLICT)

    from fastapi import Depends

    from .modules import guard
    # wise-plus: each service's endpoints answer only for businesses that switched it on (app/modules.py)
    pos, stock_or_bills = [Depends(guard("pos"))], [Depends(guard("stock", "bills"))]
    routes = [(stockplus.router, [Depends(guard("stock"))]), (stockplus.alerts, []), (bill_scan.router, [Depends(guard("bills"))]),
              (ledger.router, [Depends(guard("bills"))]), (daily_sheets.router, [Depends(guard("sheets"))]),
              (inventory.router, stock_or_bills), (orders.router, pos), (billing.router, pos), (bill_edits.router, pos), (qr.router, pos),
              (printing.router, pos), (cash.router, pos), (dayclose.router, pos), (payments.router, pos), (loyalty.router, pos),
              (offline.router, pos), (ads.router, pos)]
    routes += [(r, []) for r in (assistant.router, whatsapp.router, public.router, auth.router, tenant.router, users.router, menu.router,
                                 menu_import.router, reports.router, report_tables.router, sms.router, events.router, accounts.router, roles.router)]
    for r, deps in routes:
        app.include_router(r, prefix="/api", dependencies=deps)

    Path(s.upload_dir).mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=s.upload_dir), name="uploads")

    from .services import events as live
    live.install()  # NOTIFY screens on every committed change
    if s.live_updates:
        @app.on_event("startup")
        def _live() -> None:
            live.start_listener()

    if s.sms_worker:
        from .services.sms import start_worker

        @app.on_event("startup")
        def _sms_worker() -> None:
            from .services.alerts import box_was_off
            try:
                box_was_off()
            except Exception:  # noqa: BLE001  (database not ready yet: nothing to report)
                pass
            start_worker()

    @app.get("/api/health")
    def health() -> JSONResponse:
        """Used by the Wise Box health check and Docker: the API is up and the database answers."""
        try:
            with system_session() as db:
                db.execute(text("select 1"))
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "db": False}, status_code=503)
        return JSONResponse({"ok": True, "db": True})

    @app.get("/api/public/config")
    def public_config() -> dict:
        return {"customer_url": s.customer_url.rstrip("/") or None}

    @app.on_event("startup")
    async def _nightly() -> None:   # wise-plus: nightly WhatsApp summary
        import asyncio
        from .stockplus.nightly import scheduler
        app.state.nightly = asyncio.create_task(scheduler())

    return app


app = create_app()
