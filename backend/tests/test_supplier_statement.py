"""wise-plus: check a supplier's statement against the POS (bills saved?) and the dues ledger (payments recorded?)."""
from datetime import date

import pytest

from tests.test_api import client, h  # noqa: F401  (fixture)
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")

# Shri Tirupati Agency's ledger for Shakewallah Sardarpura, 1 Oct - 11 Nov 2026, as the reader returns it.
TIRUPATI = {
    "supplier": "SHRI TIRUPATI AGENCY", "supplier_gstin": None, "customer": "Shakewallah",
    "period_from": "2026-10-01", "period_to": "2026-11-11",
    "opening": {"amount": 49982, "customer_owes": True}, "closing": {"amount": 30108, "customer_owes": True},
    "rows": [
        {"date": "2026-10-09", "type": "bill", "ref": "E-1353", "amount": 4841, "particulars": "SALES"},
        {"date": "2026-10-12", "type": "bill", "ref": "E-1370", "amount": 3110, "particulars": "SALES"},
        {"date": "2026-10-21", "type": "bill", "ref": "E-1443", "amount": 11957, "particulars": "SALES"},
        {"date": "2026-10-22", "type": "bill", "ref": "E-1448", "amount": 5100, "particulars": "SALES"},
        {"date": "2026-10-22", "type": "payment", "ref": "1003", "amount": 49982, "particulars": "HDFC BANK",
         "bank": "HDFC BANK (50200092040100)"},
        {"date": "2026-10-24", "type": "bill", "ref": "E-1473", "amount": 5100, "particulars": "SALES"},
    ],
}


def test_clean_reads_the_ledger():
    from app.ledger.statement import clean
    st = clean(TIRUPATI)
    assert st["opening_paise"] == 4998200 and st["closing_paise"] == 3010800
    assert st["adds_up"] is True                          # 49,982 + 30,108 of bills - 49,982 paid = 30,108
    assert [r["type"] for r in st["rows"]].count("bill") == 5
    bad = clean({**TIRUPATI, "closing": {"amount": 31000, "customer_owes": True}})
    assert bad["adds_up"] is False


def test_ref_matching():
    from app.ledger.statement import _ref_match
    assert _ref_match("E-1353", "1353") and _ref_match("E-1353", "e1353") and _ref_match("2627/TS000322", "TS-322")
    assert not _ref_match("E-1353", "1354") and not _ref_match("E-12", "12") and not _ref_match(None, "1353")


def test_statement_against_pos_and_dues(client, monkeypatch):
    from app.ledger import statement as stm
    s = setup(client)
    tok, sp, milk = s["tok"], s["sp"], s["milk"]
    sup = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Shri Tirupati Agency"}).json()

    def buy(no, d, rupees):
        r = client.post("/api/inventory/purchases", headers=h(tok), json={"outlet_id": sp, "supplier_id": sup["id"], "invoice_no": no,
                        "invoice_date": d, "lines": [{"stock_item_id": milk["id"], "qty_m": 1_000_000, "unit": "ml",
                                                      "amount_paise": rupees * 100, "gst_bp": 0}]})
        assert r.status_code == 201, r.text
    buy("1353", "2026-10-09", 4841)            # by number
    buy("abc", "2026-10-13", 3110)             # no usable number: matched by amount and a close date
    buy("E-1443", "2026-10-21", 11900)         # right number, amount saved differently
    buy("X-77", "2026-10-30", 999)             # in the POS but not on their statement

    async def fake_read(data, ct, name):
        return stm.clean(TIRUPATI)
    monkeypatch.setattr(stm, "read_statement", fake_read)
    up = lambda **form: client.post("/api/ledger/statement", headers=h(tok), data=form,  # noqa: E731
                                    files={"file": ("ledger.pdf", b"%PDF-1.4 fake", "application/pdf")})

    r = up()
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["supplier"]["name"] == "Shri Tirupati Agency"            # "SHRI TIRUPATI AGENCY" found by name
    assert (d["bills_total"], d["bills_in_pos"]) == (5, 3)
    by = {b["ref"]: b for b in d["bills"]}
    assert by["E-1353"]["matched_by"] == "number" and by["E-1370"]["matched_by"] == "amount"
    assert by["E-1443"]["differs_paise"] == -5700                       # saved ₹57 less than they billed
    assert not by["E-1448"]["in_pos"] and not by["E-1473"]["in_pos"] and d["missing_paise"] == 1020000
    assert [x["invoice_no"] for x in d["only_in_pos"]] == ["X-77"]
    assert d["payments_new"] == 1 and d["payments"][0]["mode"] == "bank"
    assert d["opening"]["suggest"] == 4998200                           # no history here before 1 Oct
    assert d["closing"]["theirs"] == 3010800

    pay = d["payments"][0]
    body = {"supplier_id": sup["id"], "opening": {"date": "2026-09-30", "amount_paise": d["opening"]["suggest"]},
            "payments": [{k: pay[k] for k in ("date", "amount_paise", "kind", "mode", "ref", "bank", "particulars")}]}
    a = client.post("/api/ledger/statement/apply", headers=h(tok), json=body)
    assert a.status_code == 200 and a.json()["added"] == 2, a.text
    assert client.post("/api/ledger/statement/apply", headers=h(tok), json=body).json()["added"] == 0   # never twice

    d2 = up().json()
    assert d2["payments_new"] == 0 and d2["payments"][0]["recorded"] and d2["opening"]["suggest"] is None
    assert d2["opening"]["ours"] == 4998200
    # ours: 49,982 + 4,841 + 3,110 + 11,900 + 999 - 49,982 = 20,850 vs their 30,108 (two bills missing, one ₹57 short, one extra)
    assert d2["closing"]["ours"] == 2085000


def test_unknown_supplier_offers_choices_or_adds_it(client, monkeypatch):
    from app.ledger import statement as stm
    s = setup(client)
    tok = s["tok"]

    async def fake_read(data, ct, name):
        return stm.clean(TIRUPATI)
    monkeypatch.setattr(stm, "read_statement", fake_read)
    files = {"file": ("ledger.pdf", b"%PDF-1.4 fake", "application/pdf")}
    d = client.post("/api/ledger/statement", headers=h(tok), files=files).json()
    assert d["supplier"] is None and any(c["name"] == "Metro" for c in d["choices"])
    d = client.post("/api/ledger/statement", headers=h(tok), files=files, data={"new_supplier": "true"}).json()
    assert d["supplier"]["name"] == "SHRI TIRUPATI AGENCY" and d["bills_in_pos"] == 0 and d["payments_new"] == 1
    assert date.fromisoformat(d["bills"][0]["date"]) == date(2026, 10, 9)


def test_summary_text():
    from app.ledger.statement import summary_text
    res = {"statement": {"period_from": date(2026, 10, 1), "period_to": date(2026, 11, 11), "adds_up": True},
           "supplier": {"id": "x", "name": "Shri Tirupati Agency"}, "supplier_added": False,
           "bills_total": 5, "bills_in_pos": 3, "missing_paise": 1020000,
           "bills": [{"date": "2026-10-22", "ref": "E-1448", "amount_paise": 510000, "in_pos": False},
                     {"date": "2026-10-21", "ref": "E-1443", "amount_paise": 1195700, "in_pos": True, "differs_paise": -5700, "pos_total_paise": 1190000}],
           "only_in_pos": [], "payments": [{"date": date(2026, 10, 22), "amount_paise": 4998200, "bank": "HDFC BANK (50200092040100)", "recorded": True}],
           "closing": {"theirs": 3010800, "ours": 2085000}}
    t = summary_text(res, added=1)
    assert "*3 of 5*" in t and "E-1448 ₹5,100" in t and "theirs ₹11,957, POS ₹11,900" in t
    assert "Added to supplier dues: 1" in t and "They say you owe ₹30,108; Wise says ₹20,850" in t and "Difference ₹9,258" in t


def test_bill_scanner_spots_a_statement(client, monkeypatch):
    from app.ledger import statement as stm
    from app.purchases_ai import extraction as ex
    s = setup(client)
    tok = s["tok"]
    client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Shri Tirupati Agency"})
    monkeypatch.setattr(ex, "EMERGENT_LLM_KEY", "k")
    monkeypatch.setattr(ex, "get_first_page_png", lambda d, ct, n: b"png")

    async def classify(png):
        return {"type": "SUPPLIER_STATEMENT", "confidence": 95, "accepted": False, "should_warn": False}

    async def fake_read(data, ct, name):
        return stm.clean(TIRUPATI)
    monkeypatch.setattr(ex, "classify_document", classify)
    monkeypatch.setattr(stm, "read_statement", fake_read)
    r = client.post("/api/inventory/purchases/scan", headers=h(tok), data={"outlet_id": s["sp"]},
                    files={"file": ("photo.jpg", b"\xff\xd8x", "image/jpeg")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kind"] == "statement" and d["supplier"]["name"] == "Shri Tirupati Agency" and d["bills_total"] == 5
    # a typed Tally PDF is recognised without asking the AI
    assert stm.looks_like_statement(b"x", "image/jpeg", "Tirupati ledger.jpg")
    assert not stm.looks_like_statement(b"x", "image/jpeg", "bill.jpg")


from tests.test_ledger_assistant import shop  # noqa: E402,F401  (fixture)


def test_whatsapp_statement(shop, monkeypatch, tmp_path):
    import hashlib
    import hmac
    import json
    import uuid as _u

    from app.config import get_settings
    from app.ledger import statement as stm
    from app.purchases_ai import extraction as ex
    from app.whatsapp import client as wa
    from app.whatsapp import router as wr

    c, hh, tok = shop["c"], shop["h"], shop["tok"]
    st = get_settings()
    for k, v in {"wa_app_secret": "shh", "wa_token": "t", "wa_phone_number_id": "123", "upload_dir": str(tmp_path)}.items():
        monkeypatch.setattr(st, k, v)
    sent = []
    monkeypatch.setattr(wa, "send_options", lambda to, body, options, button="Choose": sent.append(body))
    monkeypatch.setattr(wa, "send_text", lambda to, body: sent.append(body))
    monkeypatch.setattr(wa, "get_media", lambda mid: (b"%PDF-1.4 typed", "application/pdf"))
    monkeypatch.setattr(ex, "EMERGENT_LLM_KEY", "k")
    monkeypatch.setattr(stm, "_pdf_text", lambda d: "Shakewallah Ledger Account 1-Oct-26 to 11-Nov-26 Opening Balance 49,982")

    async def classify(png):
        raise AssertionError("a typed ledger shouldn't need the classifier")

    async def fake_read(data, ct, name):
        return stm.clean(TIRUPATI)
    monkeypatch.setattr(ex, "classify_document", classify)
    monkeypatch.setattr(stm, "read_statement", fake_read)

    def post(msg):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": _u.uuid4().hex, "from": "91" + shop["phone"], **msg}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        assert c.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200

    wr._choice.clear()
    post({"type": "document", "document": {"id": "m9", "mime_type": "application/pdf", "filename": "ACCOUNT_STATEMENT-15.pdf"}})
    t = sent[-1]
    assert "SHRI TIRUPATI AGENCY" in t and "New supplier" in t and "*0 of 5*" in t and "E-1473 ₹5,100" in t, t
    assert "Added to supplier dues: 2" in t           # the HDFC payment and the opening balance
    assert "They say you owe ₹30,108; Wise says ₹0" in t
    sups = c.get("/api/ledger/suppliers?all=true", headers=hh(tok)).json()["suppliers"]
    tir = next(x for x in sups if x["name"] == "SHRI TIRUPATI AGENCY")
    assert tir["balance_paise"] == 0 and tir["paid_paise"] == 4998200 and tir["opening_paise"] == 4998200
    post({"type": "document", "document": {"id": "m10", "mime_type": "application/pdf", "filename": "ACCOUNT_STATEMENT-15.pdf"}})
    assert "Added to supplier dues" not in sent[-1] and "New supplier" not in sent[-1]      # sending it twice adds nothing


def test_all_purchase_bills_filter_and_delete(client):
    s = setup(client)
    tok, sp, ck, milk = s["tok"], s["sp"], s["ck"], s["milk"]
    a = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Amul Dairy"}).json()
    b = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Kanha Foods"}).json()

    def buy(out, sup, no, d, rupees):
        r = client.post("/api/inventory/purchases", headers=h(tok), json={"outlet_id": out, "supplier_id": sup, "invoice_no": no, "invoice_date": d,
                        "lines": [{"stock_item_id": milk["id"], "qty_m": 1_000_000, "unit": "ml", "amount_paise": rupees * 100, "gst_bp": 0}]})
        assert r.status_code == 201, r.text
        return r.json()
    base = client.get("/api/inventory/purchases/all", headers=h(tok)).json()       # setup() already bought something
    p1 = buy(sp, a["id"], "AM-1", "2026-11-01", 1000)
    p2 = buy(ck, a["id"], "AM-2", "2026-11-05", 2500)
    p3 = buy(ck, b["id"], "KF-9", "2026-11-10", 400)
    g = lambda **q: client.get("/api/inventory/purchases/all", headers=h(tok), params=q).json()  # noqa: E731
    allp = g()
    assert allp["count"] == base["count"] + 3 and allp["total_paise"] == base["total_paise"] + 390000
    assert all(x["outlet"] for x in allp["purchases"])
    assert g(supplier_id=a["id"])["count"] == 2 and g(q="kanha")["count"] == 1 and g(q="am-2")["count"] == 1
    assert g(d1="2026-11-04", d2="2026-11-30", q="am")["count"] >= 1 and g(min_total=100000, supplier_id=a["id"])["count"] == 2
    assert g(outlet_id=sp, supplier_id=a["id"])["count"] == 1
    assert g(q="milk")["count"] >= 3                                          # item names too
    r = client.post("/api/inventory/purchases/delete", headers=h(tok), json={"ids": [p1["id"], p3["id"], p3["id"]]}).json()
    assert r["deleted"] == 2
    left = g()
    assert left["count"] == base["count"] + 1 and left["total_paise"] == base["total_paise"] + 250000
    assert g(status="cancelled")["count"] == 2 and g(status="all")["count"] == base["count"] + 3
    assert client.post("/api/inventory/purchases/delete", headers=h(tok), json={"ids": [p1["id"]]}).json()["deleted"] == 0
    dues = client.get(f"/api/ledger/suppliers/{b['id']}", headers=h(tok)).json()
    assert dues["balance_paise"] == 0                                        # a deleted bill isn't owed
