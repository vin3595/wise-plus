"""OTP issue/verify with pluggable delivery. Codes are stored as HMACs, never in plain text."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.security import generate_otp, hash_otp, otp_matches
from ..models import OtpCode

log = logging.getLogger("wise.otp")

MAX_SENDS_PER_WINDOW = 5
SEND_WINDOW_MINUTES = 15


class OtpError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue(db: Session, identifier: str, purpose: str) -> str:
    """Create and deliver an OTP. Returns the code (only exposed to callers in console mode)."""
    s = get_settings()
    since = _now() - timedelta(minutes=SEND_WINDOW_MINUTES)
    recent = db.scalar(
        select(func.count()).select_from(OtpCode).where(
            OtpCode.identifier == identifier, OtpCode.created_at >= since)
    )
    if recent and recent >= MAX_SENDS_PER_WINDOW:
        raise OtpError("Too many codes requested. Try again in 15 minutes.")
    code = generate_otp()
    db.add(OtpCode(
        identifier=identifier, purpose=purpose, code_hash=hash_otp(code, identifier, s.jwt_secret),
        expires_at=_now() + timedelta(minutes=s.otp_ttl_minutes),
    ))
    db.flush()
    _deliver(identifier, code)
    return code


def verify(db: Session, identifier: str, purpose: str, code: str) -> None:
    s = get_settings()
    otp = db.scalar(
        select(OtpCode).where(
            OtpCode.identifier == identifier, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None))
        .order_by(OtpCode.created_at.desc()).limit(1).with_for_update()
    )
    if otp is None or otp.expires_at < _now():
        raise OtpError("Code expired. Request a new one.")
    if otp.attempts >= s.otp_max_attempts:
        raise OtpError("Too many wrong attempts. Request a new code.")
    if not otp_matches(code, identifier, s.jwt_secret, otp.code_hash):
        otp.attempts += 1
        raise OtpError("Wrong code")
    otp.consumed_at = _now()


def _phone(identifier: str) -> str:
    """Staff and owner sign-in codes are keyed "<tenant id>:<phone>"; SMS goes to the phone part."""
    return identifier.rsplit(":", 1)[-1]


def _deliver_via(provider: str, identifier: str, code: str) -> None:
    s = get_settings()
    _deliver(identifier, code, s.model_copy(update={"otp_provider": provider}))


def send_mail(to: str, subject: str, text: str, html: str | None = None, settings=None) -> None:
    """Email through the Gmail account set in admin › Login codes (Google app password, STARTTLS on 587)."""
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr
    s = settings or get_settings()
    msg = EmailMessage()
    msg["From"] = formataddr(("Ease My Bill", s.mail_user))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(s.mail_host, s.mail_port, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(s.mail_user, s.mail_password)
        smtp.send_message(msg)


def _email_code(to: str, code: str) -> None:
    mins = get_settings().otp_ttl_minutes
    send_mail(to, f"{code} is your Ease My Bill code",
              f"Your Ease My Bill code is {code}\n\nIt works for {mins} minutes. Don't share it with anyone.\n"
              f"If you didn't ask for it, ignore this email.",
              f"""<div style="font-family:Arial,sans-serif;max-width:420px">
<p>Your Ease My Bill code is</p><p style="font-size:32px;font-weight:bold;letter-spacing:6px;margin:8px 0">{code}</p>
<p style="color:#555">It works for {mins} minutes. Don't share it with anyone.<br>If you didn't ask for it, ignore this email.</p></div>""")


def _deliver(identifier: str, code: str, settings=None) -> None:
    s = settings or get_settings()
    if "@" in identifier:
        to = identifier.rsplit(":", 1)[-1]
        if s.mail_user and s.mail_password:
            try:
                _email_code(to, code)
            except Exception:  # noqa: BLE001
                log.exception("email OTP send failed")
                raise OtpError("Couldn't email the code right now. Try your mobile number.") from None
            return
        if s.otp_provider in ("console", "box"):
            log.warning("OTP for %s: %s", identifier, code)    # development only
            return
        raise OtpError("Codes by email aren't switched on yet. Use your mobile number.")
    if s.otp_provider in ("console", "box"):
        log.warning("OTP for %s: %s", identifier, code)        # development only
        return
    if settings is None and s.phone_otp in ("whatsapp", "sms"):   # the admin's pick for mobile numbers
        prov = "whatsapp" if s.phone_otp == "whatsapp" else ("fast2sms" if s.fast2sms_api_key else "msg91")
        if prov != s.otp_provider:
            _deliver_via(prov, identifier, code)
            return
    if s.otp_provider == "msg91":
        # MSG91 OTP API: template must contain the ##OTP## placeholder.
        resp = httpx.post(
            "https://control.msg91.com/api/v5/otp",
            params={"template_id": s.msg91_template_id, "mobile": "91" + _phone(identifier), "otp": code},
            headers={"authkey": s.msg91_auth_key},
            timeout=10,
        )
        if resp.status_code >= 400:
            log.error("MSG91 send failed: %s %s", resp.status_code, resp.text[:200])
            raise OtpError("Couldn't send the code right now. Try again.")
        return
    if s.otp_provider == "fast2sms":
        # Fast2SMS OTP route: their own pre-approved OTP text, no DLT registration needed.
        resp = httpx.get(
            "https://www.fast2sms.com/dev/bulkV2",
            params={"authorization": s.fast2sms_api_key, "route": "otp", "variables_values": code,
                    "numbers": _phone(identifier), "flash": "0"},
            timeout=10,
        )
        ok = resp.status_code < 400
        try:
            ok = ok and bool(resp.json().get("return"))
        except ValueError:
            ok = False
        if not ok:
            log.error("Fast2SMS send failed: %s %s", resp.status_code, resp.text[:200])
            raise OtpError("Couldn't send the code right now. Try again.")
        return
    if s.otp_provider == "whatsapp":
        # wise-plus: the code arrives on WhatsApp from the business number (approved authentication
        # template WISE_WA_OTP_TEMPLATE with a copy-code button), so people can sign up on their own.
        from ..whatsapp import client
        try:
            client._post({"to": client.to_wa(_phone(identifier)), "type": "template", "template": {
                "name": s.wa_otp_template, "language": {"code": "en"}, "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": code}]},
                    {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]}]}})
        except Exception:  # noqa: BLE001
            log.exception("WhatsApp OTP send failed")
            # wise-plus: never stranded on a sign-in code: SMS when an SMS service is set up (codes are never logged)
            if s.fast2sms_api_key or (s.msg91_auth_key and s.msg91_template_id):
                prov = "fast2sms" if s.fast2sms_api_key else "msg91"
                _deliver_via(prov, identifier, code)
                return
            raise OtpError("Couldn't send the code on WhatsApp right now. Try again.") from None
        return
    raise OtpError(f"Unknown OTP provider {s.otp_provider!r}")
