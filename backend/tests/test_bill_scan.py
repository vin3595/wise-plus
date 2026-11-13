"""wise-plus bill scanning: matching rules (no database) and the scan endpoint with the AI reader faked."""
import os
import uuid

import pytest

from app.purchases_ai.match import build_draft, name_score, nearest_gst_bp, pick_unit


ITEMS = [
    {"id": "milk", "name": "Milk", "unit": "ml", "purchase_units": [{"name": "crate", "factor": 12000}]},
    {"id": "sugar", "name": "Sugar", "unit": "g", "purchase_units": []},
    {"id": "oreo", "name": "Oreo Biscuits", "unit": "pc", "purchase_units": [{"name": "box of 24", "factor": 24}]},
    {"id": "vanilla", "name": "Vanilla Ice Cream", "unit": "ml", "purchase_units": [{"name": "tub", "factor": 5000}]},
]


def test_names_match_the_way_bills_print_them():
    assert name_score("SUGAR M-30 50KG BAG", "Sugar") >= 0.9
    assert name_score("Oreo Biscuit 120g", "Oreo Biscuits") >= 0.55
    assert name_score("VADILAL VANILLA 5 LTR TUB", "Vanilla Tub") >= 0.55
    # Too unsure: left for the person to pick (and remembered after that)
    assert name_score("VADILAL VANILLA 5 LTR TUB", "Vanilla Ice Cream") < 0.55
    assert name_score("Paper cups 250ml", "Sugar") < 0.55


def test_units_and_gst():
    assert pick_unit(ITEMS[1], "SUGAR 50 KG") == "kg"
    assert pick_unit(ITEMS[0], "Amul Taaza 1 LTR") == "l"
    assert pick_unit(ITEMS[2], "Oreo box of 24") == "box of 24"
    assert pick_unit(ITEMS[3], "Vanilla") == "tub"          # bills count packs
    assert nearest_gst_bp(18) == 1800 and nearest_gst_bp(17.9) == 1800 and nearest_gst_bp(None) == 0 and nearest_gst_bp(4.9) == 500


def test_draft_from_a_read_bill():
    bill = {"vendor": "Kanha Food Products", "gstin": "08ABCDE1234F1Z5", "invoice_number": "KFP/1182", "bill_date": "2026-11-12",
            "total": 1180.0, "taxable_amount": 1000.0, "cgst_amount": 90.0, "sgst_amount": 90.0,
            "items": [{"name": "SUGAR 10 KG", "quantity": 10, "unit_price": 50, "total": 500},
                      {"name": "OREO BOX OF 24", "quantity": 2, "unit_price": 250, "total": 500},
                      {"name": "Straws", "quantity": 1, "unit_price": 0, "total": 0}]}
    sups = [{"id": "s1", "name": "Kanha Foods", "gstin": "08ABCDE1234F1Z5"}]
    d = build_draft(bill, ITEMS, sups, {})
    assert d["supplier"] == {"id": "s1", "name": "Kanha Foods", "how": "gstin"}
    assert d["invoice_no"] == "KFP/1182" and d["total_paise"] == 118000
    a, b, c = d["lines"]
    assert (a["stock_item_id"], a["unit"], a["qty_m"], a["amount_paise"], a["gst_bp"]) == ("sugar", "kg", 10000, 50000, 1800)
    assert (b["stock_item_id"], b["unit"], b["qty_m"]) == ("oreo", "box of 24", 2000)
    assert c["stock_item_id"] is None                       # no stock item called straws: the person picks one
    # Remembered from this supplier's last bill: the person had saved "Straws" as Oreo (made-up, but it must win)
    d2 = build_draft(bill, ITEMS, sups, {"straws": "oreo"})
    assert d2["lines"][2]["stock_item_id"] == "oreo" and d2["lines"][2]["match"] == 1.0


# ---------------------------------------------------------------- endpoint, against Postgres like test_api.py

api = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="set WISE_RUN_API_TESTS=1 and start Postgres")


@api
def test_scan_endpoint_drafts_and_remembers(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.purchases_ai import extraction
    from tests.test_api import _v1_setup, h

    c = TestClient(app)
    tok, _asha, out, _v1, _v2 = _v1_setup(c, "billscan" + uuid.uuid4().hex[:6])
    milk = c.post("/api/inventory/items", headers=h(tok), json={"name": "Milk", "unit": "ml", "purchase_units": [{"name": "crate", "factor": 12000}]})
    assert milk.status_code in (200, 201), milk.text
    sup = c.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Saras Dairy", "gstin": "08AAAAA0000A1Z5"}).json()

    # Off until the server has a key
    monkeypatch.setattr(extraction, "EMERGENT_LLM_KEY", "")
    assert c.get("/api/inventory/purchases/scan/status", headers=h(tok)).json() == {"enabled": False}
    r = c.post("/api/inventory/purchases/scan", headers=h(tok), data={"outlet_id": out["id"]}, files={"file": ("b.jpg", b"x", "image/jpeg")})
    assert r.status_code == 503

    monkeypatch.setattr(extraction, "EMERGENT_LLM_KEY", "test-key")
    monkeypatch.setattr(extraction, "get_first_page_png", lambda data, ct, name: b"png")

    async def classify(png):
        return {"type": "INVOICE", "confidence": 95, "reason": "", "accepted": True, "should_warn": False}

    async def read(data, ct, name):
        return ({"vendor": "SARAS DAIRY JODHPUR", "gstin": "08AAAAA0000A1Z5", "invoice_number": "SD-77", "bill_date": "2026-11-13",
                 "total": 630.0, "items": [{"name": "SARAS GOLD MILK 1 LTR", "quantity": 12, "unit_price": 50, "total": 600, "tax_rate": 5}]},
                "data:image/png;base64,AAAA")

    monkeypatch.setattr(extraction, "classify_document", classify)
    monkeypatch.setattr(extraction, "extract_bill_from_file", read)
    assert c.get("/api/inventory/purchases/scan/status", headers=h(tok)).json() == {"enabled": True}
    r = c.post("/api/inventory/purchases/scan", headers=h(tok), data={"outlet_id": out["id"]}, files={"file": ("b.jpg", b"jpegbytes", "image/jpeg")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["supplier"]["id"] == sup["id"] and d["invoice_no"] == "SD-77" and "duplicate" not in d
    ln = d["lines"][0]
    assert ln["stock_item_id"] == milk.json()["id"] and ln["unit"] == "l" and ln["qty_m"] == 12000 and ln["gst_bp"] == 500

    # Save it through the normal purchase API, as the screen does; the line name is kept.
    body = {"outlet_id": out["id"], "supplier_id": sup["id"], "invoice_no": "SD-77", "invoice_date": "2026-11-13",
            "lines": [{"stock_item_id": ln["stock_item_id"], "qty_m": ln["qty_m"], "unit": ln["unit"], "amount_paise": ln["amount_paise"],
                       "gst_bp": ln["gst_bp"], "bill_name": ln["bill_name"]}]}
    p = c.post("/api/inventory/purchases", headers=h(tok), json=body)
    assert p.status_code == 201, p.text
    assert p.json()["lines"][0]["bill_name"] == "SARAS GOLD MILK 1 LTR" and p.json()["total_paise"] == 63000

    # Same bill again: flagged as already entered
    d = c.post("/api/inventory/purchases/scan", headers=h(tok), data={"outlet_id": out["id"]}, files={"file": ("b.jpg", b"jpegbytes", "image/jpeg")}).json()
    assert d["duplicate"]["total_paise"] == 63000

    # Not a bill
    async def selfie(png):
        return {"type": "RANDOM_PHOTO", "confidence": 97, "reason": "a photo of a cup", "accepted": False, "should_warn": False}
    monkeypatch.setattr(extraction, "classify_document", selfie)
    r = c.post("/api/inventory/purchases/scan", headers=h(tok), data={"outlet_id": out["id"]}, files={"file": ("b.jpg", b"jpegbytes", "image/jpeg")})
    assert r.status_code == 422 and "doesn't look like" in r.json()["detail"]

    # Cashiers can't scan purchases
    asha = _asha
    r = c.post("/api/inventory/purchases/scan", headers=h(asha), data={"outlet_id": out["id"]}, files={"file": ("b.jpg", b"jpegbytes", "image/jpeg")})
    assert r.status_code == 403
