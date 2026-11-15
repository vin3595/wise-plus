"""wise-plus: supplier dues, the owner's keyword answers, and WhatsApp (Meta faked)."""
import hashlib
import hmac
import json
import os
import uuid

import pytest

from app.assistant.answers import answer, rs

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="set WISE_RUN_API_TESTS=1 and start Postgres")


def test_rupees():
    assert rs(12345678) == "₹1,23,456.78" and rs(100) == "₹1" and rs(-50000) == "-₹500" and rs(99999900) == "₹9,99,999"


@pytest.fixture()
def shop():
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_api import h, signup

    c = TestClient(app)
    login = "ledger" + uuid.uuid4().hex[:6]
    phone = "97" + str(uuid.uuid4().int)[:8]
    tok = signup(c, login, phone)["token"]
    brand = c.get("/api/tenant/brands", headers=h(tok)).json()[0]["id"]
    out = c.post("/api/tenant/outlets", headers=h(tok), json={"brand_id": brand, "name": "Sardarpura", "code": "SP"}).json()
    kanha = c.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Kanha Food Products"}).json()
    vad = c.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Shiv Gauri Agencies (Vadilal)"}).json()
    van = c.post("/api/inventory/items", headers=h(tok), json={"name": "Super Vanilla Bulk 5L", "unit": "ml",
                                                              "purchase_units": [{"name": "tub 5L", "factor": 5000}]}).json()
    bc = c.post("/api/inventory/items", headers=h(tok), json={"name": "Blueberry Cheesecake", "unit": "pc",
                                                             "purchase_units": [{"name": "box", "factor": 1}]}).json()

    def buy(sup, item, unit, qty, amount, day, inv):
        r = c.post("/api/inventory/purchases", headers=h(tok), json={
            "outlet_id": out["id"], "supplier_id": sup["id"], "invoice_no": inv, "invoice_date": day,
            "lines": [{"stock_item_id": item["id"], "qty_m": qty * 1000, "unit": unit, "amount_paise": amount, "gst_bp": 500}]})
        assert r.status_code == 201, r.text
        return r.json()

    buy(vad, van, "tub 5L", 4, 140000, "2026-10-20", "TS005001")      # ₹350 a tub
    buy(vad, van, "tub 5L", 4, 151656, "2026-11-07", "TS005689")      # ₹379.14 a tub, 1,592.39 with GST
    buy(kanha, bc, "box", 2, 217688, "2026-11-07", "5043")            # 2,285.72 with GST
    return {"c": c, "h": h, "tok": tok, "login": login, "phone": phone, "out": out, "kanha": kanha, "vad": vad}


def test_dues_payments_and_answers(shop):
    c, h, tok = shop["c"], shop["h"], shop["tok"]
    kanha, vad = shop["kanha"], shop["vad"]
    # Kanha: opening ₹10,000 owed from before Wise; paid ₹5,000 by UPI; ₹1,000 cash from the drawer.
    assert c.post("/api/ledger/entries", headers=h(tok), json={"supplier_id": kanha["id"], "kind": "opening", "entry_date": "2026-10-01",
                                                               "amount_paise": 1000000}).status_code == 201
    r = c.post("/api/ledger/entries", headers=h(tok), json={"supplier_id": kanha["id"], "kind": "payment", "entry_date": "2026-11-10",
                                                           "amount_paise": 500000, "mode": "upi", "reference": "UTR123"})
    assert r.status_code == 201 and r.json()["balance_paise"] == 1000000 + 228572 - 500000
    assert c.post("/api/ledger/entries", headers=h(tok), json={"supplier_id": kanha["id"], "kind": "payment", "entry_date": "2026-11-10",
                                                               "amount_paise": 500000}).status_code == 422      # how was it paid?
    pin = c.post("/api/users", headers=h(tok), json={"name": "Asha", "role": "cashier", "pin": "7311", "outlet_ids": [shop["out"]["id"]]})
    assert pin.status_code in (200, 201), pin.text
    asha = c.post("/api/auth/pin", json={"login_id": shop["login"], "outlet_id": shop["out"]["id"], "pin": "7311"}).json()["token"]
    sh = c.post("/api/shifts", headers=h(asha), json={"outlet_id": shop["out"]["id"], "opening_cash_paise": 500000}).json()
    r = c.post(f"/api/shifts/{sh['id']}/cash", headers=h(asha), json={"kind": "out", "amount_paise": 100000, "purpose": "supplier",
                                                                     "paid_to": "kanha food products ", "reason": "Kanha"})
    assert r.status_code == 200, r.text

    b = c.get("/api/ledger/suppliers", headers=h(tok)).json()
    by = {s["name"]: s for s in b["suppliers"]}
    assert by["Kanha Food Products"]["balance_paise"] == 1000000 + 228572 - 500000 - 100000
    assert by["Shiv Gauri Agencies (Vadilal)"]["balance_paise"] == 147000 + 159239
    assert b["total_paise"] == 628572 + 306239
    st = c.get(f"/api/ledger/suppliers/{kanha['id']}", headers=h(tok)).json()
    assert [e["kind"] for e in st["entries"]] == ["opening", "bill", "payment", "cash_drawer"] and st["balance_paise"] == 628572
    assert c.get("/api/ledger/suppliers", headers=h(asha)).status_code == 403          # cashiers don't see dues

    ask = lambda q: c.post("/api/assistant/ask", headers=h(tok), json={"q": q}).json()["text"]  # noqa: E731
    t = ask("dues")
    assert "Supplier dues: ₹9,348.11" in t and t.index("Kanha") < t.index("Vadilal")
    t = ask("Dues kanha")
    assert "Kanha Food Products: ₹6,285.72 due" in t and "UTR123" in t
    t = ask("last order from vadilal")
    assert "TS005689" in t and "Super Vanilla Bulk 5L: 4 tub 5L" in t and "2 bill(s)" in t
    t = ask("price vanilla")
    assert "₹379.14 per tub 5L" in t and "₹350 per tub 5L on 20 Oct" in t and "▲ 8.3%" in t
    assert "Which item?" in ask("price xyzzy pqr") or "No item" in ask("price xyzzy pqr")
    assert "Ask me" in ask("help") and "didn't get" in ask("weather")
    assert "No bills yet" in ask("sales")

    # Cancelling the UPI payment puts it back
    pay = next(e for e in st["entries"] if e["kind"] == "payment")
    assert c.post(f"/api/ledger/entries/{pay['id']}/cancel", headers=h(tok)).status_code == 200
    assert c.get(f"/api/ledger/suppliers/{kanha['id']}", headers=h(tok)).json()["balance_paise"] == 1128572


def test_whatsapp(shop, monkeypatch):
    from app.config import get_settings
    from app.whatsapp import client as wa
    from app.whatsapp import router as wr

    c = shop["c"]
    s = get_settings()
    monkeypatch.setattr(s, "wa_app_secret", "shh")
    monkeypatch.setattr(s, "wa_verify_token", "vt")
    monkeypatch.setattr(s, "wa_token", "t")
    monkeypatch.setattr(s, "wa_phone_number_id", "123")
    sent = []
    monkeypatch.setattr(wa, "send_text", lambda to, body: sent.append((to, body)))

    # Meta's one-time check
    assert c.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}).text == "42"
    assert c.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code == 403

    def post(frm, text, mid=None, secret="shh"):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": mid or uuid.uuid4().hex, "from": frm, "type": "text",
                                                                         "text": {"body": text}}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return c.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig, "content-type": "application/json"})

    assert post("91" + shop["phone"], "dues", secret="wrong").status_code == 403       # not from Meta
    assert post("91" + shop["phone"], "dues", mid="m1").status_code == 200
    assert post("91" + shop["phone"], "dues", mid="m1").status_code == 200              # Meta's retry: answered once
    assert len(sent) == 1 and sent[0][0] == "91" + shop["phone"] and "Supplier dues" in sent[0][1]
    post("919999999999", "dues")                                                         # a stranger: no answer at all
    assert len(sent) == 1
    post("91" + shop["phone"], "price vanilla")
    assert "₹379.14 per tub 5L" in sent[-1][1]

    # The same owner on a second restaurant: asked to pick, then remembered
    from tests.test_api import signup
    second = "ledger2" + uuid.uuid4().hex[:5]
    signup(c, second, shop["phone"])
    wr._choice.clear()
    assert "more than one restaurant" in wr.reply_for("91" + shop["phone"], "dues")
    assert "Kanha" in wr.reply_for("91" + shop["phone"], f"@{shop['login']} dues")
    assert f"@{shop['login']}" in wr.reply_for("91" + shop["phone"], "last kanha")
    assert "Nothing outstanding" in wr.reply_for("91" + shop["phone"], f"@{second} dues")
    assert "not on that restaurant" in wr.reply_for("91" + shop["phone"], "@someoneelse dues")
