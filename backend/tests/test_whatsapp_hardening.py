"""wise-plus: WhatsApp that doesn't silently fail — retries, delivery ticks, reasons in words, 24-hour fallback,
alerts, broadcasts only from a restaurant's own number, sending from that number, its webhooks accepted."""
import hashlib
import hmac
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate  # noqa: E402,F401
from tests.test_stock_extras import setup  # noqa: E402


class R:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(body)

    def json(self):
        return self._b


def _meta(monkeypatch, answers):
    """httpx.post answers in turn: (status, body). Records (url, json)."""
    import httpx

    from app.config import get_settings
    for k, v in {"wa_token": "shared-tok", "wa_phone_number_id": "111111", "wa_app_secret": "appsecret", "wa_update_template": "wise_update"}.items():
        monkeypatch.setattr(get_settings(), k, v)
    calls = []
    seq = list(answers)

    def post(url, json=None, timeout=None, headers=None):
        calls.append((url, json, headers))
        code, body = seq.pop(0) if seq else (200, {"messages": [{"id": f"wamid.{uuid.uuid4().hex}"}]})
        return R(code, body)
    monkeypatch.setattr(httpx, "post", post)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return calls


def _hook(client, body, secret="appsecret"):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/whatsapp/webhook", content=raw, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def test_retry_reasons_fallback_and_ticks(client, monkeypatch):
    from sqlalchemy import select

    from app.db import system_session
    from app.whatsapp import client as wa
    from app.whatsapp.models import WaMessage
    to = "9" + str(uuid.uuid4().int)[:9]
    calls = _meta(monkeypatch, [(500, {"error": {"code": 131000, "message": "x"}}), (200, {"messages": [{"id": "wamid.A1"}]})])
    with wa.context(purpose="owner"):
        wa.send_text(to, "hello")                                  # a hiccup, then it goes
    assert len(calls) == 2
    with system_session() as db:
        m = db.scalar(select(WaMessage).where(WaMessage.wamid == "wamid.A1"))
        assert m.status == "accepted" and m.tries == 2 and m.purpose == "owner"

    _meta(monkeypatch, [(400, {"error": {"code": 131026, "message": "undeliverable"}})])
    with pytest.raises(wa.WaError) as e:
        wa.send_text(to, "x")
    assert e.value.reason.startswith("Not on WhatsApp")

    calls = _meta(monkeypatch, [(400, {"error": {"code": 131047, "message": "re-engagement"}}), (200, {"messages": [{"id": "wamid.F1"}]})])
    wa.send_text(to, "Line one\nLine two")                         # 24 h passed: goes as the 'update' template
    assert calls[1][1]["type"] == "template" and calls[1][1]["template"]["name"] == "wise_update"
    assert calls[1][1]["template"]["components"][0]["parameters"][1]["text"] == "Line one · Line two"
    with system_session() as db:
        assert db.scalar(select(WaMessage.fallback).where(WaMessage.wamid == "wamid.F1")) is True

    # ticks never go backwards; read stays read
    st = lambda s, **x: {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [{"id": "wamid.A1", "status": s, **x}]}}]}]}  # noqa: E731
    assert _hook(client, st("read")).status_code == 200
    _hook(client, st("delivered"))
    with system_session() as db:
        assert db.scalar(select(WaMessage.status).where(WaMessage.wamid == "wamid.A1")) == "read"
    assert _hook(client, st("read"), secret="wrong").status_code == 403


def test_bill_ticks_unreachable_and_alerts(client, monkeypatch):
    from sqlalchemy import select, text

    from app.config import get_settings
    from app.custwa import service as cw
    from app.db import system_session
    from app.models import Customer
    from app.whatsapp import health
    from tests.test_banking import _bill
    s = setup(client)
    tok, ck, v = s["tok"], s["ck"], s["v"]
    _meta(monkeypatch, [])
    monkeypatch.setattr(get_settings(), "wa_bill_template", "bill_receipt")
    cust = "98" + str(uuid.uuid4().int)[:8]
    client.post("/api/shifts", headers=h(tok), json={"outlet_id": ck, "opening_cash_paise": 0})
    od = client.post("/api/orders", headers=h(tok), json={"outlet_id": ck, "order_type": "takeaway", "items": [{"variant_id": v, "qty": 1}]}).json()
    tot = client.post("/api/bills/preview", headers=h(tok), json={"order_id": od["id"]}).json()["total_paise"]
    b = client.post("/api/bills", headers=h(tok), json={"order_id": od["id"], "customer": {"phone": cust, "name": "Ravi"},
                                                        "payments": [{"mode": "cash", "amount_paise": tot}]}).json()
    with system_session() as db:
        db.execute(text("update outlets set wa_bills = true where id = :o"), {"o": ck})
        n = db.execute(text("select count(*) from wa_outbox where ref = :r"), {"r": b["id"]}).scalar()
    if not n:                                             # the outlet had WhatsApp bills off: queue it now
        with system_session() as db:
            from app.models import Bill
            cw.queue_bill(db, db.get(Bill, uuid.UUID(b["id"])))
    while cw.send_due():
        pass
    with system_session() as db:
        wamid = db.execute(text("select wamid from wa_outbox where ref = :r"), {"r": b["id"]}).scalar()
    assert wamid
    bills = client.get("/api/bills", headers=h(tok), params={"outlet_id": ck}).json()
    assert next(x for x in bills if x["id"] == b["id"])["wa"] == "sent"
    _hook(client, {"entry": [{"changes": [{"field": "messages", "value": {"statuses": [
        {"id": wamid, "status": "failed", "errors": [{"code": 131026, "title": "Message undeliverable"}]}]}}]}]})
    x = next(x for x in client.get("/api/bills", headers=h(tok), params={"outlet_id": ck}).json() if x["id"] == b["id"])
    assert x["wa"] == "failed" and x["wa_why"].startswith("Not on WhatsApp")
    with system_session() as db:
        assert db.scalar(select(Customer.wa_unreachable).where(Customer.phone == cust)) is True
    assert client.get("/api/loyalty/customer", headers=h(tok), params={"phone": cust}).json()["wa_unreachable"] is True

    # a template rejected → the platform owner is told, once
    monkeypatch.setattr(get_settings(), "house_tenants", s["login"])
    sent = []
    from app.whatsapp import client as wa
    monkeypatch.setattr(wa, "send_text", lambda to, body: sent.append((to, body)))
    name = "tpl" + uuid.uuid4().hex[:6]
    ev = {"entry": [{"changes": [{"field": "message_template_status_update", "value": {"event": "REJECTED", "message_template_name": name, "reason": "PROMOTIONAL"}}]}]}
    _hook(client, ev)
    _hook(client, ev)
    mine = [b for t, b in sent if name in b]
    assert len(mine) == 1 and "rejected" in mine[0] and sent[0][0] == s["phone"][-10:]
    # number flagged → broadcasts pause
    _hook(client, {"entry": [{"changes": [{"field": "phone_number_quality_update",
                                           "value": {"event": "FLAGGED", "display_phone_number": "15551465201", "current_limit": "TIER_1K"}}]}]})
    assert health.promos_paused()
    with system_session() as db:
        db.execute(text("delete from wa_health where key like 'quality:%'"))
    assert health.promos_paused() is None


def test_broadcast_only_from_own_number_and_routing(client, monkeypatch):
    from app.db import system_session
    from app.whatsapp import client as wa
    from app.whatsapp import numbers
    s = setup(client)
    tok = s["tok"]
    calls = _meta(monkeypatch, [])
    f = {"name": "Diwali", "text": "Hello from us", "filters": {}}
    pv = client.post("/api/broadcasts/preview", headers=h(tok), json={}).json()
    assert pv["send_from"] is None and "own WhatsApp number" in pv["blocked"]
    r = client.post("/api/broadcasts", headers=h(tok), json=f)
    assert r.status_code == 409

    # their own API, set up by hand: checked with Meta first
    import httpx

    def get(url, params=None, timeout=None, headers=None):
        return R(200, {"display_phone_number": "+91 98290 00001", "verified_name": "Test Cafe", "quality_rating": "GREEN"})
    monkeypatch.setattr(httpx, "request", lambda method, url, timeout=None, headers=None, **kw: get(url, **kw))
    out = client.post("/api/whatsapp/connect-manual", headers=h(tok), json={"phone_number_id": "222222", "token": "T" * 40,
                                                                          "app_secret": "their-app-secret-123"})
    assert out.status_code == 200, out.text
    assert client.get("/api/whatsapp/connection", headers=h(tok)).json()["mode"] == "own"
    pv = client.post("/api/broadcasts/preview", headers=h(tok), json={}).json()
    assert pv["send_from"] == "own" and pv["credits_each"] == 0 and not pv["blocked"]

    with system_session() as db:
        from sqlalchemy import text
        tid = db.execute(text("select id from tenants where login_id = :l"), {"l": s["login"]}).scalar()
    with wa.context(tenant_id=tid, purpose="promo"):
        wa.send_text("9812345678", "hi")
    assert "/222222/messages" in calls[-1][0] and calls[-1][2]["Authorization"] == "Bearer " + "T" * 40
    with wa.context(tenant_id=tid, via="111111"):              # a reply on the shared number stays on it
        wa.send_text("9812345678", "hi")
    assert "/111111/messages" in calls[-1][0]
    # their app's webhooks are accepted with their app secret
    assert _hook(client, {"entry": []}, secret="their-app-secret-123").status_code == 200
    numbers.remove(tid)
    assert _hook(client, {"entry": []}, secret="their-app-secret-123").status_code == 403


def test_marketing_stop_from_whatsapp(client, monkeypatch):
    from sqlalchemy import select

    from app.db import system_session
    from app.models import Customer
    from app.whatsapp import health
    _meta(monkeypatch, [])
    s = setup(client)
    p = "97" + str(uuid.uuid4().int)[:8]
    with system_session() as db:
        from sqlalchemy import text
        tid = db.execute(text("select id from tenants where login_id = :l"), {"l": s["login"]}).scalar()
        db.add(Customer(id=uuid.uuid4(), tenant_id=tid, phone=p, points_paise=0, stamps=0))
    _hook(client, {"entry": [{"changes": [{"field": "user_preferences", "value": {"user_preferences": [
        {"wa_id": "91" + p, "category": "marketing_messages", "value": "stop"}]}}]}]})
    with system_session() as db:
        assert db.scalar(select(Customer.wa_optout).where(Customer.phone == p)) is True
    health.on_account("user_preferences", {"user_preferences": [{"wa_id": "91" + p, "category": "marketing_messages", "value": "resume"}]})
    with system_session() as db:
        assert db.scalar(select(Customer.wa_optout).where(Customer.phone == p)) is False


def test_health_endpoint(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "wa_token", "")
    r = client.get("/api/health/whatsapp")
    assert r.status_code == 200 and r.json()["configured"] is False
