"""wise-plus: for you, the platform owner — every restaurant's WhatsApp messages and bill scans in one place.

Who sees it: the owner of one of your own businesses (WISE_HOUSE_TENANTS). Nobody else, whatever their role."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import system_session
from ..deps import Auth
from ..models import Outlet, Tenant
from .admin_auth import is_admin_phone, require_admin
from .models import Subscription
from .plans import MSGS_PER_OUTLET, SCANS_PER_MONTH
from .service import _month, access, house

# What Meta charges us per message in India (rate card), to show the cost behind each restaurant's sending.
META_PAISE = {"bill": 11.5, "otp": 11.5, "promo": 86.31}

router = APIRouter(prefix="/platform", tags=["platform"])


def is_admin(db: Session, auth: Auth) -> bool:
    """Restaurant logins (your own included) are never the platform admin: that's /admin, see admin_auth."""
    return False


def usage(month: str | None = None) -> dict:
    """Every restaurant, this month (or YYYY-MM): bill messages used of the free allowance, bought credits left,
    what actually went out on WhatsApp by kind and what Meta charged for it, and bill scans."""
    month = month or _month()
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    rows = []
    with system_session() as db:
        sent: dict = {}
        for tid, kind, st, n in db.execute(text(
                "select tenant_id, kind, status, count(*) from wa_outbox where created_at >= :a and created_at < :b group by 1, 2, 3"),
                {"a": start, "b": end}):
            sent.setdefault(tid, {}).setdefault(kind, {})[st] = n
        ticks: dict = {}
        for tid, st, n in db.execute(text("select tenant_id, status, count(*) from wa_messages where created_at >= :a and created_at < :b "
                                          "group by 1, 2"), {"a": start, "b": end}):
            ticks.setdefault(tid, {})[st] = n
        whys: dict = {}
        for tid, err, n in db.execute(text("select tenant_id, error, count(*) from wa_messages where created_at >= :a and created_at < :b "
                                           "and status = 'failed' group by 1, 2 order by 3 desc"), {"a": start, "b": end}):
            whys.setdefault(tid, []).append({"why": err or "Unknown", "count": n})
        own = {t for (t,) in db.execute(text("select tenant_id from wa_numbers where status = 'active'"))}
        outlets = dict(db.execute(select(Outlet.tenant_id, func.count()).where(Outlet.is_active.is_(True), Outlet.is_training.is_(False))
                                  .group_by(Outlet.tenant_id)).all())
        for t in db.scalars(select(Tenant).order_by(Tenant.name)):
            s = db.get(Subscription, t.id)
            st = access(db, t)["state"]
            n_out = outlets.get(t.id, 0) or 1
            cur = month == _month()
            used = (s.msgs_used if s and s.msg_month == month else 0) if cur else None
            allow = MSGS_PER_OUTLET * n_out
            by = sent.get(t.id, {})
            out_sent = {k: v.get("sent", 0) for k, v in by.items()}
            failed = sum(v.get("failed", 0) for v in by.values())
            cost = sum(META_PAISE.get(k, META_PAISE["bill"]) * n for k, n in out_sent.items())
            unlimited = st in ("house", "free")
            rows.append({
                "tenant_id": str(t.id), "name": t.app_name or t.name, "login_id": t.login_id, "state": st, "outlets": n_out,
                "bill_msgs_used": used if used is not None else out_sent.get("bill", 0), "bill_msgs_free": None if unlimited else allow,
                "credits_left": None if unlimited else (s.bonus_msgs if s else 0),
                "left_this_month": None if unlimited else (max(allow - (used or 0), 0) + (s.bonus_msgs if s else 0)),
                "sent": out_sent, "failed": failed, "meta_cost_paise": round(cost),
                "scans_used": (s.scans_used if s and s.scan_month == month else 0), "scans_free": None if house(t.login_id) else SCANS_PER_MONTH,
                "scans_bought_left": s.bonus_scans if s else 0,
                "packs_bought": sum(1 for p in (s.packs or []) if p.get("messages")) if s else 0,
                "alert": (s.msg_alert or "").split(":")[-1] if s and s.msg_alert and s.msg_alert.startswith(month) else None,
                "delivery": _rates(ticks.get(t.id, {})), "fail_reasons": whys.get(t.id, [])[:4], "own_number": t.id in own,
            })
    tot = {"sent": sum(sum(r["sent"].values()) for r in rows), "meta_cost_paise": sum(r["meta_cost_paise"] for r in rows),
           "credits_left": sum(r["credits_left"] or 0 for r in rows), "restaurants": len(rows),
           "running_out": sum(1 for r in rows if r["left_this_month"] is not None and r["left_this_month"] <= max(1, (r["bill_msgs_free"] or 0) // 5))}
    from ..whatsapp.health import status as wa_status
    allt: dict = {}
    for r in rows:
        for k, v in r["delivery"]["counts"].items():
            allt[k] = allt.get(k, 0) + v
    tot["delivery"] = _rates(allt)
    try:
        health = wa_status()
    except Exception:  # noqa: BLE001
        health = None
    return {"month": month, "restaurants": rows, "total": tot, "health": health}


def _rates(c: dict) -> dict:
    """Of the messages Meta took: how many reached the phone, were read, failed."""
    n = sum(c.values())
    reached = c.get("delivered", 0) + c.get("read", 0)
    pct = lambda x: round(x * 100 / n) if n else None  # noqa: E731
    return {"counts": c, "total": n, "delivered_pct": pct(reached), "read_pct": pct(c.get("read", 0)), "failed_pct": pct(c.get("failed", 0))}


@router.get("/usage")
def platform_usage(month: str | None = None, _adm: str = Depends(require_admin)) -> dict:
    if month and not (len(month) == 7 and month[4] == "-" and month.replace("-", "").isdigit()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Month as YYYY-MM")
    return usage(month)


def whatsapp_text(month: str | None = None) -> str:
    u = usage(month)
    t = u["total"]
    lines = [f"*WhatsApp across all restaurants, {u['month']}*",
             f"{t['sent']} messages sent · Meta cost ₹{t['meta_cost_paise'] / 100:,.0f} · {t['credits_left']} bought credits unused"
             + (f" · ⚠️ {t['running_out']} running out" if t["running_out"] else "")
             + (f"\nDelivered {t['delivery']['delivered_pct']}% · read {t['delivery']['read_pct']}% · failed {t['delivery']['failed_pct']}%"
                if t["delivery"]["total"] else ""), ""]
    h = u.get("health") or {}
    num = h.get("number") or {}
    if num:
        lines.insert(1, f"Number {num.get('number') or ''}: quality {num.get('quality') or '?'} · limit {num.get('tier') or '?'} · {num.get('status') or ''}".strip())
    if h.get("problem"):
        lines.insert(2, "⚠️ Last problem: " + h["problem"]["message"])
    for r in sorted(u["restaurants"], key=lambda r: -sum(r["sent"].values()))[:25]:
        if r["bill_msgs_free"] is None:
            left = "unlimited"
        else:
            left = f"{r['bill_msgs_used']}/{r['bill_msgs_free']} free used · {r['credits_left']} bought left"
            if r["left_this_month"] is not None and r["left_this_month"] <= max(1, r["bill_msgs_free"] // 5):
                left += " ⚠️"
        sent = sum(r["sent"].values())
        d = r["delivery"]
        tick = f" · {d['delivered_pct']}% delivered" if d["total"] else ""
        bad = f" · ❌ {r['fail_reasons'][0]['why']}" if r["fail_reasons"] else ""
        lines.append(f"• *{r['name']}* ({r['state']}): {left} · sent {sent}" + (f" ({r['sent'].get('promo', 0)} broadcast)" if r["sent"].get("promo") else "")
                     + tick + bad)
    return "\n".join(lines)


def admin_by_phone(phone: str) -> bool:
    return is_admin_phone(phone)


@router.post("/whatsapp/{login_id}")
def set_whatsapp_for(login_id: str, body: dict, _adm: str = Depends(require_admin)) -> dict:
    """You setting up a restaurant's own WhatsApp API for them."""
    from ..whatsapp.connect import ManualIn, manual_connect
    with system_session() as sdb:
        t = sdb.scalar(select(Tenant).where(func.lower(Tenant.login_id) == login_id.lower()))
        tid = t.id if t else None
    if tid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such restaurant")
    try:
        data = ManualIn.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Phone number id, token (and WABA id) please") from e
    return manual_connect(tid, data)


# ---------------------------------------------------------------- your Razorpay, for Ease My Bill's own billing

@router.get("/razorpay")
def razorpay_keys(_adm: str = Depends(require_admin)) -> dict:
    from . import rz
    k = rz.keys()
    return {"key_id": k["key_id"] or None, "has_secret": bool(k["key_secret"]), "has_webhook_secret": bool(k["webhook_secret"]),
            "webhook_url": get_settings().public_base_url.rstrip("/") + "/api/public/billing/razorpay"}


@router.post("/razorpay")
def set_razorpay_keys(body: dict, _adm: str = Depends(require_admin)) -> dict:
    """Paste the live Key Id and Key Secret; they're checked with Razorpay before they're kept."""
    import secrets
    from . import rz
    kid = str(body.get("key_id") or "").strip()
    sec = str(body.get("key_secret") or "").strip()
    if not kid.startswith("rzp_") or len(sec) < 10:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Paste the Key Id (starts rzp_live_) and the Key Secret")
    try:
        rz.call("GET", "/plans?count=1", auth=(kid, sec))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Razorpay didn't accept those keys ({e})") from e
    vals = {"key_id": kid, "key_secret": sec}
    if not rz.keys()["webhook_secret"] or body.get("new_webhook_secret"):
        vals["webhook_secret"] = secrets.token_urlsafe(24)
    rz.save_keys(vals)
    return {"ok": True, "key_id": kid, "webhook_url": get_settings().public_base_url.rstrip("/") + "/api/public/billing/razorpay",
            "webhook_secret": vals.get("webhook_secret")}     # shown once, to paste into Razorpay › Webhooks


@router.post("/razorpay/webhook-secret")
def new_razorpay_webhook_secret(_adm: str = Depends(require_admin)) -> dict:
    """A fresh webhook secret, shown once: paste it into Razorpay › Webhooks (the old one stops working)."""
    import secrets
    from . import rz
    if not rz.configured():
        raise HTTPException(status.HTTP_409_CONFLICT, "Add your Razorpay keys first")
    sec = secrets.token_urlsafe(24)
    rz.save_keys({"webhook_secret": sec})
    return {"webhook_secret": sec, "webhook_url": get_settings().public_base_url.rstrip("/") + "/api/public/billing/razorpay"}


# ---------------------------------------------------------------- Ease My Bill's own WhatsApp number (the shared sender)

@router.get("/whatsapp-sender")
def wa_sender(_adm: str = Depends(require_admin)) -> dict:
    s = get_settings()
    return {"phone_number_id": s.wa_phone_number_id or None, "waba_id": s.wa_business_id or None, "app_id": s.wa_app_id or None,
            "display_number": s.wa_display_number or None, "has_token": bool(s.wa_token), "has_app_secret": bool(s.wa_app_secret),
            "webhook_url": s.public_base_url.rstrip("/") + "/api/whatsapp/webhook", "verify_token": s.wa_verify_token or None}


@router.post("/whatsapp-sender")
def set_wa_sender(body: dict, _adm: str = Depends(require_admin)) -> dict:
    """Paste the permanent (system user) token, the phone number ID and WABA ID; checked with Meta, kept sealed.
    The API restarts to pick it up (sudo wiseplus-cloud restart) — or it is used from the next restart."""
    import httpx

    from ..config import PLATFORM_WA_KEYS
    from ..whatsapp.numbers import seal
    from .models import PlatformSecret
    s = get_settings()
    tok = str(body.get("token") or "").strip() or s.wa_token      # blank: keep the saved token (e.g. adding the app secret)
    pid = str(body.get("phone_number_id") or "").strip()
    waba = str(body.get("waba_id") or "").strip()
    sec = str(body.get("app_secret") or "").strip()
    app_id = str(body.get("app_id") or "").strip()
    if not (tok.startswith("EAA") and pid.isdigit()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Paste the token (starts EAA) and the phone number ID (digits)")
    try:
        r = httpx.get(f"https://graph.facebook.com/{s.wa_graph_version}/{pid}", params={"access_token": tok,
                      "fields": "display_phone_number,verified_name,code_verification_status"}, timeout=20).json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Couldn't reach Meta ({type(e).__name__}). Try again.") from e
    if "error" in r:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Meta refused: {r['error'].get('message', 'unknown error')}")
    shown = "".join(ch for ch in r.get("display_phone_number", "") if ch.isdigit())
    if sec and app_id.isdigit():      # the app secret must belong to that app
        chk = httpx.get(f"https://graph.facebook.com/{s.wa_graph_version}/oauth/access_token", params={
            "client_id": app_id, "client_secret": sec, "grant_type": "client_credentials"}, timeout=20).json()
        if "access_token" not in chk:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That app secret doesn't match app " + app_id)
    vals = {"wa_token": tok, "wa_phone_number_id": pid, "wa_display_number": shown}
    if waba.isdigit():
        vals["wa_business_id"] = waba
    if sec:
        vals["wa_app_secret"] = sec
    if app_id.isdigit():
        vals["wa_app_id"] = app_id
    with system_session() as db:
        for k, v in vals.items():
            assert k in PLATFORM_WA_KEYS
            row = db.get(PlatformSecret, k)
            if row:
                row.value_enc = seal(v)
            else:
                db.add(PlatformSecret(key=k, value_enc=seal(v)))
    for k, v in vals.items():          # this process now; the others on restart
        setattr(s, k, v)
    return {"ok": True, "display_number": shown, "name": r.get("verified_name"), "verified": r.get("code_verification_status")}


# ---------------------------------------------------------------- login codes: how sign-in codes reach people

def _save_platform(vals: dict[str, str]) -> None:
    from ..whatsapp.numbers import seal
    from .models import PlatformSecret
    with system_session() as db:
        for k, v in vals.items():
            row = db.get(PlatformSecret, k)
            if row:
                row.value_enc = seal(v)
            else:
                db.add(PlatformSecret(key=k, value_enc=seal(v)))
    s = get_settings()
    for k, v in vals.items():          # this worker now; the others within a minute
        setattr(s, k, v)


@router.get("/login-codes")
def login_codes(_adm: str = Depends(require_admin)) -> dict:
    s = get_settings()
    phone = s.phone_otp or ("sms" if s.otp_provider in ("fast2sms", "msg91") else "whatsapp")
    return {"phone": phone, "has_sms_key": bool(s.fast2sms_api_key or (s.msg91_auth_key and s.msg91_template_id)),
            "mail_user": s.mail_user or None, "has_mail_password": bool(s.mail_password),
            "whatsapp_ready": bool(s.wa_token and s.wa_phone_number_id)}


@router.post("/login-codes")
def set_login_codes(body: dict, _adm: str = Depends(require_admin)) -> dict:
    """Mobile numbers: WhatsApp or SMS (Fast2SMS key). Email: a Gmail address and its Google app password,
    checked by signing in to Gmail and sending a test email to that same address."""
    s = get_settings()
    vals: dict[str, str] = {}
    phone = str(body.get("phone") or "").strip()
    if phone:
        if phone not in ("whatsapp", "sms"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose WhatsApp or SMS")
        vals["phone_otp"] = phone
    key = str(body.get("sms_key") or "").strip()
    if key:
        vals["fast2sms_api_key"] = key
    if phone == "sms" and not (key or s.fast2sms_api_key or (s.msg91_auth_key and s.msg91_template_id)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "For SMS, paste your Fast2SMS API key first")
    user = str(body.get("mail_user") or "").strip().lower()
    pw = str(body.get("mail_password") or "").replace(" ", "").strip()
    tested = None
    if user or pw:
        user = user or s.mail_user
        pw = pw or s.mail_password
        if "@" not in user or len(pw) < 8:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter the Gmail address and its 16-letter app password")
        from ..services.otp import send_mail
        trial = s.model_copy(update={"mail_user": user, "mail_password": pw})
        try:
            send_mail(user, "Ease My Bill can now email sign-in codes",
                      "This is a test from Ease My Bill. Sign-in codes will be sent from this address.", settings=trial)
        except Exception as e:  # noqa: BLE001
            msg = "Gmail didn't accept that app password" if "535" in str(e) or "Username" in str(e) else f"Couldn't send a test email ({e})"
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, msg) from e
        vals.update({"mail_user": user, "mail_password": pw})
        tested = user
    if not vals:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to save")
    _save_platform(vals)
    return {"ok": True, "test_email_to": tested, **login_codes(_adm)}


@router.get("/activity/{login_id}")
def restaurant_activity(login_id: str, entity: str | None = None, q: str | None = None, before: datetime | None = None,
                        limit: int = 50, _adm: str = Depends(require_admin)) -> dict:
    """Ease My Bill admin: any restaurant's activity log (who changed what), for support."""
    from ..activity import rows_for
    with system_session() as db:
        t = db.scalar(select(Tenant).where(Tenant.login_id == login_id.strip().lower()))
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No restaurant with that login ID")
        return {"restaurant": t.name, **rows_for(db, t.id, entity, None, q, before, min(max(limit, 1), 200))}
