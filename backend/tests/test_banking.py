"""wise-plus: bank accounts, statement import (dedupe), matching, learning, money that didn't arrive."""
import uuid
from datetime import date, timedelta

import pytest

from tests.test_api import client, h, migrate  # noqa: F401  (fixtures)
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")


def test_parse_table_formats():
    from app.banking.parse import rows_from_table, to_date, to_paise
    assert to_date("09/10/26") == date(2026, 10, 9) and to_date("9-Oct-2026") == date(2026, 10, 9) and to_date("2026-10-09") == date(2026, 10, 9)
    assert to_paise("1,23,456.50") == 12345650 and to_paise("500.00 Dr") == -50000 and to_paise("") is None
    hdfc = [["HDFC BANK Ltd", None], ["Account No : XXXXXXXX0100"], [],
            ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
            ["01/11/26", "UPI-RAVI-ravi@okaxis-123", "0000412", "01/11/26", "", "250.00", "10,250.00"],
            ["02/11/26", "NEFT DR-SHRI TIRUPATI AGENCY", "N123", "02/11/26", "5,100.00", "", "5,150.00"],
            ["*** End of statement ***"]]
    rows = rows_from_table(hdfc)
    assert [r["amount_paise"] for r in rows] == [25000, -510000] and rows[1]["balance_paise"] == 515000
    # one amount column with Dr / Cr
    sbi = [["Txn Date", "Description", "Amount", "Dr/Cr", "Balance"], ["3 Nov 2026", "ATM WDL", "2000", "DR", "100"]]
    assert rows_from_table(sbi)[0]["amount_paise"] == -200000


def _bill(client, tok_cashier, outlet, v, mode):
    od = client.post("/api/orders", headers=h(tok_cashier), json={"outlet_id": outlet, "order_type": "takeaway", "items": [{"variant_id": v, "qty": 1}]}).json()
    tot = client.post("/api/bills/preview", headers=h(tok_cashier), json={"order_id": od["id"]}).json()["total_paise"]
    b = client.post("/api/bills", headers=h(tok_cashier), json={"order_id": od["id"], "customer": {"declined": True},
                                                               "payments": [{"mode": mode, "amount_paise": tot}]})
    assert b.status_code in (200, 201), b.text
    return b.json()


def test_statement_matching_and_learning(client, monkeypatch):
    from app.banking import router as br
    from app.core.kds import business_date
    s = setup(client)
    tok, ck, v = s["tok"], s["ck"], s["v"]
    client.post("/api/users", headers=h(tok), json={"name": "Asha", "role": "cashier", "pin": "7412", "outlet_ids": [ck]})
    asha = client.post("/api/auth/pin", json={"login_id": s["login"], "outlet_id": ck, "pin": "7412"}).json()["token"]
    client.post("/api/shifts", headers=h(asha), json={"outlet_id": ck, "opening_cash_paise": 0})
    card = _bill(client, asha, ck, v, "card")["total_paise"] + _bill(client, asha, ck, v, "card")["total_paise"]
    upi = _bill(client, asha, ck, v, "upi")["total_paise"]
    today = business_date()

    tir = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Shri Tirupati Agency"}).json()
    kanha = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Kanha Food Products"}).json()
    # a payment already entered in supplier dues (mode bank)
    client.post("/api/ledger/entries", headers=h(tok), json={"supplier_id": kanha["id"], "kind": "payment", "entry_date": str(today),
                                                             "amount_paise": 777700, "mode": "bank"})
    d = lambda n: (today + timedelta(days=n)).strftime("%d/%m/%Y")  # noqa: E731
    fee_card = card - int(card * 0.02)
    csv = "\n".join([
        "HDFC BANK  Statement of account  Account No : 50200092040100",
        "Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance",
        f"{d(0)},UPI-CUSTOMER-cust@okaxis-1,r1,,{upi / 100:.2f},{(100000 + upi) / 100:.2f}",
        f"{d(1)},POS SETTLEMENT PINE LABS,r2,,{fee_card / 100:.2f},0",
        f"{d(1)},NEFT DR-KANHA FOOD PRODUCTS,r3,7777.00,,0",
        f"{d(1)},IMPS-SHRI TIRUPATI AGENCY,r4,5100.00,,0",
        f"{d(2)},UPI-JVVNL JODHPUR-jvvnl@sbi-99,r5,1890.00,,0",
        f"{d(2)},SALARY NOV RAMESH,r6,12000.00,,0",
        f"{d(2)},CHRG SMS ALERT,r7,17.70,,0",
        f"{d(3)},UPI-MAHESH KIRANA-mk@ybl-55,r8,640.00,,0",
        f"{d(3)},ZOMATO LTD PAYOUT,r9,,3456.00,0",
    ])
    up = lambda data: client.post("/api/banks/statements", headers=h(tok), files={"file": ("hdfc.csv", data.encode(), "text/csv")})  # noqa: E731
    r = up(csv)
    assert r.status_code == 200, r.text
    sm = r.json()
    assert sm["account_added"] and sm["account"]["name"] == "HDFC ···0100" and sm["account"]["last4"] == "0100"
    assert sm["lines_new"] == 9
    lines = {t["narration"]: t for t in client.get("/api/banks/txns?show=all", headers=h(tok)).json()}
    assert lines["UPI-CUSTOMER-cust@okaxis-1"]["note"].startswith("UPI sales of")
    assert lines["POS SETTLEMENT PINE LABS"]["note"].startswith("CARD sales of")          # 2% short: the bank's fee
    assert lines["NEFT DR-KANHA FOOD PRODUCTS"]["kind"] == "supplier" and lines["NEFT DR-KANHA FOOD PRODUCTS"]["status"] == "matched"
    assert lines["IMPS-SHRI TIRUPATI AGENCY"]["status"] == "suggested" and lines["IMPS-SHRI TIRUPATI AGENCY"]["supplier_id"] == tir["id"]
    assert lines["UPI-JVVNL JODHPUR-jvvnl@sbi-99"]["category"] == "Electricity"
    assert lines["SALARY NOV RAMESH"]["kind"] == "salary" and lines["CHRG SMS ALERT"]["kind"] == "charges"
    assert lines["ZOMATO LTD PAYOUT"]["kind"] == "aggregator"
    assert lines["UPI-MAHESH KIRANA-mk@ybl-55"]["status"] == "unmatched"
    assert sm["to_check"] == 2

    # The same statement again adds nothing
    assert up(csv).json()["lines_new"] == 0

    # Confirm the Tirupati payment: it goes into supplier dues, paid from this bank
    tp = lines["IMPS-SHRI TIRUPATI AGENCY"]
    r = client.post(f"/api/banks/txns/{tp['id']}", headers=h(tok), json={"kind": "supplier", "supplier_id": tir["id"]})
    assert r.status_code == 200 and r.json()["txn"]["status"] == "matched"
    st = client.get(f"/api/ledger/suppliers/{tir['id']}", headers=h(tok)).json()
    assert st["balance_paise"] == -510000 and st["entries"][0]["mode"] == "bank"

    # Teach it: Mahesh Kirana is an expense (Groceries); next month's line sorts itself
    mk = lines["UPI-MAHESH KIRANA-mk@ybl-55"]
    assert client.post(f"/api/banks/txns/{mk['id']}", headers=h(tok), json={"kind": "expense"}).status_code == 422   # needs a head
    client.post(f"/api/banks/txns/{mk['id']}", headers=h(tok), json={"kind": "expense", "category": "groceries"})
    nxt = csv.splitlines()[:2] + [f"{d(20)},UPI-MAHESH KIRANA-mk@ybl-91,r20,410.00,,0"]
    sm2 = up("\n".join(nxt)).json()
    assert sm2["lines_new"] == 1 and sm2["to_check"] == 0 and sm2["groups"][0]["label"] == "Expense"
    got = next(t for t in client.get("/api/banks/txns?show=all", headers=h(tok)).json() if t["ref"] == "r20")
    assert got["category"] == "Groceries" and got["matched_by"] == "rule"

    acc = client.get("/api/banks", headers=h(tok)).json()
    assert acc["accounts"][0]["to_check"] == 0 and acc["accounts"][0]["balance_paise"] is not None

    # Card money that never came: a second account, card sales of a day with nothing in the bank
    a2 = client.post("/api/banks", headers=h(tok), json={"name": "Axis Current", "bank": "Axis Bank", "last4": "7781", "outlet_ids": [ck]}).json()
    later = "\n".join(["Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance",
                       f"{d(8)},INTEREST PAID,x,,1.00,1.00"])
    r = client.post("/api/banks/statements", headers=h(tok), data={"account_id": a2["id"]},
                    files={"file": ("axis.csv", later.encode(), "text/csv")}).json()
    # the statement starts 8 days later, so nothing is expected yet in this account (sales are before it)
    assert r["account"]["name"] == "Axis Current" and r["missing"] == []
    early = "\n".join(["Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance",
                       f"{d(0)},OPENING TRANSFER FROM 0100,tr1,,10.00,11.00", f"{d(9)},INTEREST PAID,y,,1.00,12.00"])
    r = client.post("/api/banks/statements", headers=h(tok), data={"account_id": a2["id"]},
                    files={"file": ("axis2.csv", early.encode(), "text/csv")}).json()
    modes = {m["mode"] for m in r["missing"]}
    assert modes == {"card", "upi"}, r["missing"]                 # those went to HDFC, not Axis
    t = next(t for t in client.get(f"/api/banks/txns?show=all&account_id={a2['id']}", headers=h(tok)).json() if t["ref"] == "tr1")
    assert t["kind"] == "transfer"                                  # "0100" is our HDFC account


def test_whatsapp_bank(monkeypatch, tmp_path):
    from app.banking import service as svc
    s = {"account": {"name": "HDFC ···0100", "balance_paise": 1234500}, "account_added": False, "period_from": date(2026, 11, 1),
         "period_to": date(2026, 11, 15), "lines_new": 40, "money_in_paise": 9000000, "money_out_paise": 7000000, "matched": 36,
         "to_check": 4, "groups": [{"label": "UPI / card sales", "in_paise": 8000000, "out_paise": 0}],
         "missing": [{"mode": "card", "date": date(2026, 11, 3), "sales_paise": 450000, "short_paise": 450000}], "closing_matches": True}
    t = svc.summary_text(s)
    assert "Sorted 36 of 40 · 4 to check" in t and "CARD sales of 3 Nov ₹4,500: ₹4,500 not in the bank" in t and "Balance ₹12,345" in t


from tests.test_ledger_assistant import shop  # noqa: E402,F401  (fixture)


def test_bank_on_whatsapp(shop, monkeypatch, tmp_path):
    import hashlib
    import hmac
    import json

    from app.config import get_settings
    from app.whatsapp import client as wa
    from app.whatsapp import router as wr

    c, hh, tok, p = shop["c"], shop["h"], shop["tok"], shop["phone"]
    st = get_settings()
    for k, v in {"wa_app_secret": "shh", "wa_token": "t", "wa_phone_number_id": "123", "upload_dir": str(tmp_path)}.items():
        monkeypatch.setattr(st, k, v)
    sent, opts = [], []
    monkeypatch.setattr(wa, "send_options", lambda to, body, options, button="Choose": (sent.append(body), opts.append(options)))
    monkeypatch.setattr(wa, "send_text", lambda to, body: (sent.append(body), opts.append([])))
    csv = ("Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
           "01/11/2026,NEFT DR-KANHA FOOD PRODUCTS,k1,2285.72,,7714.28\n"
           "02/11/2026,UPI-SHARMA TENT HOUSE-st@ybl,k2,3000.00,,4714.28\n"
           "03/11/2026,UPI-SHARMA TENT HOUSE-st@ybl,k3,1000.00,,3714.28\n").encode()
    monkeypatch.setattr(wa, "get_media", lambda mid: (csv, "text/csv"))

    def post(msg):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"id": uuid.uuid4().hex, "from": "91" + p, **msg}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        assert c.post("/api/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200

    wr._choice.clear()
    post({"type": "document", "document": {"id": "b1", "mime_type": "text/csv", "filename": "Acct_Statement_XX0100.csv"}})
    assert "Bank ···0100" in sent[-1] and "Sorted 0 of 3 · 3 to check" in sent[-1], sent[-1]
    assert opts[-1][0][0] == "bank check"
    r = wr.reply_rich("91" + p, "bank check")
    assert "*To check* (3)" in r.text and "KANHA" in r.text and "Looks like a payment to Kanha" in r.text
    r = wr.reply_rich("91" + p, r.options[0][0])                       # "Yes, Kanha Food Products"
    assert "Paid to Kanha Food Products, added to supplier dues" in r.text and "SHARMA TENT HOUSE" in r.text
    tap = next(cmd for cmd, label in r.options if label == "Expense")
    r = wr.reply_rich("91" + p, tap)
    assert "What was it for?" in r.text
    r = wr.reply_rich("91" + p, f"{tap}:Tent hire")
    assert "Expense: Tent Hire" in r.text and "Also sorted 1 similar" in r.text and "Nothing left to check" in r.text
    dues = c.get(f"/api/ledger/suppliers/{shop['kanha']['id']}", headers=hh(tok)).json()
    assert dues["balance_paise"] == 0                                 # the bill ₹2,285.72 is now paid
    r = wr.reply_rich("91" + p, "bank")
    assert "Banks" in r.text and "₹3,714.28" in r.text and r.options == []
