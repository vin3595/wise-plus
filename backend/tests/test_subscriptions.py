"""wise-plus billing: prices, trial, Razorpay checkout, founding members, read-only after the trial,
bill-scan allowance and packs, house accounts and the private Daily cash sheet."""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_api import client, h, signup  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("client")


def test_prices():
    from app.billing.plans import price
    assert price(["pos"], "monthly")["amount_paise"] == 59900
    assert price(["stock", "bills"], "monthly")["amount_paise"] == 59800
    assert price(["pos", "stock", "bills"], "monthly")["amount_paise"] == 99900
    y = price(["pos"], "yearly")
    assert y["amount_paise"] == 599000 and y["saving_paise"] == 119800              # pay 10, get 12
    b = price(["pos", "stock", "bills", "sheets"], "yearly")
    assert b["amount_paise"] == 899100 and b["modules"] == ["pos", "stock", "bills"]  # bundle: pay 9
    f = price(["pos"], "founding")
    assert f["amount_paise"] == 499900 and f["modules"] == ["pos", "stock", "bills"]
    with pytest.raises(ValueError):
        price(["sheets"], "monthly")


@pytest.fixture
def rzp(monkeypatch):
    from app.billing import rz
    from app.config import get_settings
    s = get_settings()
    for k, v in {"billing_rz_key_id": "rzp_test_k", "billing_rz_key_secret": "sec", "billing_rz_webhook_secret": "whs",
                 "house_tenants": "housex"}.items():
        monkeypatch.setattr(s, k, v)
    calls = []
    from app.billing.models import RzPlan
    from app.db import system_session
    with system_session() as db:      # plans are cached in the database; start each test clean
        db.query(RzPlan).delete()

    def call(method, path, body=None):
        calls.append((method, path, body))
        if path == "/plans":
            return {"id": "plan_" + uuid.uuid4().hex[:8]}
        if path == "/subscriptions":
            return {"id": "sub_" + uuid.uuid4().hex[:10]}
        if path == "/orders":
            return {"id": "order_" + uuid.uuid4().hex[:8]}
        if path.startswith("/orders/"):
            return {"id": path.split("/")[-1], "notes": calls_notes.get(path.split("/")[-1], {})}
        return {}
    calls_notes: dict = {}
    monkeypatch.setattr(rz, "call", call)
    return {"calls": calls, "notes": calls_notes}


def sig(msg: str, secret="sec"):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def expire_trial(login):
    from app.db import system_session
    from app.models import Tenant
    with system_session() as db:
        t = db.query(Tenant).filter(Tenant.login_id == login).one()
        t.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)


def test_trial_pay_and_lock(client, rzp):
    login = "bil" + uuid.uuid4().hex[:6]
    tok = signup(client, login, "93" + str(uuid.uuid4().int)[:8])["token"]
    b = client.get("/api/billing", headers=h(tok)).json()
    assert b["state"] == "trial" and b["ready"] and b["founding"]["slots"] == 50 and not b["house"]
    assert client.get("/api/tenant/modules", headers=h(tok)).json()["on"] == ["pos", "stock", "bills"]   # no daily sheet for others
    # Trial over: read-only, but billing still works
    expire_trial(login)
    r = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Metro"})
    assert r.status_code == 402 and "Settings › Billing" in r.json()["detail"]
    assert client.get("/api/billing", headers=h(tok)).json()["state"] == "locked"
    # Founding member
    r = client.post("/api/billing/subscribe", headers=h(tok), json={"modules": ["pos"], "period": "founding"})
    assert r.status_code == 200, r.text
    sub = r.json()["subscription_id"]
    plan_call = next(c for c in rzp["calls"] if c[1] == "/plans")
    assert plan_call[2]["item"]["amount"] == 499900 and plan_call[2]["period"] == "yearly"
    sub_call = next(c for c in rzp["calls"] if c[1] == "/subscriptions")
    assert sub_call[2]["total_count"] == 1 and "start_at" not in sub_call[2]          # trial over: starts now
    bad = client.post("/api/billing/verify", headers=h(tok), json={"razorpay_payment_id": "pay_1", "razorpay_subscription_id": sub, "razorpay_signature": "x"})
    assert bad.status_code == 400
    ok = client.post("/api/billing/verify", headers=h(tok), json={"razorpay_payment_id": "pay_1", "razorpay_subscription_id": sub,
                                                                 "razorpay_signature": sig(f"pay_1|{sub}")})
    assert ok.status_code == 200, ok.text
    b = ok.json()
    assert b["state"] == "paid" and b["founding"]["member"] and b["plan"]["period"] == "founding"
    assert client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Metro"}).status_code == 201
    # Webhook: renewal failed and halted -> after the grace days the app locks again
    body = json.dumps({"event": "subscription.halted", "payload": {"subscription": {"entity": {"id": sub, "status": "halted"}}}}).encode()
    assert client.post("/api/public/billing/razorpay", content=body, headers={"x-razorpay-signature": "nope"}).status_code == 400
    assert client.post("/api/public/billing/razorpay", content=body, headers={"x-razorpay-signature": hmac.new(b"whs", body, hashlib.sha256).hexdigest()}).status_code == 200
    from app.billing.models import Subscription
    from app.db import system_session
    from app.models import Tenant
    with system_session() as db:
        t = db.query(Tenant).filter(Tenant.login_id == login).one()
        s = db.get(Subscription, t.id)
        assert s.status == "halted"
        s.paid_until = datetime.now(timezone.utc) - timedelta(days=4)
    assert client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Metro 2"}).status_code == 402
    # Charged again -> paid till the new cycle end
    end = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())
    body = json.dumps({"event": "subscription.charged", "payload": {"subscription": {"entity": {"id": sub, "current_end": end}}}}).encode()
    client.post("/api/public/billing/razorpay", content=body, headers={"x-razorpay-signature": hmac.new(b"whs", body, hashlib.sha256).hexdigest()})
    assert client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Metro 2"}).status_code == 201


def test_plan_limits_services_and_scans(client, rzp):
    login = "bim" + uuid.uuid4().hex[:6]
    tok = signup(client, login, "92" + str(uuid.uuid4().int)[:8])["token"]
    # During the trial: choose a monthly Inventory + Bill scan plan; it starts when the trial ends
    sub = client.post("/api/billing/subscribe", headers=h(tok), json={"modules": ["stock", "bills"], "period": "monthly"}).json()
    assert sub["quote"]["amount_paise"] == 59800 and sub["starts"]
    sc = next(c for c in rzp["calls"] if c[1] == "/subscriptions")
    assert sc[2]["start_at"] > datetime.now(timezone.utc).timestamp() + 86400 * 10
    sid = sub["subscription_id"]
    client.post("/api/billing/verify", headers=h(tok), json={"razorpay_payment_id": "p", "razorpay_subscription_id": sid, "razorpay_signature": sig(f"p|{sid}")})
    assert client.get("/api/tenant/modules", headers=h(tok)).json()["on"] == ["stock", "bills"]
    r = client.put("/api/tenant/modules", headers=h(tok), json={"modules": ["pos", "stock", "bills"]})
    assert r.status_code == 402 and "POS billing isn't in your plan" in r.json()["detail"]
    assert client.put("/api/tenant/modules", headers=h(tok), json={"modules": ["stock"]}).status_code == 200
    # Scans: 150 a month, then packs
    from app.billing.models import Subscription
    from app.billing.service import ScanLimit, check_scan, count_scan
    from app.db import system_session
    from app.models import Tenant
    with system_session() as db:
        t = db.query(Tenant).filter(Tenant.login_id == login).one()
        s = db.get(Subscription, t.id)
        s.scan_month, s.scans_used = datetime.now(timezone.utc).astimezone().strftime("%Y-%m"), 150
        db.flush()
        with pytest.raises(ScanLimit):
            check_scan(db, t)
    order = client.post("/api/billing/packs/25/order", headers=h(tok)).json()
    rzp["notes"][order["order_id"]] = {"tenant_id": str(t.id), "pack": "25"}
    v = client.post("/api/billing/packs/verify", headers=h(tok), json={"razorpay_order_id": order["order_id"], "razorpay_payment_id": "pay_p",
                    "razorpay_signature": sig(f"{order['order_id']}|pay_p")}).json()
    assert "scans" in v, v
    assert v["scans"]["bonus"] == 25
    # The same payment arriving by webhook as well doesn't credit twice
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_p", "order_id": order["order_id"],
                       "notes": {"tenant_id": str(t.id), "pack": "25"}}}}}).encode()
    client.post("/api/public/billing/razorpay", content=body, headers={"x-razorpay-signature": hmac.new(b"whs", body, hashlib.sha256).hexdigest()})
    with system_session() as db:
        t = db.query(Tenant).filter(Tenant.login_id == login).one()
        check_scan(db, t)
        count_scan(db, t)
        assert db.get(Subscription, t.id).bonus_scans == 24


def test_house_account(client, rzp, monkeypatch):
    from app.config import get_settings
    login = "hse" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(get_settings(), "house_tenants", f"someone, {login.upper()}")
    tok = signup(client, login, "91" + str(uuid.uuid4().int)[:8])["token"]
    b = client.get("/api/billing", headers=h(tok)).json()
    assert "scans" in b, b
    assert b["state"] == "house" and b["scans"]["limit"] is None
    assert client.get("/api/tenant/modules", headers=h(tok)).json()["on"] == ["pos", "stock", "bills", "sheets"]
    expire_trial(login)
    assert client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Metro"}).status_code == 201
    assert client.post("/api/billing/subscribe", headers=h(tok), json={"modules": ["pos"], "period": "monthly"}).status_code == 409
