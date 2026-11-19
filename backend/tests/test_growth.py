"""wise-plus growth: website leads (consent, honeypot, demo slots), follow-ups on schedule, stop, sign-up links the lead,
referral → both get a month on first payment, the Leads page only for you."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate  # noqa: E402,F401
from tests.test_stock_extras import setup  # noqa: E402


def _wa(monkeypatch):
    from app.growth import service as gs
    out = []
    monkeypatch.setattr(gs, "_send", lambda ph, text, marketing, purpose: out.append((ph, text, marketing, purpose)) or True)
    return out


def test_lead_flow_followups_stop_and_admin(client, monkeypatch):
    from sqlalchemy import select

    from app.config import get_settings
    from app.db import system_session
    from app.growth import service as gs
    from app.growth.models import Lead
    sent = _wa(monkeypatch)
    boss = setup(client)
    monkeypatch.setattr(get_settings(), "house_tenants", boss["login"])
    info = client.get("/api/public/growth/info").json()
    assert info["bundle_paise"] == 99900 and info["founding_left"] >= 0 and info["trial_days"] == get_settings().trial_days
    slots = client.get("/api/public/growth/demo-slots").json()["slots"]
    assert slots and all("T" in s for s in slots)
    phone = "9" + str(uuid.uuid4().int)[:9]
    body = {"name": "Ramesh Kumar", "phone": phone, "restaurant": "Chai Point", "city": "Jodhpur", "outlets": 2, "uses_now": "Petpooja",
            "consent": True, "demo_at": slots[0], "utm": {"utm_source": "instagram", "utm_campaign": "nov-jodhpur", "evil": "x"}}
    r = client.post("/api/public/growth/leads", json=body, headers={"x-forwarded-for": "10.1.1." + phone[-2:]})
    assert r.status_code == 201, r.text
    assert r.json()["trial_url"] == f"/signup?phone={phone}"
    assert any(p == phone and "Namaste Ramesh" in t and "demo" in t.lower() for p, t, *_ in sent)        # welcome to them
    assert any(p == boss["phone"][-10:] and "Lead: *Ramesh Kumar*" in t and "nov-jodhpur" in t for p, t, *_ in sent)   # and to you
    assert client.post("/api/public/growth/leads", json=body | {"phone": "9" + str(uuid.uuid4().int)[:9]},
                       headers={"x-forwarded-for": "10.9.9.9"}).status_code == 409                      # that slot is taken now
    bot = client.post("/api/public/growth/leads", json={"name": "Bot", "phone": "9" + str(uuid.uuid4().int)[:9], "website": "spam"},
                      headers={"x-forwarded-for": "10.2.2.2"})
    assert bot.status_code == 201
    with system_session() as db:
        ld = db.scalar(select(Lead).where(Lead.phone10 == phone))
        assert ld.utm == {"utm_source": "instagram", "utm_campaign": "nov-jodhpur"} and ld.status == "demo"
        ld.next_at = datetime.now(timezone.utc) - timedelta(minutes=1)            # pretend a day passed
    n = len(sent)
    gs.run_leads()
    first = [t for p, t, m, _ in sent[n:] if p == phone]
    assert first and f"free for {get_settings().trial_days} days" in first[0].lower() and sent[n][2] is True                         # follow-up 1, marketing
    gs.run_leads()
    assert len([1 for p, *_ in sent if p == phone]) == 2                         # not again until it's due
    assert gs.stop(phone)
    with system_session() as db:
        ld = db.scalar(select(Lead).where(Lead.phone10 == phone))
        assert ld.stopped and ld.next_at is None

    # your Leads page; nobody else's
    other = setup(client)
    from tests.test_platform_usage import admin_tok
    adm = admin_tok(client, monkeypatch, boss["phone"])
    for t in (other["tok"], boss["tok"]):       # restaurant logins, your own included: no Leads page
        assert client.get("/api/platform/leads", headers=h(t)).status_code == 404
    lst = client.get("/api/platform/leads", headers=h(adm)).json()
    me = next(x for x in lst["leads"] if x["phone"] == phone)
    assert me["campaign"] == "nov-jodhpur" and lst["funnel"].get("demo", 0) >= 1
    e = client.patch(f"/api/platform/leads/{me['id']}", headers=h(adm), json={"status": "talking", "notes": "Wants 2 outlets"}).json()
    assert e["status"] == "talking" and e["notes"] == "Wants 2 outlets"


def test_trial_nudges(client, monkeypatch):
    from app.db import system_session
    from app.growth import service as gs
    from app.models import Tenant
    sent = _wa(monkeypatch)
    s = setup(client)
    with system_session() as db:
        from sqlalchemy import select
        t = db.scalar(select(Tenant).where(Tenant.login_id == s["login"]))
        t.created_at = datetime.now(timezone.utc) - timedelta(days=1, hours=1)
        t.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=13)
    gs.run_trials()
    mine = [x for x in sent if x[0] == s["phone"][-10:]]
    assert len(mine) == 1 and "Quick start" in mine[0][1] and mine[0][2] is False
    gs.run_trials()
    assert len([x for x in sent if x[0] == s["phone"][-10:]]) == 1                # once only


def test_referral_month_free_on_first_payment(client, monkeypatch):
    from sqlalchemy import select

    from app.billing.models import Subscription
    from app.db import system_session
    from app.growth import service as gs
    from app.growth.models import Referral
    from app.models import Tenant
    _wa(monkeypatch)
    a = setup(client)
    ra = client.get("/api/growth/referral", headers=h(a["tok"])).json()
    assert ra["link"].endswith(f"/start?ref={ra['code']}") and "month free" in ra["share"]
    assert client.get(f"/api/public/growth/ref/{ra['code'].lower()}").json()["restaurant"]
    b = setup(client)
    with system_session() as db:
        tb = db.scalar(select(Tenant).where(Tenant.login_id == b["login"]))
        ta = db.scalar(select(Tenant).where(Tenant.login_id == a["login"]))
        bid, aid = tb.id, ta.id
        a_trial = ta.trial_ends_at
    gs.on_signup(bid, b["phone"], ra["code"])
    gs.on_signup(bid, b["phone"], ra["code"])                                     # twice: still one referral
    with system_session() as db:
        assert len(db.scalars(select(Referral).where(Referral.referred_id == bid)).all()) == 1
        tb = db.get(Tenant, bid)
        s = db.get(Subscription, bid) or Subscription(tenant_id=bid)
        if s not in db:
            db.add(s)
        s.paid_until = datetime.now(timezone.utc) + timedelta(days=31)
        before = s.paid_until
        db.flush()
        gs.on_first_payment(db, tb)
        gs.on_first_payment(db, tb)                                               # once only
        assert (s.paid_until - before).days == 31
        ta = db.get(Tenant, aid)
        assert ta.trial_ends_at >= max(a_trial or datetime.now(timezone.utc), datetime.now(timezone.utc)) + timedelta(days=30)
        assert db.scalar(select(Referral.status).where(Referral.referred_id == bid)) == "rewarded"


def test_signup_carries_ref(client, monkeypatch):
    from sqlalchemy import select

    from app.db import system_session
    from app.growth.models import Referral
    _wa(monkeypatch)
    a = setup(client)
    code = client.get("/api/growth/referral", headers=h(a["tok"])).json()["code"]
    login = "r" + uuid.uuid4().hex[:8]
    phone = "97" + str(uuid.uuid4().int)[:8]
    body = {"login_id": login, "restaurant_name": "New Cafe", "owner_name": "Neha", "email": f"{login}@x.in", "phone": phone, "ref": code}
    st = client.post("/api/auth/signup/start", json=body).json()
    r = client.post("/api/auth/signup/verify", json=body | {"otp": st.get("dev_otp") or "000000"})
    assert r.status_code == 200, r.text
    with system_session() as db:
        from app.models import Tenant
        tid = db.scalar(select(Tenant.id).where(Tenant.login_id == login))
        assert db.scalar(select(Referral).where(Referral.referred_id == tid)) is not None
