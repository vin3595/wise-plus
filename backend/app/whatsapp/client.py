"""Sending WhatsApp messages through Meta's Cloud API (wise-plus). Settings: WISE_WA_* in the server's .env."""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re

import httpx

from ..config import get_settings

log = logging.getLogger("wise.whatsapp")


def enabled() -> bool:
    s = get_settings()
    return bool(s.wa_token and s.wa_phone_number_id) or f2s_ready()


def f2s_ready() -> bool:
    """Templates go out through Fast2SMS (chosen in admin, with its key, number ID and template IDs)."""
    s = get_settings()
    return s.wa_provider == "fast2sms" and bool(s.fast2sms_api_key and s.wa_f2s_number_id and f2s_templates())


def f2s_templates() -> dict[str, str]:
    import json
    try:
        d = json.loads(get_settings().wa_f2s_templates or "{}")
        return {str(k): str(v) for k, v in d.items() if str(v).strip()} if isinstance(d, dict) else {}
    except ValueError:
        return {}


def to_wa(phone: str) -> str:
    """'98290 00001' / '+91 9829000001' -> '919829000001' (Indian numbers; others as given)."""
    d = re.sub(r"\D", "", phone or "")
    return "91" + d if len(d) == 10 else d


def last10(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


# ------------------------------------------------------------------ who is sending, and why (set around a send)

_tenant: contextvars.ContextVar = contextvars.ContextVar("wa_tenant", default=None)
_via: contextvars.ContextVar = contextvars.ContextVar("wa_via", default=None)       # the number a message came in on
_purpose: contextvars.ContextVar = contextvars.ContextVar("wa_purpose", default=None)
_ref: contextvars.ContextVar = contextvars.ContextVar("wa_ref", default=None)


@contextlib.contextmanager
def context(tenant_id=None, via: str | None = None, purpose: str | None = None, ref: str | None = None):
    """Messages sent inside go from this restaurant's own number (if connected), are logged under it, and labelled."""
    toks = [v.set(x) for v, x in ((_tenant, str(tenant_id) if tenant_id else _tenant.get()), (_via, via or _via.get()),
                                  (_purpose, purpose or _purpose.get()), (_ref, ref or _ref.get()))]
    try:
        yield
    finally:
        for v, t in zip((_tenant, _via, _purpose, _ref), toks):
            v.reset(t)


class WaError(Exception):
    """Meta refused or failed a message. reason is in plain words for the screen."""

    def __init__(self, code: int | None, reason: str, detail: str = ""):
        super().__init__(f"{code}: {reason}" if code else reason)
        self.code, self.reason, self.detail = code, reason, detail

    @property
    def window_closed(self) -> bool:
        return self.code == 131047


# Meta's error codes → what happened, in words; and which are worth trying again in a moment.
REASONS = {
    131026: "Not on WhatsApp (or an old WhatsApp version)", 131047: "More than 24 hours since they last messaged",
    131049: "WhatsApp held it back: too many offers to this person lately", 131050: "They stopped offers from you on WhatsApp",
    131051: "WhatsApp doesn't support this kind of message", 131052: "The picture or file couldn't be fetched",
    131053: "The picture or file couldn't be uploaded", 131048: "Paused: too many messages marked as spam",
    131056: "Too many messages to this person in a short time", 130429: "WhatsApp is busy: too many messages a second",
    131000: "WhatsApp had a problem (will try again)", 131016: "WhatsApp is down for a moment (will try again)",
    132000: "The message template's details don't match", 132001: "The message template isn't there or isn't approved",
    132005: "The message template text is too long", 132007: "The message template broke WhatsApp's rules",
    132012: "The message template's details are in the wrong format", 132015: "The message template is paused by WhatsApp",
    132016: "The message template is switched off by WhatsApp", 133010: "The WhatsApp number isn't registered",
    131031: "The WhatsApp account is locked", 131042: "WhatsApp payment problem on the business account",
    368: "The WhatsApp number is blocked for a while (policy)", 190: "The WhatsApp access token has expired",
    10: "The app lost permission on the WhatsApp number", 100: "Something in the message wasn't accepted",
    4: "Too many requests to WhatsApp (will try again)", 80007: "WhatsApp's limit for this number was reached",
    2: "WhatsApp had a problem (will try again)", 1: "WhatsApp had a problem (will try again)",
}
RETRY = {131000, 131016, 130429, 131056, 4, 80007, 2, 1}


def reason(code: int | None, fallback: str = "") -> str:
    return REASONS.get(code or -1, fallback or "WhatsApp didn't take the message")


def _sender() -> tuple[str, str]:
    """(phone_number_id, token): the number a message came in on, else the restaurant's own, else the shared number."""
    s = get_settings()
    via, tid = _via.get(), _tenant.get()
    if via == s.wa_phone_number_id or not (via or tid):
        return s.wa_phone_number_id, s.wa_token            # a reply on the shared number stays on it
    try:
        from . import numbers
        got = numbers.sender_for(via=via) if via else numbers.sender_for(tenant_id=tid)
        if got:
            return got
    except Exception:  # noqa: BLE001 - never block a message on a lookup: use the shared number
        log.exception("whatsapp: couldn't look up the sending number")
    return s.wa_phone_number_id, s.wa_token


def _record(payload: dict, from_id: str, wamid: str | None, tries: int, err: WaError | None, fallback: bool = False) -> None:
    """Best effort: a log line per message. A failure here never stops the message."""
    try:
        import uuid as _u

        from ..db import system_session
        from .models import WaMessage
        kind = payload.get("type", "text")
        tpl = (payload.get("template") or {}).get("name")
        with system_session() as db:
            db.add(WaMessage(id=_u.uuid4(), tenant_id=_u.UUID(_tenant.get()) if _tenant.get() else None, wamid=wamid, from_id=from_id,
                             to_phone=str(payload.get("to", ""))[:15], kind=kind[:12], purpose=(_purpose.get() or _guess(payload))[:16],
                             template=tpl, ref=(_ref.get() or None), status="failed" if err else "accepted",
                             error_code=err.code if err else None, error=(err.reason if err else None), tries=tries, fallback=fallback))
    except Exception:  # noqa: BLE001
        log.warning("whatsapp: couldn't log a message", exc_info=True)


def _guess(payload: dict) -> str:
    t = (payload.get("template") or {}).get("name") or ""
    s = get_settings()
    if t == s.wa_bill_template:
        return "bill"
    if t in (s.wa_promo_template, s.wa_promo_offer_template):
        return "promo"
    if t == s.wa_otp_template:
        return "otp"
    if t:
        return "template"
    return "reply"


def _f2s_params(tpl: dict) -> tuple[list[str], str | None]:
    """Meta-style template components -> (body values in order, header picture/PDF link)."""
    body, media = [], None
    for c in tpl.get("components") or []:
        for p in c.get("parameters") or []:
            if c.get("type") == "header" and p.get("type") in ("image", "document", "video"):
                media = (p.get(p["type"]) or {}).get("link") or media
            elif c.get("type") == "body":
                body.append(str(p.get("text", "")))
    if not body:                                  # e.g. a code only in the button
        for c in tpl.get("components") or []:
            if c.get("type") == "button":
                body += [str(p.get("text", "")) for p in c.get("parameters") or [] if p.get("type") == "text"]
    return body, media


def _post_f2s(payload: dict, _fallback: bool = False) -> dict:
    """An approved template through Fast2SMS's WhatsApp API (GET /dev/whatsapp, key in the Authorization header)."""
    import time
    s = get_settings()
    tpl = payload.get("template") or {}
    name = tpl.get("name") or ""
    mid = f2s_templates().get(name)
    pnid = s.wa_f2s_number_id
    if not mid:
        err = WaError(132001, f"Template '{name}' has no Fast2SMS ID yet (admin › Your WhatsApp)")
        _record(payload, pnid, None, 0, err, _fallback)
        raise err
    body, media = _f2s_params(tpl)
    q = {"message_id": mid, "phone_number_id": pnid, "numbers": last10(str(payload.get("to", ""))) if str(payload.get("to", "")).startswith("91") else str(payload.get("to", "")),
         "variables_values": "|".join(_flatten(v).replace("|", "/") for v in body)}
    if media:
        q["media_url"] = media
    err: WaError | None = None
    tries = 0
    for wait in (0, 1.0, 3.0):
        if wait:
            time.sleep(wait)
        tries += 1
        try:
            r = httpx.get("https://www.fast2sms.com/dev/whatsapp", params=q, timeout=20, headers={"Authorization": s.fast2sms_api_key})
        except httpx.HTTPError as e:
            err = WaError(None, "Couldn't reach Fast2SMS (network)", str(e)[:200])
            continue
        try:
            data = r.json() or {}
        except ValueError:
            data = {}
        if r.status_code < 300 and (data.get("status") is True or data.get("return") is True):
            rid = str(data.get("request_id") or "") or None
            _record(payload, pnid, rid, tries, None, _fallback)
            return {"messages": [{"id": rid}], "provider": "fast2sms", **data}
        msg = str(data.get("message") or r.text or "")[:200]
        err = WaError(None, f"Fast2SMS refused: {msg}" if msg else "Fast2SMS didn't take the message", msg)
        log.warning("fast2sms whatsapp send failed %s: %s", r.status_code, msg)
        if r.status_code < 500 and r.status_code != 429:
            break
    _record(payload, pnid, None, tries, err, _fallback)
    raise err


def _post_f2s_session(payload: dict, text: str) -> dict:
    """Free-form reply through Fast2SMS (POST /dev/whatsapp-session): only works within 24 hours of their last message."""
    s = get_settings()
    to = to_wa(str(payload.get("to", "")))
    try:
        r = httpx.post("https://www.fast2sms.com/dev/whatsapp-session", params={"phone_number_id": s.wa_f2s_number_id, "to": to},
                       json={"type": "text", "text": text[:4000]}, timeout=20, headers={"Authorization": s.fast2sms_api_key})
        data = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError) as e:
        raise WaError(None, "Couldn't reach Fast2SMS (network)", str(e)[:200]) from e
    ok = r.status_code < 300 and (data.get("status") in (True, "success") or data.get("return") is True)
    if not ok:
        raise WaError(131047, "Chat reply not accepted (outside the 24-hour window?)", str(data.get("message") or r.text)[:200])
    rid = str(data.get("request_id") or data.get("message_id") or "") or None
    _record({**payload, "type": "text"}, s.wa_f2s_number_id, rid, 1, None, False)
    return {"messages": [{"id": rid}], "provider": "fast2sms", "session": True, **data}


# ---------------------------------------------------------------- who pays: the restaurant's prepaid credits
# Ease My Bill pays only for sign-in codes. Bills and broadcasts are charged when they're queued (custwa / offers);
# everything else sent for a restaurant (nightly summary, alerts to the owner, replies, offer codes) is charged here.
_PREPAID_ELSEWHERE = {"otp", "login", "bill", "promo", "alert", "lead", "growth"}


def _credits_cost(payload: dict, result: dict) -> int:
    if result.get("session"):
        return 1                                   # chat reply inside the 24-hour window
    name = (payload.get("template") or {}).get("name") or ""
    s = get_settings()
    if name in (s.wa_bill_template, s.wa_nightly_figures_template):
        return 2                                   # utility template
    return 6                                       # marketing template (incl. the 'update' fallback)


def _chargeable() -> tuple[str | None, bool]:
    tid = _tenant.get()
    return tid, bool(tid) and (_purpose.get() or "") not in _PREPAID_ELSEWHERE


def _has_credits(tid: str) -> bool:
    import uuid as _u

    from ..billing.service import msgs
    from ..db import system_session
    from ..models import Tenant
    with system_session() as db:
        t = db.get(Tenant, _u.UUID(tid))
        m = msgs(db, t) if t else {"unlimited": True}
        return bool(m["unlimited"] or m["bonus"] > 0)


def _charge(tid: str, n: int) -> None:
    import uuid as _u

    from sqlalchemy import select

    from ..billing.models import Subscription
    from ..billing.service import access
    from ..db import system_session
    from ..models import Tenant
    try:
        with system_session() as db:
            t = db.get(Tenant, _u.UUID(tid))
            if t is None or access(db, t)["state"] in ("house", "free"):
                return
            sub = db.scalar(select(Subscription).where(Subscription.tenant_id == t.id).with_for_update())
            if sub:
                sub.bonus_msgs = max(sub.bonus_msgs - n, 0)
    except Exception:  # noqa: BLE001 - never fail a sent message on the bookkeeping
        log.exception("whatsapp: couldn't take credits")


def _post(payload: dict, _fallback: bool = False) -> dict:
    tid, paid = _chargeable()
    if paid and not _has_credits(tid):
        err = WaError(None, "WhatsApp message credits are used up. Recharge in Settings › Billing.")
        _record(payload, "", None, 0, err, _fallback)
        raise err
    result = _send(payload, _fallback)
    if paid:
        _charge(tid, _credits_cost(payload, result if isinstance(result, dict) else {}))
    return result


def _send(payload: dict, _fallback: bool = False) -> dict:
    if f2s_ready() and _via.get() in (None, get_settings().wa_phone_number_id) and not _own_number():
        if payload.get("type") == "template":
            return _post_f2s(payload, _fallback)
        text = _as_text(payload)
        if text:
            try:                          # inside the 24-hour window: a normal chat message (service rate, often free)
                return _post_f2s_session(payload, text)
            except WaError:
                pass                      # window closed (or refused): the approved 'update' template instead
        tpl = get_settings().wa_update_template
        if text and tpl in f2s_templates():
            return _post_f2s({"to": payload.get("to"), "type": "template", "template": {"name": tpl, "language": {"code": "en"},
                              "components": [{"type": "body", "parameters": [{"type": "text", "text": _brand()[:60]},
                                                                             {"type": "text", "text": _flatten(text)}]}]}}, True)
    return _post_meta(payload, _fallback)


def _as_text(payload: dict) -> str:
    """The words of a non-template message (text, buttons/list, picture or file with its link)."""
    k = payload.get("type")
    if k == "text":
        return (payload.get("text") or {}).get("body", "")
    if k == "interactive":
        it = payload.get("interactive") or {}
        act = it.get("action") or {}
        opts = [b["reply"]["title"] for b in act.get("buttons", [])] + [r["title"] for s in act.get("sections", []) for r in s.get("rows", [])]
        return ((it.get("body") or {}).get("text", "") + (" · Reply: " + " / ".join(opts) if opts else ""))
    if k in ("image", "document"):
        m = payload.get(k) or {}
        return " ".join(x for x in (m.get("caption"), m.get("link")) if x)
    return ""


def _own_number() -> bool:
    """A restaurant with its own WhatsApp number connected keeps sending from it (Meta directly)."""
    tid = _tenant.get()
    if not tid:
        return False
    try:
        from . import numbers
        got = numbers.sender_for(tenant_id=tid)
        return bool(got and got[0] != get_settings().wa_phone_number_id)
    except Exception:  # noqa: BLE001
        return False


def _post_meta(payload: dict, _fallback: bool = False) -> dict:
    """Send one message: tries again on Meta's passing errors (busy, rate limit, a hiccup), logs it with its id
    for the delivery ticks, and raises WaError with the reason in words when it can't go."""
    import time
    pnid, token = _sender()
    s = get_settings()
    url = f"https://graph.facebook.com/{s.wa_graph_version}/{pnid}/messages"
    body = {"messaging_product": "whatsapp", **payload}
    err: WaError | None = None
    tries = 0
    for wait in (0, 0.6, 2.0, 5.0):
        if wait:
            time.sleep(wait)
        tries += 1
        try:
            r = httpx.post(url, json=body, timeout=20, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:                     # network: worth another go
            err = WaError(None, "Couldn't reach WhatsApp (network)", str(e)[:200])
            continue
        if r.status_code < 300:
            data = r.json()
            wamid = ((data.get("messages") or [{}])[0]).get("id")
            _record(payload, pnid, wamid, tries, None, _fallback)
            return data
        try:
            e = (r.json() or {}).get("error") or {}
        except ValueError:
            e = {}
        code = e.get("code")
        sub = ((e.get("error_data") or {}).get("details") or e.get("message") or "")[:200]
        err = WaError(code, reason(code, sub), sub)
        log.warning("whatsapp send failed %s (%s): %s", r.status_code, code, sub)
        if not (code in RETRY or r.status_code >= 500 or r.status_code == 429):
            break
    _record(payload, pnid, None, tries, err, _fallback)
    if err and err.code in (190, 10, 133010, 131031, 368, 131042, 132001, 132015, 132016):
        try:                                             # the number or a template is broken: tell the platform owner
            from . import health
            health.problem(f"code:{err.code}", f"WhatsApp: {err.reason}. Messages aren't going out ({err.detail[:120]})")
        except Exception:  # noqa: BLE001
            pass
    raise err


def _flatten(text: str) -> str:
    """Templates can't take new lines, tabs or 4+ spaces in a variable."""
    t = re.sub(r"[\t ]{2,}", " ", re.sub(r"\s*\n+\s*", " · ", text or "")).strip(" ·")
    return t[:900]


def _or_template(fn, to: str, text: str):
    """A reply that fails only because the 24-hour window closed goes again as the approved 'update' template."""
    try:
        return fn()
    except WaError as e:
        tpl = get_settings().wa_update_template
        if not (e.window_closed and tpl):
            raise
        from ..models import Tenant  # noqa: F401
        brand = _brand()
        return _post({"to": to_wa(to), "type": "template", "template": {"name": tpl, "language": {"code": "en"}, "components": [
            {"type": "body", "parameters": [{"type": "text", "text": brand[:60]}, {"type": "text", "text": _flatten(text)}]}]}}, _fallback=True)


def _brand() -> str:
    tid = _tenant.get()
    if tid:
        try:
            import uuid as _u

            from ..db import system_session
            from ..models import Tenant
            with system_session() as db:
                t = db.get(Tenant, _u.UUID(tid))
                if t:
                    return t.app_name or t.name
        except Exception:  # noqa: BLE001
            pass
    return "Ease My Bill"


def send_text(to: str, body: str) -> dict:
    """Free text: only inside the 24-hour window after the person messaged us (replies to the owner's questions)."""
    return _or_template(lambda: _post({"to": to_wa(to), "type": "text", "text": {"preview_url": False, "body": body[:4000]}}), to, body)


def send_template(to: str, name: str, params: list[str] | None = None, lang: str = "en") -> dict:
    """An approved template: anytime (bills, order ready, reminders). Body variables {{1}}, {{2}}… in order."""
    comp = [{"type": "body", "parameters": [{"type": "text", "text": str(p)[:1000]} for p in params]}] if params else []
    return _post({"to": to_wa(to), "type": "template", "template": {"name": name, "language": {"code": lang}, "components": comp}})


def send_document(to: str, link: str, filename: str, caption: str | None = None) -> dict:
    return _post({"to": to_wa(to), "type": "document",
                  "document": {"link": link, "filename": filename, **({"caption": caption} if caption else {})}})


def send_image(to: str, link: str, caption: str | None = None) -> dict:
    return _or_template(lambda: _post({"to": to_wa(to), "type": "image", "image": {"link": link, **({"caption": caption[:1024]} if caption else {})}}),
                        to, (caption or "") + f" {link}")


def get_media(media_id: str) -> tuple[bytes, str]:
    """A photo or PDF someone sent us: (bytes, mime type). Meta gives a short-lived URL that needs the token.
    Through Fast2SMS the 'id' is 'url:' + their download link."""
    if media_id.startswith("url:https://"):
        # Fast2SMS sends the webhook a moment before the file is on their server: wait for it (404 → try again)
        import time
        for wait in (0, 2, 3, 5, 8, 12):
            time.sleep(wait)
            r = httpx.get(media_id[4:], timeout=60, follow_redirects=True)
            if r.status_code not in (403, 404):
                break
        r.raise_for_status()
        if len(r.content) > 16 * 1024 * 1024:
            raise ValueError("file too large")
        return r.content, r.headers.get("content-type", "application/octet-stream").split(";")[0]
    s = get_settings()
    h = {"Authorization": f"Bearer {_sender()[1]}"}
    meta = httpx.get(f"https://graph.facebook.com/{s.wa_graph_version}/{media_id}", headers=h, timeout=20)
    meta.raise_for_status()
    info = meta.json()
    if int(info.get("file_size") or 0) > 16 * 1024 * 1024:
        raise ValueError("file too large")
    r = httpx.get(info["url"], headers=h, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content, info.get("mime_type") or r.headers.get("content-type", "application/octet-stream")


def send_options(to: str, body: str, options: list[tuple[str, str]], button: str = "Menu") -> dict:
    """Text with tappable choices: up to 3 as buttons, up to 10 as a list. options = (command, label).
    A tap comes back as that command, as if typed. Only inside the 24-hour window (replies to the person)."""
    options = options[:10]
    if not options:
        return send_text(to, body)
    if len(body) > 1000:                       # interactive bodies are capped at 1024 characters
        send_text(to, body)
        body = "What next?"
    if len(options) <= 3:
        action = {"buttons": [{"type": "reply", "reply": {"id": cmd[:256], "title": label[:20]}} for cmd, label in options]}
        kind = "button"
    else:
        action = {"button": button[:20], "sections": [{"title": "Choose one", "rows": [
            {"id": cmd[:200], "title": label[:24]} for cmd, label in options]}]}
        kind = "list"
    return _or_template(lambda: _post({"to": to_wa(to), "type": "interactive", "interactive": {"type": kind, "body": {"text": body}, "action": action}}),
                        to, body)
