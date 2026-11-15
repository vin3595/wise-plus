"""wise-plus: credit notes on bills (GST shares, numbering, limits, cash back from the drawer), purchase returns,
supplier credit notes."""
import pytest

from tests.test_api import client, h, migrate  # noqa: F401  (fixtures)
from tests.test_banking import _bill
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")


def test_cn_number():
    from app.creditnotes.router import cn_number
    assert cn_number("SHKA", 2026, 7) == "CN/SHKA/26270007" and len(cn_number("SHKA", 2026, 9999)) == 16


def test_credit_note_on_a_bill(client):
    s = setup(client)
    tok, ck, v = s["tok"], s["ck"], s["v"]
    client.post("/api/users", headers=h(tok), json={"name": "Asha", "role": "cashier", "pin": "7413", "outlet_ids": [ck]})
    asha = client.post("/api/auth/pin", json={"login_id": s["login"], "outlet_id": ck, "pin": "7413"}).json()["token"]
    sh = client.post("/api/shifts", headers=h(asha), json={"outlet_id": ck, "opening_cash_paise": 0}).json()
    od = client.post("/api/orders", headers=h(asha), json={"outlet_id": ck, "order_type": "takeaway", "items": [{"variant_id": v, "qty": 3}]}).json()
    tot = client.post("/api/bills/preview", headers=h(asha), json={"order_id": od["id"]}).json()["total_paise"]
    bill = client.post("/api/bills", headers=h(asha), json={"order_id": od["id"], "customer": {"declined": True},
                                                           "payments": [{"mode": "cash", "amount_paise": tot}]}).json()
    r = client.get(f"/api/bills/{bill['id']}/returnable", headers=h(asha)).json()
    line = r["lines"][0]
    assert line["qty"] == 3 and line["left"] == 3

    body = {"lines": [{"bill_line_id": line["id"], "qty": 1}], "reason": "Shake spilt", "refund_mode": "cash"}
    assert client.post(f"/api/bills/{bill['id']}/credit-notes", headers=h(asha), json=body).status_code == 403    # cashier needs a PIN
    client.post("/api/users", headers=h(tok), json={"name": "Mohan", "role": "manager", "pin": "8899", "outlet_ids": [ck], "phone": "9" + "1" * 9})
    cn = client.post(f"/api/bills/{bill['id']}/credit-notes", headers=h(asha), json=body | {"manager_pin": "8899"})
    assert cn.status_code == 201, cn.text
    cn = cn.json()
    assert cn["number"].startswith("CN/CK/") and cn["number"].endswith("0001")
    bl = bill["lines"][0]
    assert cn["taxable_paise"] == round(bl["taxable_paise"] / 3) and cn["cgst_paise"] == round(bl["cgst_paise"] / 3)
    assert cn["total_paise"] % 100 == 0 and abs(cn["total_paise"] - tot / 3) <= 100
    # the cash came out of the drawer
    cur = client.get(f"/api/shifts/current?outlet_id={ck}", headers=h(asha)).json()
    assert cur["id"] == sh["id"] and cur["cash_out_paise"] == cn["total_paise"], cur

    # can't give back more than was sold
    too = client.post(f"/api/bills/{bill['id']}/credit-notes", headers=h(tok), json=body | {"lines": [{"bill_line_id": line["id"], "qty": 3}]})
    assert too.status_code == 422 and "Only 2" in too.text
    cn2 = client.post(f"/api/bills/{bill['id']}/credit-notes", headers=h(tok), json=body | {"refund_mode": "upi", "lines": [{"bill_line_id": line["id"], "qty": 2}]}).json()
    assert cn2["number"].endswith("0002")
    assert cn["taxable_paise"] + cn2["taxable_paise"] == bl["taxable_paise"]          # shares add up to the line exactly
    assert client.get(f"/api/bills/{bill['id']}/returnable", headers=h(tok)).json()["lines"][0]["left"] == 0
    # cancelling frees the quantity again
    assert client.post(f"/api/credit-notes/{cn2['id']}/cancel", headers=h(tok), json={"reason": "Entered by mistake"}).json()["status"] == "cancelled"
    assert client.get(f"/api/bills/{bill['id']}/returnable", headers=h(tok)).json()["lines"][0]["left"] == 2
    lst = client.get("/api/credit-notes", headers=h(tok), params={"outlet_id": ck}).json()
    assert [x["number"][-4:] for x in lst] == ["0002", "0001"] and lst[0]["invoice_no"] == bill["invoice_no"]


def test_purchase_return_and_supplier_credit_note(client):
    s = setup(client)
    tok, ck, milk, kk = s["tok"], s["ck"], s["milk"], s["kitkat"]
    sup = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Saras Dairy"}).json()
    p = client.post("/api/inventory/purchases", headers=h(tok), json={"outlet_id": ck, "supplier_id": sup["id"], "invoice_no": "SD-9",
                    "invoice_date": "2026-11-12", "lines": [{"stock_item_id": milk["id"], "qty_m": 10_000, "unit": "l", "amount_paise": 60000, "gst_bp": 500}]})
    assert p.status_code == 201, p.text
    p = p.json()
    lvl = lambda: next(x for x in client.get(f"/api/inventory/stock?outlet_id={ck}", headers=h(tok)).json()["items"] if x["id"] == milk["id"])["qty_m"]  # noqa: E731
    before = lvl()
    r = client.post(f"/api/inventory/purchases/{p['id']}/return", headers=h(tok), json={"lines": [{"stock_item_id": milk["id"], "qty_m": 2_000}], "reason": "Sour"})
    assert r.status_code == 201, r.text
    assert r.json()["amount_paise"] == 12000 and r.json()["tax_paise"] == 600
    assert before - lvl() == 2_000_000                                     # 2 l = 2,000,000 milli-ml
    st = client.get(f"/api/ledger/suppliers/{sup['id']}", headers=h(tok)).json()
    assert st["balance_paise"] == 63000 - 12600 and st["entries"][-1]["kind"] == "debit_note"
    over = client.post(f"/api/inventory/purchases/{p['id']}/return", headers=h(tok), json={"lines": [{"stock_item_id": milk["id"], "qty_m": 9_000}], "reason": "x"})
    assert over.status_code == 422
    assert client.post(f"/api/inventory/purchases/{p['id']}/return", headers=h(tok),
                       json={"lines": [{"stock_item_id": kk["id"], "qty_m": 1000}], "reason": "nope"}).status_code == 422
    # the supplier sends a credit note (rate difference)
    e = client.post("/api/ledger/entries", headers=h(tok), json={"supplier_id": sup["id"], "kind": "credit_note", "entry_date": "2026-11-14",
                                                                "amount_paise": 5000, "reference": "CN-44"})
    assert e.status_code == 201 and e.json()["balance_paise"] == 63000 - 12600 - 5000
