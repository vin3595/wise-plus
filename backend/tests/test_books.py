"""wise-plus: the full Tally export (every voucher balances, nothing counted twice), firm filter, CA pack."""
import io
import re
import zipfile
from datetime import timedelta

import pytest

from tests.test_api import client, h, migrate  # noqa: F401  (fixtures)
from tests.test_banking import _bill
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")


def test_full_books(client):
    from app.core.kds import business_date
    s = setup(client)
    tok, ck, v, milk = s["tok"], s["ck"], s["v"], s["milk"]
    today = business_date()
    client.patch(f"/api/tenant/outlets/{ck}", headers=h(tok), json={**{k: x for k, x in next(o for o in client.get("/api/tenant/outlets", headers=h(tok)).json() if o["id"] == ck).items()
                                                                       if k in ("brand_id", "name", "code")}, "gstin": "08ABCDE1234F1Z5"})
    client.post("/api/users", headers=h(tok), json={"name": "Asha", "role": "cashier", "pin": "7414", "outlet_ids": [ck]})
    asha = client.post("/api/auth/pin", json={"login_id": s["login"], "outlet_id": ck, "pin": "7414"}).json()["token"]
    sh = client.post("/api/shifts", headers=h(asha), json={"outlet_id": ck, "opening_cash_paise": 0}).json()
    b = _bill(client, asha, ck, v, "upi")
    _bill(client, asha, ck, v, "cash")
    ln = client.get(f"/api/bills/{b['id']}/returnable", headers=h(tok)).json()["lines"][0]
    cn = client.post(f"/api/bills/{b['id']}/credit-notes", headers=h(tok), json={"lines": [{"bill_line_id": ln["id"], "qty": 1}],
                                                                                "reason": "Wrong item", "refund_mode": "cash"}).json()
    # cash drawer: an expense and a bank deposit
    cm = client.post(f"/api/shifts/{sh['id']}/cash", headers=h(asha), json={"kind": "out", "amount_paise": 15000, "reason": "milk",
                                                                           "purpose": "expense", "category": "Milk"})
    assert cm.status_code == 200, cm.text
    sup = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Saras Dairy", "gstin": "08AAAAA0000A1Z5"}).json()
    p = client.post("/api/inventory/purchases", headers=h(tok), json={"outlet_id": ck, "supplier_id": sup["id"], "invoice_no": "SD-1",
                    "invoice_date": str(today), "lines": [{"stock_item_id": milk["id"], "qty_m": 10_000, "unit": "l", "amount_paise": 60000, "gst_bp": 500}]}).json()
    client.post(f"/api/inventory/purchases/{p['id']}/return", headers=h(tok), json={"lines": [{"stock_item_id": milk["id"], "qty_m": 1_000}], "reason": "Sour"})
    d = lambda n: (today + timedelta(days=n)).strftime("%d/%m/%Y")  # noqa: E731
    csv = "\n".join(["Statement Account No: XXXX4455", "Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance",
                     f"{d(0)},NEFT-SARAS DAIRY,a1,500.00,,9500.00", f"{d(0)},SALARY NOV,a2,8000.00,,1500.00",
                     f"{d(0)},CHRG SMS ALERT,a3,10.00,,1490.00", f"{d(0)},UPI-SOMEONE-x@ybl,a4,,25.00,1515.00"])
    r = client.post("/api/banks/statements", headers=h(tok), files={"file": ("acct.csv", csv.encode(), "text/csv")}).json()
    saras = next(t for t in client.get("/api/banks/txns?show=all", headers=h(tok)).json() if t["ref"] == "a1")
    client.post(f"/api/banks/txns/{saras['id']}", headers=h(tok), json={"kind": "supplier", "supplier_id": sup["id"]})
    q = {"date_from": str(today - timedelta(days=1)), "date_to": str(today + timedelta(days=1))}

    def xml(kind, **extra):
        res = client.get("/api/accounts/tally", headers=h(tok), params={"kind": kind, **q, **extra})
        return res.status_code, res.text
    code, all_xml = xml("all")
    assert code == 200, all_xml
    kinds = re.findall(r'VCHTYPE="([^"]+)"', all_xml)
    for k in ("Sales", "Purchase", "Credit Note", "Debit Note", "Payment"):
        assert k in kinds, (k, kinds)
    assert kinds.count("Payment") == 4              # Saras by bank (once, from dues), cash expense, salary, charges
    assert all_xml.count("Paid Saras Dairy") == 1 and all_xml.count("NEFT-SARAS") == 1     # one voucher, not two
    assert "Refund CN/" not in all_xml and cn["number"] in all_xml          # the drawer's refund line isn't a second voucher
    assert "Milk" in all_xml and "Salary" in all_xml and "Bank Charges" in all_xml
    assert "UPI-SOMEONE" not in all_xml                                     # still to check: left out
    acct_name = r["account"]["name"]
    assert f"<LEDGERNAME>{acct_name}</LEDGERNAME>" in all_xml               # the bank account is its own ledger
    code, m = xml("masters")
    assert code == 200 and acct_name in m and "Drawings" in m and "<PARENT>Indirect Expenses</PARENT>" in m and "Milk" in m
    assert xml("credit_notes")[0] == 200 and xml("bank")[0] == 200
    # one firm (by GSTIN): its outlet's sales, no bank lines (the account isn't set to that outlet)
    code, firm = xml("all", gstin="08abcde1234f1z5")
    assert code == 200 and "SALARY" not in firm and "Sales" in firm
    assert xml("all", gstin="08ZZZZZ0000Z1Z1")[0] == 404

    pk = client.get("/api/accounts/pack", headers=h(tok), params=q)
    assert pk.status_code == 200, pk.text
    z = zipfile.ZipFile(io.BytesIO(pk.content))
    names = set(z.namelist())
    assert {"README.txt", "1-tally-ledgers.xml", "2-tally-vouchers.xml", "accounts.xlsx"} <= names
    readme = z.read("README.txt").decode()
    assert "1 bank line(s) still 'To check'" in readme and "Credit Note 1" in readme
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(z.read("accounts.xlsx")))
    assert {"Credit notes", "Supplier payments & notes", "Bank lines", "GST summary"} <= set(wb.sheetnames)


from tests.test_ledger_assistant import shop  # noqa: E402,F401  (fixture)


def test_whatsapp_files_and_menu(shop, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.whatsapp import router as wr
    p = shop["phone"]
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    wr._choice.clear()
    r = wr.reply_rich("91" + p, "hi")
    assert r.text.startswith("What do you need") and len(r.options) == 10 and ("tally", "CA pack (Tally)") in r.options
    r = wr.reply_rich("91" + p, "tally")
    assert r.text.startswith("📒 Accounts for") and r.document and r.document[1].endswith(".zip") and "/api/public/files/" in r.document[0]
    r = wr.reply_rich("91" + p, "tally oct 2026")
    assert "October 2026" in r.text
    r = wr.reply_rich("91" + p, "backup")
    assert r.text.startswith("📦") and r.document[1].endswith(".zip")
    import io
    import zipfile
    from app.exports import _dir
    tok = r.document[0].split("/api/public/files/")[1].split("/")[0]
    z = zipfile.ZipFile(io.open(f"{_dir()}/{tok}/{r.document[1]}", "rb"))
    assert "data.json.gz" in z.namelist()
