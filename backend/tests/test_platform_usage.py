"""wise-plus: the platform owner sees every restaurant's WhatsApp messages, credits and scans; nobody else does."""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate  # noqa: E402,F401
from tests.test_stock_extras import setup  # noqa: E402


def admin_tok(client, monkeypatch, phone="9000011111"):
    """Sign in at /admin the real way: the code is caught on its way out."""
    from app.config import get_settings
    from app.services import otp
    monkeypatch.setattr(get_settings(), "platform_admin_phones", phone)
    sent = {}
    monkeypatch.setattr(otp, "_deliver", lambda ident, code, settings=None: sent.update(code=code))
    assert client.post("/api/admin/login/start", json={"phone": phone}).json() == {"sent": True}
    r = client.post("/api/admin/login/verify", json={"phone": phone, "code": sent["code"]})
    assert r.status_code == 200
    return r.json()["token"]


def test_platform_usage_only_for_house_owner(client, monkeypatch):
    from sqlalchemy import text

    from app.config import get_settings
    from app.db import system_session
    from app.whatsapp import router as wr
    house = setup(client)
    other = setup(client)
    monkeypatch.setattr(get_settings(), "house_tenants", house["login"])
    adm = admin_tok(client, monkeypatch, house["phone"])
    # your restaurant login is a restaurant login: no platform pages there
    assert client.get("/api/platform/usage", headers=h(other["tok"])).status_code == 404
    assert client.get("/api/platform/usage", headers=h(house["tok"])).status_code == 404
    assert client.get("/api/billing", headers=h(house["tok"])).json()["platform_admin"] is False
    # someone else asking for a code gets the same answer and no code
    assert client.post("/api/admin/login/start", json={"phone": other["phone"]}).json() == {"sent": True}
    assert client.post("/api/admin/login/verify", json={"phone": other["phone"], "code": "123456"}).status_code == 400
    with system_session() as db:
        tid = db.execute(text("select id from tenants where login_id = :l"), {"l": other["login"]}).scalar()
        for kind, st in (("bill", "sent"), ("bill", "sent"), ("promo", "sent"), ("bill", "failed")):
            db.execute(text("insert into wa_outbox (id, tenant_id, kind, ref, to_phone, payload, status, tries) "
                            "values (cast(:i as uuid), :t, :k, :r, '919999999999', '{}', :s, 1)"), {"i": str(uuid.uuid4()), "r": uuid.uuid4().hex, "t": tid, "k": kind, "s": st})
    u = client.get("/api/platform/usage", headers=h(adm)).json()
    me = next(r for r in u["restaurants"] if r["login_id"] == other["login"])
    assert me["sent"] == {"bill": 2, "promo": 1} and me["failed"] == 1
    assert me["meta_cost_paise"] == round(2 * 11.5 + 86.31) and me["bill_msgs_free"] == 500 * me["outlets"]
    assert next(r for r in u["restaurants"] if r["login_id"] == house["login"])["bill_msgs_free"] is None   # house: unlimited
    assert client.get("/api/platform/usage", headers=h(adm), params={"month": "bad"}).status_code == 422
    wr._choice.clear()
    r = wr.reply_rich("91" + house["phone"], "credits")
    assert "across all restaurants" in r.text and "Meta cost" in r.text
    r = wr.reply_rich("91" + other["phone"], "credits")
    assert "across all restaurants" not in r.text


def test_platform_owner_pastes_razorpay_keys(client, monkeypatch):
    from sqlalchemy import text

    from app.billing import rz
    from app.config import get_settings
    from app.db import system_session
    house = setup(client)
    other = setup(client)
    monkeypatch.setattr(get_settings(), "house_tenants", house["login"])
    adm = admin_tok(client, monkeypatch)
    with system_session() as db:
        db.execute(text("delete from platform_secrets"))
    seen = []

    def fake(method, path, body=None, auth=None):
        seen.append(auth)
        if auth and auth[1] == "wrong-secret-x":
            raise RuntimeError("Razorpay: Authentication failed")
        return {"items": []}
    monkeypatch.setattr(rz, "call", fake)
    for t in (other["tok"], house["tok"]):
        assert client.post("/api/platform/razorpay", headers=h(t), json={"key_id": "rzp_live_a", "key_secret": "s" * 20}).status_code == 404
    r = client.post("/api/platform/razorpay", headers=h(adm), json={"key_id": "rzp_live_a", "key_secret": "wrong-secret-x"})
    assert r.status_code == 422 and "didn't accept" in r.json()["detail"]
    r = client.post("/api/platform/razorpay", headers=h(adm), json={"key_id": "rzp_live_a", "key_secret": "s" * 20}).json()
    assert r["ok"] and len(r["webhook_secret"]) > 20
    k = rz.keys()
    assert k == {"key_id": "rzp_live_a", "key_secret": "s" * 20, "webhook_secret": r["webhook_secret"]} and rz.configured()
    with system_session() as db:
        assert "s" * 20 not in str(db.execute(text("select value_enc from platform_secrets")).all())   # sealed, not plain
    g = client.get("/api/platform/razorpay", headers=h(adm)).json()
    assert g["key_id"] == "rzp_live_a" and g["has_secret"] and "key_secret" not in g
    # pasting again keeps the webhook secret unless asked for a new one
    r2 = client.post("/api/platform/razorpay", headers=h(adm), json={"key_id": "rzp_live_b", "key_secret": "t" * 20}).json()
    assert r2["webhook_secret"] is None and rz.keys()["webhook_secret"] == r["webhook_secret"]
    with system_session() as db:
        db.execute(text("delete from platform_secrets"))


def test_new_razorpay_webhook_secret(client, monkeypatch):
    from app.billing import rz
    monkeypatch.setattr(rz, "configured", lambda: True)
    saved = {}
    monkeypatch.setattr(rz, "save_keys", lambda v: saved.update(v))
    adm = admin_tok(client, monkeypatch)
    r = client.post("/api/platform/razorpay/webhook-secret", headers=h(adm), json={})
    assert r.status_code == 200 and len(r.json()["webhook_secret"]) > 20
    assert saved["webhook_secret"] == r.json()["webhook_secret"]
    assert r.json()["webhook_url"].endswith("/api/public/billing/razorpay")


def test_admin_login_codes(client, monkeypatch):
    from app.services import otp as otp_mod
    adm = admin_tok(client, monkeypatch)
    g = client.get("/api/platform/login-codes", headers=h(adm)).json()
    assert g["phone"] in ("whatsapp", "sms")
    assert client.post("/api/platform/login-codes", headers=h(adm), json={"phone": "sms"}).status_code == 422   # no SMS key yet
    monkeypatch.setattr(otp_mod, "send_mail", lambda *a, **k: (_ for _ in ()).throw(Exception("535 Username and Password not accepted")))
    r = client.post("/api/platform/login-codes", headers=h(adm), json={"mail_user": "a@gmail.com", "mail_password": "abcd efgh ijkl mnop"})
    assert r.status_code == 422 and "app password" in r.json()["detail"]
    monkeypatch.setattr(otp_mod, "send_mail", lambda *a, **k: None)
    r = client.post("/api/platform/login-codes", headers=h(adm), json={"mail_user": "a@gmail.com", "mail_password": "abcd efgh ijkl mnop"}).json()
    assert r["test_email_to"] == "a@gmail.com" and r["has_mail_password"]
    from sqlalchemy import text
    from app.config import get_settings
    from app.db import system_session
    with system_session() as db:
        db.execute(text("delete from platform_secrets where key in ('mail_user', 'mail_password', 'phone_otp')"))
    monkeypatch.setattr(get_settings(), "mail_user", ""); monkeypatch.setattr(get_settings(), "mail_password", "")
