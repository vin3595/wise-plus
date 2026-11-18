"""wise-plus: keeping WhatsApp healthy.

* Delivery ticks: Meta's status webhooks (sent, delivered, read, failed + why) on every message we sent.
* Account events: number quality dropping, templates rejected or paused, the account restricted → you're told.
* An hourly check of each number (quality, daily limit, token still working) and of failure spikes.
* /api/health/whatsapp says ok or not (no secrets): the server's own nightly check emails you if it isn't."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update

from ..config import get_settings
from ..db import system_session
from .models import WaHealth, WaMessage, WaNumber

log = logging.getLogger("wise.whatsapp")
ORDER = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
QUIET = timedelta(hours=6)          # the same alert again at most every 6 hours


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get(db, key: str) -> dict | None:
    row = db.get(WaHealth, key)
    return row.value if row else None


def _put(db, key: str, value: dict) -> None:
    row = db.get(WaHealth, key)
    if row is None:
        db.add(WaHealth(key=key, value=value, updated_at=_now()))
    else:
        row.value, row.updated_at = value, _now()


# ------------------------------------------------------------------ telling you

def platform_phones(db) -> list[str]:
    """Owners of your own businesses (WISE_HOUSE_TENANTS): they get WhatsApp's alerts."""
    from ..models import Tenant, User
    logins = [x.strip().lower() for x in get_settings().house_tenants.split(",") if x.strip()]
    if not logins:
        return []
    return sorted({p[-10:] for (p,) in db.execute(select(User.phone).join(Tenant, Tenant.id == User.tenant_id).where(
        func.lower(Tenant.login_id).in_(logins), User.role == "owner", User.is_active.is_(True), User.phone.is_not(None))) if p})


def problem(key: str, message: str) -> bool:
    """Tell the platform owner (once per key every 6 hours). Also kept for /api/health/whatsapp. True if sent now."""
    with system_session() as db:
        last = _get(db, f"alert:{key}")
        if last and datetime.fromisoformat(last["at"]) > _now() - QUIET:
            return False
        _put(db, f"alert:{key}", {"at": _now().isoformat(), "message": message})
        _put(db, "last_problem", {"at": _now().isoformat(), "key": key, "message": message})
        phones = platform_phones(db)
    log.warning("whatsapp alert: %s", message)
    from . import client
    for p in phones:
        try:
            with client.context(purpose="alert"):
                client.send_text(p, "⚠️ " + message)
        except Exception:  # noqa: BLE001 - WhatsApp itself may be what's broken: the email check still catches it
            pass
    return True


# ------------------------------------------------------------------ Meta's webhooks

def on_statuses(statuses: list[dict]) -> None:
    """Delivery ticks. Never goes backwards (read stays read). A failure keeps Meta's reason."""
    from . import client
    for st in statuses or []:
        wamid, new = st.get("id"), (st.get("status") or "").lower()
        if not wamid or new not in ORDER:
            continue
        errs = st.get("errors") or []
        code = errs[0].get("code") if errs else None
        why = client.reason(code, (errs[0].get("title") or "")) if errs else None
        with system_session() as db:
            m = db.scalar(select(WaMessage).where(WaMessage.wamid == wamid).with_for_update())
            if m is not None and (ORDER[new] > ORDER.get(m.status, 0) or new == "failed"):
                m.status, m.updated_at = new, _now()
                if new == "failed":
                    m.error_code, m.error = code, why
            db.execute(update_outbox(wamid, new))
            if new == "failed" and code == 131026 and m is not None and m.tenant_id:
                _unreachable(db, m.tenant_id, m.to_phone, True)
            elif new in ("delivered", "read") and m is not None and m.tenant_id:
                _unreachable(db, m.tenant_id, m.to_phone, False)


def update_outbox(wamid: str, status: str):
    return text("update wa_outbox set delivery = :s where wamid = :w and (delivery is null or delivery <> 'read' or :s = 'failed')"
                ).bindparams(s=status, w=wamid)


def _unreachable(db, tenant_id, to_phone: str, flag: bool) -> None:
    """Remember a number that isn't on WhatsApp (the counter is told to print), and forget it once a message lands."""
    from ..models import Customer
    p10 = to_phone[-10:]
    done = db.execute(update(Customer).where(Customer.tenant_id == tenant_id, Customer.phone == p10).values(wa_unreachable=flag)).rowcount
    if not done and flag:
        db.add(Customer(id=uuid.uuid4(), tenant_id=tenant_id, phone=p10, points_paise=0, stamps=0, wa_unreachable=True))


def on_account(field: str, value: dict) -> None:
    """Number quality, templates, the account itself."""
    if field == "message_template_status_update":
        ev = (value.get("event") or "").upper()
        name = value.get("message_template_name") or "?"
        with system_session() as db:
            _put(db, f"template:{name}", {"status": ev, "reason": value.get("reason"), "at": _now().isoformat()})
        if ev in ("REJECTED", "PAUSED", "DISABLED", "FLAGGED"):
            problem(f"template:{name}:{ev}", f"WhatsApp template '{name}' is {ev.lower()}"
                    + (f" ({value.get('reason')})" if value.get("reason") not in (None, "NONE") else "")
                    + ". Messages that use it won't go until it's fixed.")
    elif field == "phone_number_quality_update":
        ev = (value.get("event") or "").upper()
        num = value.get("display_phone_number") or ""
        with system_session() as db:
            _put(db, f"quality:{num}", {"event": ev, "limit": value.get("current_limit"), "at": _now().isoformat()})
        if ev in ("FLAGGED", "DOWNGRADE"):
            problem(f"quality:{num}:{ev}", f"WhatsApp number {num}: quality {ev.lower()} (limit now {value.get('current_limit') or '?'}). "
                    "Broadcasts are paused until it recovers; bills still go.")
    elif field in ("account_update", "account_alerts", "business_capability_update"):
        ev = (value.get("event") or value.get("alert_type") or "").upper()
        if ev and ev not in ("VERIFIED_ACCOUNT", "ACCOUNT_VERIFIED", "PARTNER_ADDED"):
            problem(f"account:{ev}", f"WhatsApp account notice: {ev.replace('_', ' ').lower()}"
                    + (f" ({value.get('ban_info') or value.get('alert_description') or ''})" if value.get("ban_info") or value.get("alert_description") else ""))


# ------------------------------------------------------------------ hourly check

def _numbers() -> list[tuple[str, str, str | None]]:
    """(label, phone_number_id, token) for the shared number and every restaurant's own."""
    from .numbers import unseal
    s = get_settings()
    out = [("shared", s.wa_phone_number_id, s.wa_token)] if s.wa_phone_number_id and s.wa_token else []
    with system_session() as db:
        for n in db.scalars(select(WaNumber).where(WaNumber.status == "active")):
            out.append((str(n.tenant_id), n.phone_number_id, unseal(n.token_enc)))
    return out


def check_numbers() -> dict:
    """Ask Meta about each number. Bad token, not connected, red quality → alert."""
    import httpx
    s = get_settings()
    res = {}
    for label, pnid, tok in _numbers():
        if not tok:
            res[label] = {"ok": False, "why": "No token: connect WhatsApp again"}
            problem(f"token:{pnid}", f"WhatsApp number of {label} can't send: its connection needs to be made again.")
            continue
        try:
            r = httpx.get(f"https://graph.facebook.com/{s.wa_graph_version}/{pnid}", timeout=15,
                          params={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier,status,name_status,throughput"},
                          headers={"Authorization": f"Bearer {tok}"})
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as e:  # noqa: BLE001
            res[label] = {"ok": None, "why": f"Couldn't reach Meta ({e.__class__.__name__})"}
            continue
        if r.status_code >= 300:
            code = (data.get("error") or {}).get("code")
            from .client import reason
            res[label] = {"ok": False, "why": reason(code), "code": code}
            problem(f"check:{pnid}:{code}", f"WhatsApp number check failed for {label}: {reason(code)}.")
            continue
        q = (data.get("quality_rating") or "").upper()
        st = (data.get("status") or "").upper()
        info = {"ok": q != "RED" and st in ("", "CONNECTED"), "number": data.get("display_phone_number"), "name": data.get("verified_name"),
                "quality": q, "tier": data.get("messaging_limit_tier"), "status": st, "name_status": data.get("name_status"),
                "at": _now().isoformat()}
        res[label] = info
        with system_session() as db:
            _put(db, f"number:{label}", info)
        if q == "RED" or (st and st != "CONNECTED"):
            problem(f"number:{pnid}:{q}:{st}", f"WhatsApp number {info['number']} is {('quality ' + q.lower()) if q == 'RED' else st.lower()}. "
                    "Broadcasts are paused; check WhatsApp Manager.")
    return res


def check_failures() -> None:
    """More than 1 in 5 of the last hour's messages failed (at least 10 sent)?"""
    since = _now() - timedelta(hours=1)
    with system_session() as db:
        n, bad = db.execute(select(func.count(), func.count().filter(WaMessage.status == "failed"))
                            .where(WaMessage.created_at >= since)).one()
        top = db.execute(select(WaMessage.error, func.count()).where(WaMessage.created_at >= since, WaMessage.status == "failed")
                         .group_by(WaMessage.error).order_by(func.count().desc()).limit(2)).all()
    if n >= 10 and bad * 5 > n:
        problem("spike", f"{bad} of {n} WhatsApp messages failed in the last hour. Mostly: "
                + "; ".join(f"{e or 'unknown'} ({c})" for e, c in top) + ".")


def promos_paused() -> str | None:
    """Why broadcasts should wait (shared number in trouble), or None."""
    with system_session() as db:
        shared = _get(db, "number:shared") or {}
        if shared.get("quality") == "RED" or (shared.get("status") and shared.get("status") != "CONNECTED"):
            return "the WhatsApp number's quality is low"
        flagged = db.scalars(select(WaHealth).where(WaHealth.key.like("quality:%"))).all()
        for f in flagged:
            if f.value.get("event") == "FLAGGED" and datetime.fromisoformat(f.value["at"]) > _now() - timedelta(days=7):
                return "the WhatsApp number was flagged"
    return None


def hourly() -> None:
    with system_session() as db:
        last = _get(db, "hourly")
        if last and datetime.fromisoformat(last["at"]) > _now() - timedelta(minutes=55):
            return
        _put(db, "hourly", {"at": _now().isoformat()})
    try:
        check_numbers()
        check_failures()
    except Exception:  # noqa: BLE001
        log.exception("whatsapp hourly check")


def status() -> dict:
    """For /api/health/whatsapp and the platform page (no secrets)."""
    s = get_settings()
    with system_session() as db:
        shared = _get(db, "number:shared")
        lastp = _get(db, "last_problem")
        since = _now() - timedelta(days=1)
        n, bad = db.execute(select(func.count(), func.count().filter(WaMessage.status == "failed")).where(WaMessage.created_at >= since)).one()
    recent = bool(lastp and datetime.fromisoformat(lastp["at"]) > _now() - timedelta(hours=24))
    ok = bool(s.wa_token and s.wa_phone_number_id) and (shared or {}).get("ok", True) is not False and not (n >= 20 and bad * 3 > n)
    return {"ok": ok, "configured": bool(s.wa_token and s.wa_phone_number_id), "number": shared, "day": {"sent": n, "failed": bad},
            "problem": lastp if recent else None}


def whatsapp_uuid(x) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(x))
    except (TypeError, ValueError):
        return None
