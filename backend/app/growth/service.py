"""wise-plus growth: what happens to a lead and a new restaurant, on WhatsApp, without anyone chasing.

Lead (website form, ticked 'message me on WhatsApp'):
  now      welcome + trial link (+ demo time if booked); you're told
  1 day    'start your free trial' (if they haven't)        3 days  what it does at Shakewallah
  7 days   founding offer, last note                        a 'Stop' reply ends it
  1 hour before a booked demo: a reminder to them and to you
New restaurant on a free trial (account notices):
  day 1 getting started · day 3 help if no bill yet · 4 days and 1 day before the trial ends · the day after it ends
Referral: their link brings a restaurant; when that one first pays, both get a month free."""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..config import get_settings
from ..db import system_session
from .models import Lead, RefCode, Referral, TrialNudge

log = logging.getLogger("wise.growth")
IST = timezone(timedelta(hours=5, minutes=30))
BRAND = "Ease My Bill"
STEPS = [timedelta(days=1), timedelta(days=3), timedelta(days=7)]      # after the welcome


def _now() -> datetime:
    return datetime.now(timezone.utc)


def support() -> str:
    p = get_settings().support_phone[-10:]
    return f"+91 {p[:5]} {p[5:]}" if len(p) == 10 else p


def site() -> str:
    return get_settings().public_base_url.rstrip("/")


def p10(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")[-10:]
    if not re.fullmatch(r"[6-9]\d{9}", d):
        raise ValueError("Enter a 10-digit Indian mobile number")
    return d


# ------------------------------------------------------------------ sending

def _send(phone10: str, text: str, marketing: bool, purpose: str) -> bool:
    """Free text inside the 24-hour window; otherwise an approved template (the marketing one for follow-ups,
    the 'update' one for account notices). Never raises."""
    from ..whatsapp import client
    s = get_settings()
    if not client.enabled():
        log.info("growth: WhatsApp off, not sent to …%s", phone10[-4:])
        return False
    with client.context(purpose=purpose):
        try:
            if not marketing:
                client.send_text("91" + phone10, text)                 # falls back to the 'update' template itself
                return True
            try:
                client._post({"to": "91" + phone10, "type": "text", "text": {"preview_url": True, "body": text[:4000]}})
                return True
            except client.WaError as e:
                if not e.window_closed or not s.wa_growth_template:
                    raise
            client._post({"to": "91" + phone10, "type": "template", "template": {
                "name": s.wa_growth_template, "language": {"code": "en"}, "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": client._flatten(text)}]},
                    {"type": "button", "sub_type": "quick_reply", "index": "0", "parameters": [{"type": "payload", "payload": "lead-stop"}]}]}},
                _fallback=True)
            return True
        except Exception:  # noqa: BLE001
            log.warning("growth: message to …%s didn't go", phone10[-4:], exc_info=True)
            return False


def tell_me(text: str) -> None:
    """You (owners of your own businesses), on WhatsApp."""
    from ..whatsapp.health import platform_phones
    with system_session() as db:
        phones = platform_phones(db)
    for p in phones:
        _send(p, text, False, "growth")


# ------------------------------------------------------------------ leads

def demo_slots(days: int = 7) -> list[str]:
    """Open demo times (IST, every hour 11:00–19:00, from 3 hours from now), as ISO strings."""
    with system_session() as db:
        taken = {d.astimezone(IST).replace(minute=0, second=0, microsecond=0) for (d,) in db.execute(
            select(Lead.demo_at).where(Lead.demo_at.is_not(None), Lead.demo_at >= _now())) if d}
    out = []
    start = (_now() + timedelta(hours=3)).astimezone(IST)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(days + 1):
        d = day + timedelta(days=i)
        for h in range(11, 20):
            t = d.replace(hour=h)
            if t >= start and t not in taken:
                out.append(t.isoformat())
    return out


def save_lead(data: dict) -> tuple[Lead, bool]:
    """From the website (or added by you). Returns (lead, new?)."""
    phone = p10(data["phone"])
    with system_session() as db:
        ld = db.scalar(select(Lead).where(Lead.phone10 == phone).with_for_update())
        new = ld is None
        if new:
            ld = Lead(id=uuid.uuid4(), phone10=phone, name=data["name"].strip()[:80], status="new", step=0, consent=False, stopped=False)
            db.add(ld)
        for k in ("restaurant", "city", "uses_now", "source"):
            if data.get(k):
                setattr(ld, k, str(data[k]).strip()[: Lead.__table__.c[k].type.length])
        if data.get("outlets"):
            ld.outlets = max(1, min(int(data["outlets"]), 500))
        if data.get("utm"):
            ld.utm = {**(ld.utm or {}), **{k: str(v)[:80] for k, v in data["utm"].items() if k.startswith("utm_") or k in ("fbclid", "gclid")}}
        if data.get("ref"):
            ld.ref_code = str(data["ref"]).upper()[:20]
            ld.source = ld.source or "referral"
        if data.get("consent"):
            ld.consent = True
            ld.stopped = False
        if data.get("demo_at"):
            ld.demo_at = data["demo_at"]
            if ld.status in ("new", "talking"):
                ld.status = "demo"
        if new and ld.consent:
            ld.step, ld.next_at = 0, _now() + STEPS[0]
        ld.updated_at = _now()
        db.flush()
        db.expunge(ld)
    return ld, new


def welcome(ld: Lead) -> None:
    first = ld.name.split()[0]
    demo = ""
    if ld.demo_at:
        demo = f"\n\n📅 Your demo: *{ld.demo_at.astimezone(IST):%a %d %b, %I:%M %p}*. We'll call you on this number."
    trial = f"{site()}/signup?phone={ld.phone10}" + (f"&ref={ld.ref_code}" if ld.ref_code else "")
    if ld.consent:
        _send(ld.phone10, f"Namaste {first}! 🙏 Thanks for your interest in *{BRAND}*: the restaurant POS with WhatsApp built in."
              f"{demo}\n\nStart your {get_settings().trial_days}-day free trial now (no card needed): {trial}\n\nReply here with any question.", True, "lead")
    tell_me(f"🆕 Lead: *{ld.name}*{f' · {ld.restaurant}' if ld.restaurant else ''}{f' · {ld.city}' if ld.city else ''}"
            f"{f' · {ld.outlets} outlet(s)' if ld.outlets else ''}{f' · uses {ld.uses_now}' if ld.uses_now else ''}\n📞 +91 {ld.phone10}"
            + (f"\n📅 Demo {ld.demo_at.astimezone(IST):%a %d %b %I:%M %p}" if ld.demo_at else "")
            + (f"\nVia {ld.source}" if ld.source else "") + (f" ({(ld.utm or {}).get('utm_campaign')})" if (ld.utm or {}).get("utm_campaign") else ""))


FOLLOW = [
    lambda ld: (f"Hi {ld.name.split()[0]}, did you get a chance to try {BRAND}? Setting up takes about 10 minutes: add your menu "
                f"(or send us a photo of it) and start billing. Bills go to your customers on WhatsApp automatically.\n\n"
                f"Free for {get_settings().trial_days} days: {site()}/signup?phone={ld.phone10}"),
    lambda ld: (f"At Shakewallah in Jodhpur, {BRAND} runs every outlet: bills on WhatsApp, offers by QR "
                f"(customers send a keyword, scan at the counter), supplier bills read from a photo, stock and recipe costs, Tally for the CA. "
                f"Want to see it on your own menu? Reply *demo* and we'll call you."),
    lambda ld: (f"Last note from us, {ld.name.split()[0]}: our *founding offer* (everything for ₹4,999 for the whole first year) is only for the "
                f"first 50 restaurants. Start here: {site()}/signup?phone={ld.phone10}\n\nThanks, and all the best with your restaurant! 🙏"),
]


def run_leads() -> int:
    """Due follow-ups and demo reminders. Returns messages sent."""
    sent = 0
    with system_session() as db:
        due = db.scalars(select(Lead).where(Lead.next_at.is_not(None), Lead.next_at <= _now(), Lead.stopped.is_(False),
                                            Lead.consent.is_(True)).with_for_update(skip_locked=True).limit(50)).all()
        work = []
        for ld in due:
            if ld.tenant_id or ld.status in ("trial", "paid", "lost") or ld.step >= len(FOLLOW):
                ld.next_at = None
                continue
            work.append((ld.phone10, FOLLOW[ld.step](ld)))
            ld.step += 1
            ld.next_at = (ld.created_at + STEPS[ld.step]) if ld.step < len(STEPS) else None
            if ld.next_at and ld.next_at <= _now():
                ld.next_at = _now() + timedelta(days=1)
        soon = db.scalars(select(Lead).where(Lead.demo_at.is_not(None), Lead.demo_at > _now(), Lead.demo_at <= _now() + timedelta(minutes=70))).all()
        from ..whatsapp.health import _get, _put
        reminders = []
        for ld in soon:
            key = f"demo-remind:{ld.id}:{ld.demo_at.isoformat()}"
            if _get(db, key):
                continue
            _put(db, key, {"at": _now().isoformat()})
            reminders.append((ld.phone10, ld.name, ld.restaurant, ld.demo_at, ld.consent))
    for ph, text in work:
        sent += _send(ph, text, True, "lead")
    for ph, name, rest, at, ok in reminders:
        if ok:
            sent += _send(ph, f"Hi {name.split()[0]}, a reminder: your {BRAND} demo is at *{at.astimezone(IST):%I:%M %p}* today. "
                              "We'll call you on this number.", False, "lead")
        tell_me(f"⏰ Demo at {at.astimezone(IST):%I:%M %p}: *{name}*{f' ({rest})' if rest else ''} · +91 {ph}")
    return sent


def stop(phone10: str) -> bool:
    with system_session() as db:
        ld = db.scalar(select(Lead).where(Lead.phone10 == phone10[-10:]))
        if ld is None:
            return False
        ld.stopped, ld.next_at = True, None
    return True


def wants_demo(phone10: str) -> bool:
    """A lead replying 'demo': you're told, they hear back."""
    with system_session() as db:
        ld = db.scalar(select(Lead).where(Lead.phone10 == phone10[-10:]))
        if ld is None:
            return False
        if ld.status in ("new", "talking"):
            ld.status = "demo"
        name, rest = ld.name, ld.restaurant
    tell_me(f"📞 *{name}*{f' ({rest})' if rest else ''} asked for a demo: +91 {phone10[-10:]}")
    _send(phone10[-10:], "Great! We'll call you shortly to set up a demo on your own menu. 🙂", False, "lead")
    return True


# ------------------------------------------------------------------ new restaurants on a trial

TRIAL = [
    ("day1", lambda t: t.created_at + timedelta(days=1), lambda t: (
        f"Welcome to {BRAND}, {t.name}! Quick start: 1) add your menu (Setup › Menu, or import a photo) 2) bill your first order "
        f"3) turn on bills on WhatsApp. Any trouble? WhatsApp or call {support()}.")),
    ("day3", lambda t: t.created_at + timedelta(days=3), None),          # only if no bill yet (see below)
    ("end4", lambda t: t.trial_ends_at - timedelta(days=4), lambda t: (
        f"Your {BRAND} free trial ends on {t.trial_ends_at.astimezone(IST):%d %b}. Keep everything running: choose a plan under "
        f"Settings › Billing.  ₹999 per outlet a month, everything included.")),
    ("end1", lambda t: t.trial_ends_at - timedelta(days=1), lambda t: (
        f"Your {BRAND} trial ends tomorrow. Pick a plan under Settings › Billing so billing doesn't stop. Questions? WhatsApp or call {support()}.")),
    ("ended", lambda t: t.trial_ends_at + timedelta(days=1), lambda t: (
        f"Your {BRAND} trial has ended. Your menu, bills and data are safe: choose a plan under Settings › Billing to carry on.")),
]


def run_trials() -> int:
    from ..billing.models import Subscription
    from ..billing.service import house
    from ..models import Bill, Tenant
    sent = 0
    out = []
    with system_session() as db:
        since = _now() - timedelta(days=30)
        for t in db.scalars(select(Tenant).where(Tenant.trial_ends_at.is_not(None), Tenant.created_at >= since)):
            if house(t.login_id):
                continue
            s = db.get(Subscription, t.id)
            if s and s.paid_until and s.paid_until > _now():
                continue
            nd = db.get(TrialNudge, t.id) or TrialNudge(tenant_id=t.id, sent=[])
            if nd not in db:
                db.add(nd)
            done = set(nd.sent or [])
            for key, when, text in TRIAL:
                if key in done or when(t) > _now() or when(t) < _now() - timedelta(days=2):
                    continue
                if key == "day3":
                    bills = db.scalar(select(func.count()).select_from(Bill).where(Bill.tenant_id == t.id)) or 0
                    if bills:
                        done.add(key)
                        continue
                    msg = (f"Hi from {BRAND}! We noticed no bill yet at {t.name}. Want us to set up your menu for you? "
                           "Send a photo of your menu here and we'll do it.")
                    out.append(("me", f"🧭 *{t.name}* ({t.login_id}): no bill yet on day 3. Owner +91 {t.owner_phone[-10:]}"))
                else:
                    msg = text(t)
                out.append((t.owner_phone[-10:], msg))
                done.add(key)
                break                                   # one a day at most
            nd.sent = sorted(done)
    for ph, msg in out:
        if ph == "me":
            tell_me(msg)
        else:
            sent += _send(ph, msg, False, "trial")
    return sent


def tick() -> None:
    """Every few minutes from the WhatsApp sender loop."""
    from ..whatsapp.health import _get, _put
    with system_session() as db:
        last = _get(db, "growth-tick")
        if last and datetime.fromisoformat(last["at"]) > _now() - timedelta(minutes=10):
            return
        _put(db, "growth-tick", {"at": _now().isoformat()})
    try:
        run_leads()
        run_trials()
    except Exception:  # noqa: BLE001
        log.exception("growth tick")


# ------------------------------------------------------------------ referrals

def ref_code(tenant_id) -> str:
    from ..models import Tenant
    with system_session() as db:
        rc = db.get(RefCode, tenant_id)
        if rc:
            return rc.code
        t = db.get(Tenant, tenant_id)
        base = re.sub(r"[^A-Z0-9]", "", (t.login_id or "EMB").upper())[:6] or "EMB"
        for _ in range(8):
            code = f"{base}{secrets.choice('23456789')}{secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
            if db.scalar(select(RefCode).where(RefCode.code == code)) is None:
                db.add(RefCode(tenant_id=t.id, code=code))
                return code
        code = secrets.token_hex(4).upper()
        db.add(RefCode(tenant_id=t.id, code=code))
        return code


def referrer_name(code: str) -> str | None:
    from ..models import Tenant
    with system_session() as db:
        rc = db.scalar(select(RefCode).where(RefCode.code == (code or "").upper()))
        t = db.get(Tenant, rc.tenant_id) if rc else None
        return (t.app_name or t.name) if t else None


def on_signup(tenant_id, phone: str, ref: str | None) -> None:
    """A new restaurant: its lead moves to 'trial', and a referral is noted."""
    try:
        ph = p10(phone)
    except ValueError:
        ph = None
    with system_session() as db:
        ld = db.scalar(select(Lead).where(Lead.phone10 == ph)) if ph else None
        code = (ref or (ld.ref_code if ld else None) or "").upper()
        if ld is not None:
            ld.tenant_id, ld.status, ld.next_at = tenant_id, "trial", None
        rc = db.scalar(select(RefCode).where(RefCode.code == code)) if code else None
        if rc and rc.tenant_id != tenant_id and db.scalar(select(Referral).where(Referral.referred_id == tenant_id)) is None:
            db.add(Referral(id=uuid.uuid4(), referrer_id=rc.tenant_id, referred_id=tenant_id))
    tell_me(f"🎉 New restaurant signed up{f' (referred, code {code})' if code else ''}. Check All restaurants.")


def on_first_payment(db, tenant) -> None:
    """Billing: the restaurant paid. Its lead is 'paid'; if it was referred, both get 31 days free (once)."""
    from ..billing.models import Subscription
    from ..models import Tenant
    ld = db.scalar(select(Lead).where(Lead.tenant_id == tenant.id))
    if ld is not None:
        ld.status = "paid"
    r = db.scalar(select(Referral).where(Referral.referred_id == tenant.id, Referral.status == "signed").with_for_update())
    if r is None:
        return
    gift = timedelta(days=31)
    for tid in (r.referred_id, r.referrer_id):
        s = db.get(Subscription, tid)
        t = db.get(Tenant, tid)
        if s is not None and s.paid_until and s.paid_until > _now():
            s.paid_until += gift
        elif t is not None:
            t.trial_ends_at = max(t.trial_ends_at or _now(), _now()) + gift
    r.status, r.rewarded_at = "rewarded", _now()
    ref = db.get(Tenant, r.referrer_id)
    if ref is not None:
        _send(ref.owner_phone[-10:], f"🎁 {tenant.app_name or tenant.name} joined {BRAND} with your link: you both get a month free. Thank you!",
              False, "referral")


def referral_info(tenant_id) -> dict:
    code = ref_code(tenant_id)
    with system_session() as db:
        rows = db.scalars(select(Referral).where(Referral.referrer_id == tenant_id)).all()
    link = f"{site()}/start?ref={code}"
    return {"code": code, "link": link, "signed": len(rows), "rewarded": sum(1 for r in rows if r.status == "rewarded"),
            "share": f"I run my restaurant on {BRAND}: billing, bills on WhatsApp, offers, stock and Tally in one app. "
                     f"Try it free for {get_settings().trial_days} days with my link and we both get a month free: {link}"}
