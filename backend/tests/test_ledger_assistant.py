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
    assert "What do you need" in ask("help") and "Ask me" in ask("how") and "didn't get" in ask("weather")
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
    monkeypatch.setattr(wa, "send_options", lambda to, body, options, button="Choose": sent.append((to, body, options)))

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
    assert ("dues Kanha Food Products", "Kanha Food Products") in sent[0][2]
    post("91" + shop["phone"], "dues Kanha Food Products")              # a list tap comes back as its command
    assert "Kanha Food Products: ₹2,285.72 due" in sent[-1][1] and sent[-1][2] == [("last Kanha Food Products", "Last order"), ("dues", "All dues")]
    n = len(sent)
    post("919999999999", "dues")                                                         # a stranger: no answer at all
    assert len(sent) == n
    post("91" + shop["phone"], "price vanilla")
    assert "₹379.14 per tub 5L" in sent[-1][1]

    # The same owner on a second restaurant: asked to pick, then remembered
    from tests.test_api import signup
    second = "ledger2" + uuid.uuid4().hex[:5]
    signup(c, second, shop["phone"])
    wr._choice.clear()
    r = wr.reply_rich("91" + shop["phone"], "dues")
    assert "more than one restaurant" in r.text and (f"@{shop['login']} dues", f"@{shop['login']}") in r.options
    assert "Kanha" in wr.reply_for("91" + shop["phone"], f"@{shop['login']} dues")
    assert f"@{shop['login']}" in wr.reply_for("91" + shop["phone"], "last kanha")
    assert "Nothing outstanding" in wr.reply_for("91" + shop["phone"], f"@{second} dues")
    assert "not on that restaurant" in wr.reply_for("91" + shop["phone"], "@someoneelse dues")


def test_whatsapp_bill_photo(shop, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.purchases_ai import extraction as ex
    from app.whatsapp import client as wa
    from app.whatsapp import router as wr

    c, h, tok = shop["c"], shop["h"], shop["tok"]
    s = get_settings()
    for k, v in {"wa_app_secret": "shh", "wa_token": "t", "wa_phone_number_id": "123", "upload_dir": str(tmp_path)}.items():
        monkeypatch.setattr(s, k, v)
    sent, opts = [], []
    monkeypatch.setattr(wa, "send_options", lambda to, body, options, button="Choose": (sent.append(body), opts.append(options)))
    monkeypatch.setattr(wa, "get_media", lambda mid: (b"\xff\xd8photo", "image/jpeg"))
    monkeypatch.setattr(ex, "EMERGENT_LLM_KEY", "k")
    monkeypatch.setattr(ex, "get_first_page_png", lambda d, ct, n: b"png")

    async def classify(png):
        return {"type": "INVOICE", "confidence": 95, "accepted": True, "should_warn": False}
    bill = {"vendor": "SHIV GAURI AGENCIES", "invoice_number": "TS005700", "bill_date": "2026-11-14", "total": 796.0,
            "items": [{"name": "Super Vanilla Bulk [1*1] 5000ML", "quantity": 2, "unit_price": 379.14, "total": 796.19, "tax_rate": 5}]}
    reads = [bill]

    async def read(d, ct, n):
        return reads[-1], "data:image/png;base64,AAAA"
    monkeypatch.setattr(ex, "classify_document", classify)
    monkeypatch.setattr(ex, "extract_bill_from_file", read)
    # The Vadilal supplier is known by name only ("Shiv Gauri Agencies (Vadilal)"), close enough to match.

    def post(msg):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": uuid.uuid4().hex, "from": "91" + shop["phone"], **msg}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        assert c.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200

    wr._choice.clear()
    assert "Nothing waiting" not in wr.reply_for("91" + shop["phone"], "save") and "No bill waiting" in wr.reply_for("91" + shop["phone"], "save")
    before = c.get(f"/api/ledger/suppliers/{shop['vad']['id']}", headers=h(tok)).json()["balance_paise"]
    post({"type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}})
    assert "Shiv Gauri Agencies (Vadilal)" in sent[-1] and "2 tub 5L" in sent[-1] and [l for _, l in opts[-1]] == ["Save", "Discard"], sent[-1]
    save_cmd = opts[-1][0][0]
    assert save_cmd.startswith("save ")
    assert len(c.get("/api/inventory/purchases/scan/pending", headers=h(tok)).json()) == 1
    post({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": save_cmd, "title": "Save"}}})   # tapped
    assert sent[-1].startswith("✅ Saved") and opts[-1][1] == ("dues", "All dues") and "₹796" in sent[-1], sent[-1]
    after = c.get(f"/api/ledger/suppliers/{shop['vad']['id']}", headers=h(tok)).json()["balance_paise"]
    assert abs((after - before) - 79600) <= 2          # GST rounds per line
    assert c.get("/api/inventory/purchases/scan/pending", headers=h(tok)).json() == []
    assert "No bill waiting" in wr.reply_for("91" + shop["phone"], "save")                 # saved only once
    assert "already saved" in wr.reply_for("91" + shop["phone"], save_cmd)                 # tapping Save twice

    # Same bill again: flagged, can't save from WhatsApp, waits in the app
    post({"type": "document", "document": {"id": "m2", "mime_type": "image/jpeg", "filename": "b.jpg"}})
    assert "entered already" in sent[-1] and [l for _, l in opts[-1]] == ["Discard"]
    assert "Can't save it from here" in wr.reply_for("91" + shop["phone"], "save")
    assert "Discarded" in wr.reply_for("91" + shop["phone"], "discard")

    # An item Wise doesn't know: waits in the app with the draft, ready for the Purchases form
    reads.append({**bill, "invoice_number": "TS005701", "items": [{"name": "Rajbhog Cone 120ML", "quantity": 1, "total": 500, "tax_rate": 5}], "total": 525})
    post({"type": "image", "image": {"id": "m3", "mime_type": "image/jpeg", "caption": "sardarpura"}})
    assert "1 line(s) need a stock item" in sent[-1] and "/stock" in sent[-1]
    pend = c.get("/api/inventory/purchases/scan/pending", headers=h(tok)).json()
    assert len(pend) == 1 and pend[0]["unmatched"] == 1
    one = c.get(f"/api/inventory/purchases/scan/pending/{pend[0]['id']}", headers=h(tok)).json()
    assert one["draft"]["lines"][0]["bill_name"] == "Rajbhog Cone 120ML" and one["draft"]["thumbnail"]
    assert c.post(f"/api/inventory/purchases/scan/pending/{pend[0]['id']}/done", headers=h(tok), json={"discard": True}).status_code == 200
    assert c.get("/api/inventory/purchases/scan/pending", headers=h(tok)).json() == []
    assert list(tmp_path.rglob("*.jpg"))                                                     # the photos are kept

    # Vineet's Charu Marketing bill (15 Nov 2026): new supplier and four new items -> "add" makes them and saves
    reads.append({"vendor": "CHARU MARKETING", "gstin": "08FCJPB5750C1ZT", "invoice_number": "CM/26-27/18111", "bill_date": "2026-11-14",
                  "total": 5130.0, "taxable_amount": 4885.77, "cgst_amount": 122.14, "sgst_amount": 122.14,
                  "items": [{"name": "VEEBA PROFESSIONAL WHITE CHEESE DRESSING (1KG)", "quantity": 12, "unit_price": 171.43, "total": 2057.16, "tax_rate": 5},
                            {"name": "VEEBA PIZZA PASTA SAUCE", "quantity": 6, "unit_price": 171.43, "total": 1028.58, "tax_rate": 5},
                            {"name": "VEEBA TANDOORI MAYONNAISE 1 KG", "quantity": 6, "unit_price": 214.29, "total": 1285.74, "tax_rate": 5},
                            {"name": "VEEBA SCHEZWAN DIP AND STIR FRY SAUCE 1KG", "quantity": 3, "unit_price": 171.43, "total": 514.29, "tax_rate": 5}]})
    post({"type": "image", "image": {"id": "m5", "mime_type": "image/jpeg"}})
    assert [l for _, l in opts[-1]] == ["Add & save", "Discard"] and "CHARU MARKETING" in sent[-1], sent[-1]
    add_cmd = opts[-1][0][0]
    post({"type": "text", "text": {"body": add_cmd}})
    assert "Added supplier Charu Marketing" in sent[-1] and "Veeba Pizza Pasta Sauce" in sent[-1] and "✅ Saved" in sent[-1], sent[-1]
    items = {i["name"]: i for i in c.get("/api/inventory/items", headers=h(tok)).json()}
    cheese = items["Veeba Professional White Cheese Dressing (1kg)"]
    assert cheese["unit"] == "g" and cheese["purchase_units"][0] == {"name": "pack 1kg", "factor": 1000}
    assert items["Veeba Pizza Pasta Sauce"]["unit"] == "pc"
    t = c.post("/api/assistant/ask", headers=h(tok), json={"q": "dues charu"}).json()["text"]
    assert "Charu Marketing: ₹5,130" in t, t
    t = c.post("/api/assistant/ask", headers=h(tok), json={"q": "price tandoori mayo"}).json()["text"]
    assert "per pack 1kg" in t, t
    # Their next bill matches by itself and saves with one word
    reads.append({**reads[-1], "invoice_number": "CM/26-27/18200"})
    post({"type": "image", "image": {"id": "m6", "mime_type": "image/jpeg"}})
    first_save = opts[-1][0][0]
    assert first_save.startswith("save "), sent[-1]
    # A second bill arrives before the first is saved: each button saves its own bill; typed "save" asks which
    reads.append({**reads[-1], "invoice_number": "CM/26-27/18201", "total": 1050.0,
                  "items": [{"name": "VEEBA PIZZA PASTA SAUCE", "quantity": 5, "unit_price": 200, "total": 1000, "tax_rate": 5}]})
    post({"type": "image", "image": {"id": "m7", "mime_type": "image/jpeg"}})
    second_save = opts[-1][0][0]
    r = wr.reply_rich("91" + shop["phone"], "save")
    assert "2 bills are waiting" in r.text and {o[0] for o in r.options} == {first_save, second_save}
    post({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": first_save, "title": "Save"}}})
    assert "₹5,130" in sent[-1], sent[-1]                                                    # the first bill, not the latest
    post({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": second_save, "title": "Save"}}})
    assert "₹1,050" in sent[-1], sent[-1]

    # A cashier's phone is ignored, photo or not
    c.post("/api/users", headers=h(tok), json={"name": "Ravi", "role": "cashier", "pin": "7322", "phone": "9000000099", "outlet_ids": [shop["out"]["id"]]})
    n = len(sent)
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": "x9", "from": "919000000099", "type": "image",
                                                                     "image": {"id": "m4", "mime_type": "image/jpeg"}}]}}]}]}).encode()
    c.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()})
    assert len(sent) == n


def test_send_options_format(monkeypatch):
    from app.whatsapp import client as wa
    out = []
    monkeypatch.setattr(wa, "_post", lambda payload: out.append(payload) or {})
    wa.send_options("9829000001", "Add it?", [("save", "Save"), ("discard", "Discard")])
    i = out[-1]["interactive"]
    assert out[-1]["to"] == "919829000001" and i["type"] == "button" and i["action"]["buttons"][0] == {"type": "reply", "reply": {"id": "save", "title": "Save"}}
    wa.send_options("9829000001", "Which?", [(f"dues s{n}", "A very long supplier name number " + str(n)) for n in range(12)])
    i = out[-1]["interactive"]
    rows = i["action"]["sections"][0]["rows"]
    assert i["type"] == "list" and len(rows) == 10 and all(len(r["title"]) <= 24 for r in rows)
    monkeypatch.setattr(wa, "send_text", lambda to, body: out.append({"text": body}))
    wa.send_options("9829000001", "x" * 1500, [("save", "Save")])
    assert out[-2] == {"text": "x" * 1500} and out[-1]["interactive"]["body"]["text"] == "What next?"


SHEET_13_NOV = {  # Vineet's Shakewallah sheet for 13/11/2026, as the reader returns it
    "date": "2026-11-13",
    "cash_in": {"d500": 8, "d200": 5, "d100": 94, "d50": 73, "d20": 26, "d10": 63, "coins": 42, "total": 19242},
    "cash_out": {"d500": 43, "d200": 4, "d100": 96, "d50": 73, "d20": 94, "d10": 53, "coins": 67, "total": 0},
    "vijay_ras_bhandar": [{"name": "STRAWBERRY", "amount": 0}],
    "expenses": [{"name": "MILK", "amount": 1380}, {"name": "BROWNIE", "amount": 650}, {"name": "SNACK", "amount": 240},
                 {"name": "TEA", "amount": 60}, {"name": "15 AD", "amount": 555}, {"name": "PETROL", "amount": 500},
                 {"name": "VEGETABLE", "amount": 30}, {"name": "TAXI", "amount": 50}, {"name": "MUMMY", "amount": 0}],
    "expense_payouts": [], "vijay_ras": [{"name": "VANILLA", "amount": 6}, {"name": "KITKAT", "amount": 0}],
    "absent": ["Ankull", "Raj", "Aman", "Culter", "Prince", "Guard"],
    "advances": [{"name": "Ajeet", "amount": 4000}, {"name": "Guard", "amount": 200}],
    "dues": [{"name": "Ajeet", "amount": 4000}], "notes": "Shiva - 11:52",
}


def test_daily_sheet(shop, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.daily_sheet import extract as dx
    from app.purchases_ai import extraction as ex
    from app.whatsapp import client as wa
    from app.whatsapp import router as wr

    c, h, tok = shop["c"], shop["h"], shop["tok"]
    s = get_settings()
    for k, v in {"wa_app_secret": "shh", "wa_token": "t", "wa_phone_number_id": "123", "upload_dir": str(tmp_path), "house_tenants": shop["login"]}.items():
        monkeypatch.setattr(s, k, v)
    sent, opts = [], []
    monkeypatch.setattr(wa, "send_options", lambda to, body, options, button="Choose": (sent.append(body), opts.append(options)))
    monkeypatch.setattr(wa, "get_media", lambda mid: (b"\xff\xd8sheet", "image/jpeg"))
    from app import modules as mods
    mods._cache.clear()
    monkeypatch.setattr(ex, "EMERGENT_LLM_KEY", "k")
    monkeypatch.setattr(dx, "EMERGENT_LLM_KEY", "k")
    monkeypatch.setattr(ex, "get_first_page_png", lambda d, ct, n: b"png")

    async def classify(png):
        return {"type": "LEDGER_PAGE", "confidence": 93, "accepted": False, "should_warn": False, "reason": "internal cash sheet"}
    sheets = [SHEET_13_NOV]

    async def read(b64):
        return sheets[-1]
    monkeypatch.setattr(ex, "classify_document", classify)
    monkeypatch.setattr(dx, "extract_daily_sheet", read)

    def post(msg):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": uuid.uuid4().hex, "from": "91" + shop["phone"], **msg}]}}]}]}).encode()
        c.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()})

    wr._choice.clear()
    post({"type": "image", "image": {"id": "s1", "mime_type": "image/jpeg"}})
    t = sent[-1]
    assert "Daily sheet · 13 Nov 2026" in t and "Cash in: ₹19,242" in t and "Cash out: ₹38,027 (counted)" in t, t
    assert "Expenses ₹3,465" in t and "Milk 1380" in t and "Vijay Ras out: Vanilla 6" in t and "Absent: Ankull, Raj" in t, t
    assert "Advances: Ajeet 4000, Guard 200" in t and "Shiva - 11:52" in t
    save_cmd = opts[-1][0][0]
    assert save_cmd.startswith("savesheet ") and [l for _, l in opts[-1]] == ["Save", "Discard"]
    post({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": save_cmd, "title": "Save"}}})
    assert sent[-1].startswith("✅ Daily sheet for 13 Nov 2026 saved"), sent[-1]

    ask = lambda q: c.post("/api/assistant/ask", headers=h(tok), json={"q": q}).json()  # noqa: E731
    assert "Cash in: ₹19,242" in ask("sheet 13 nov")["text"] and "Cash in: ₹19,242" in ask("sheet 13/11")["text"]
    e = ask("expenses nov")["text"]
    assert "Expenses, November 2026: ₹3,465" in e and "• Milk: ₹1,380" in e and "(1 sheet(s))" in e, e
    assert "No daily sheet saved for 12 Nov" in ask("sheet 12 nov")["text"]

    # Same date again, with a wrongly written total: flagged, and saving replaces the first
    sheets.append({**SHEET_13_NOV, "cash_in": {**SHEET_13_NOV["cash_in"], "total": 19442}})
    post({"type": "image", "image": {"id": "s2", "mime_type": "image/jpeg", "caption": "sheet"}})
    assert "₹19,442 written, but the notes add up to ₹19,242 ⚠️" in sent[-1] and "already saved" in sent[-1], sent[-1]
    post({"type": "text", "text": {"body": "save"}})                                          # typed: the waiting sheet
    assert "replaces the sheet already saved" in sent[-1], sent[-1]

    # In the app: list, open, fix a number, photo
    lst = c.get("/api/daily-sheets", headers=h(tok)).json()
    assert len(lst) == 1 and lst[0]["sheet_date"] == "2026-11-13" and lst[0]["expenses"] == 3465 and lst[0]["absent"] == 6
    one = c.get(f"/api/daily-sheets/{lst[0]['id']}", headers=h(tok)).json()
    data = one["data"]
    data["expenses"][0]["amount"] = 1400
    r = c.put(f"/api/daily-sheets/{lst[0]['id']}", headers=h(tok), json={"data": data})
    assert r.status_code == 200 and r.json()["expenses"] == 3485
    assert "₹3,485" in ask("expenses")["text"] or "No daily sheets" in ask("expenses")["text"]   # "this month" depends on today's date
    assert c.get(f"/api/daily-sheets/{lst[0]['id']}/photo", headers=h(tok)).content == b"\xff\xd8sheet"
    assert c.get("/api/daily-sheets", headers=h(shop["c"].post("/api/auth/pin", json={"login_id": shop["login"], "outlet_id": shop["out"]["id"], "pin": "0000"}).json().get("token", "x"))).status_code in (401, 403)

    # A sheet with no date can't be saved from WhatsApp
    sheets.append({**SHEET_13_NOV, "date": None})
    post({"type": "image", "image": {"id": "s3", "mime_type": "image/jpeg"}})
    assert "No date on the sheet" in sent[-1] and [l for _, l in opts[-1]] == ["Discard"]


def test_photo_kept_until_restaurant_picked(monkeypatch):
    """On two restaurants: a photo sent before choosing is read as soon as one is tapped."""
    import asyncio

    from app.whatsapp import client as wa
    from app.whatsapp import router as wr

    places = [("t1", "shakewallah", "owner", "u1"), ("t2", "shakedemo", "owner", "u2")]
    monkeypatch.setattr(wr, "_who", lambda p10: places)
    sent, read = [], []
    monkeypatch.setattr(wr, "send", lambda to, r: sent.append(r))
    monkeypatch.setattr(wa, "enabled", lambda: True)
    wr._choice.clear(); wr._held.clear()

    async def fake_read(chosen, p10, media_id, mime, filename, caption=""):
        read.append((chosen[1], media_id))
        from app.assistant.answers import Reply
        return Reply("read")
    from app.whatsapp import bills
    monkeypatch.setattr(bills, "read_sent_bill", fake_read)
    monkeypatch.setattr("app.modules.enabled", lambda tid: ["bills"])
    asyncio.run(wr.handle_media("919999900001", "m1", "image/jpeg", "", ""))
    asyncio.run(wr.handle_media("919999900001", "m2", "image/jpeg", "", ""))      # a second photo: no second question
    assert len(sent) == 1 and "straight away" in sent[0].text and [c for c, _ in sent[0].options] == ["@shakewallah", "@shakedemo"]
    wr.handle("919999900001", "@shakedemo")
    assert read == [("shakedemo", "m1"), ("shakedemo", "m2")] and not wr._held
    assert wr._choice["9999900001"] == "shakedemo"


def test_account_off_on_whatsapp(shop):
    """An account switched off on WhatsApp isn't matched to the number (sign-in by phone still works)."""
    from app.db import system_session
    from app.models import User
    from app.stockplus.models import WaAlert
    from app.whatsapp import router as wr
    assert [p[1] for p in wr._who(shop["phone"])] == [shop["login"]]
    with system_session() as db:
        u = db.query(User).filter(User.phone.like(f"%{shop['phone']}")).first()
        db.add(WaAlert(tenant_id=u.tenant_id, user_id=u.id, topic="wa_off"))
    assert wr._who(shop["phone"]) == []
    with system_session() as db:
        db.query(WaAlert).filter(WaAlert.topic == "wa_off").delete()
