"""wise-plus: petty expenses — amount + quantity into stock and costs, petty cash wallet, regular vendors, slips (app and
WhatsApp), typed 'kharcha' on WhatsApp with Undo, Tally vouchers."""
import io
import os
from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate  # noqa: E402,F401
from tests.test_stock_extras import setup  # noqa: E402


def _stock(client, tok, outlet):
    return {r["name"]: r for r in client.get("/api/inventory/stock", headers=h(tok), params={"outlet_id": outlet}).json()["items"]}


def test_petty_wallet_qty_stock_vendors_tally(client):
    s = setup(client)
    tok, ck, milk = s["tok"], s["ck"], s["milk"]
    w = client.get("/api/expenses/setup", headers=h(tok), params={"outlet_id": ck}).json()["wallet"]
    assert w["in_hand_paise"] == 0
    assert client.post("/api/expenses/topups", headers=h(tok), json={"outlet_id": ck, "amount_paise": 500000, "source": "owner"}).json()["in_hand_paise"] == 500000

    before = _stock(client, tok, ck)["Milk"]["qty_m"]
    r = client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": [
        {"amount_paise": 33000, "category": "milk", "qty_m": 6000, "unit": "L", "stock_item_id": milk["id"]},
        {"amount_paise": 5000, "category": "Transport", "what": "auto to market"}]})
    assert r.status_code == 201, r.text
    r = r.json()
    assert r["saved"] == 2 and r["wallet"]["in_hand_paise"] == 500000 - 38000 and r["wallet"]["since_topup_no_slip"] == 2
    assert r["expenses"][0]["category"] == "Milk"
    st = _stock(client, tok, ck)["Milk"]
    assert st["qty_m"] == before + 6_000_000                                   # 6 L into stock (ml, thousandths)
    # next time: ₹165 of milk reads as 3 L at the last rate, and Milk goes to the Milk stock item
    links = client.get("/api/expenses/setup", headers=h(tok), params={"outlet_id": ck}).json()["links"]
    assert links["Milk"]["stock_item_id"] == milk["id"] and links["Milk"]["rate"]["paise_per_unit"] == 5500
    bad = client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": [
        {"amount_paise": 100, "category": "Milk", "qty_m": 1000, "unit": "box", "stock_item_id": milk["id"]}]})
    assert bad.status_code == 422                                              # milk isn't counted in boxes

    # counter cash needs an open shift; then it's a cash pay-out in that shift
    c = client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": [{"amount_paise": 12000, "category": "Ice", "paid_from": "counter"}]})
    assert c.status_code == 409
    sh = client.post("/api/shifts", headers=h(tok), json={"outlet_id": ck, "opening_cash_paise": 100000}).json()
    c = client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": [{"amount_paise": 12000, "category": "Ice", "paid_from": "counter"}]})
    assert c.status_code == 201
    shift = client.get("/api/shifts/current", headers=h(tok), params={"outlet_id": ck}).json()
    assert shift["cash_out_paise"] == 12000

    # cancel: stock and petty cash come back
    lst = client.get("/api/expenses", headers=h(tok), params={"outlet_id": ck}).json()
    m = next(e for e in lst["expenses"] if e["category"] == "Milk")
    assert m["qty_m"] == 6000 and m["stock_item"] == "Milk" and m["paid_label"] == "Petty cash"
    assert client.post(f"/api/expenses/{m['id']}/cancel", headers=h(tok), json={"reason": "typed twice"}).status_code == 200
    assert _stock(client, tok, ck)["Milk"]["qty_m"] == before
    w = client.get("/api/expenses/setup", headers=h(tok), params={"outlet_id": ck}).json()["wallet"]
    assert w["in_hand_paise"] == 500000 - 5000

    # a regular: one tap, or a different amount at the same rate
    v = client.post("/api/expenses/vendors", headers=h(tok), json={"name": "Milkman", "category": "Milk", "amount_paise": 33000, "qty_m": 6000,
                                                                   "unit": "L", "stock_item_id": milk["id"]}).json()
    u = client.post(f"/api/expenses/vendors/{v['id']}/use", headers=h(tok), json={"outlet_id": ck}).json()
    assert u["expense"]["qty_m"] == 6000 and u["expense"]["amount_paise"] == 33000 and u["expense"]["source"] == "vendor"
    u = client.post(f"/api/expenses/vendors/{v['id']}/use", headers=h(tok), json={"outlet_id": ck, "amount_paise": 16500}).json()
    assert u["expense"]["qty_m"] == 3000
    lst = client.get("/api/expenses", headers=h(tok), params={"outlet_id": ck}).json()
    milkcat = next(c for c in lst["by_category"] if c["category"] == "Milk")
    assert milkcat["amount_paise"] == 49500 and milkcat["qty"]["L"] == 9000
    assert lst["total_paise"] == 49500 + 5000 + 12000

    today = date.today()
    x = client.get("/api/accounts/tally", headers=h(tok), params={"kind": "cash", "date_from": str(today - timedelta(days=2)),
                                                                  "date_to": str(today + timedelta(days=1))}).text
    assert "Petty expense: Milkman (6 L)" in x and "Petty expense: Milkman (3 L)" in x and "Petty Cash" in x and "auto to market" in x
    assert x.count("Petty expense") == 3                                       # not the counter one (that's a cash voucher) nor the cancelled one


def test_petty_slip_scan(client, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.expenses import reader
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    s = setup(client)
    tok, ck, milk = s["tok"], s["ck"], s["milk"]
    client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": [
        {"amount_paise": 33000, "category": "Milk", "qty_m": 6000, "unit": "L", "stock_item_id": milk["id"]}]})

    async def fake(data, ct, name, today):
        return {"slips": [{"spent_on": today.isoformat(), "amount_paise": 22000, "what": "doodh", "qty_m": None, "unit": None, "category": "Milk"},
                          {"spent_on": today.isoformat(), "amount_paise": 12000, "what": "2 silli", "qty_m": 2000, "unit": "block", "category": "Ice"}],
                "has_gstin": False, "note": None}
    monkeypatch.setattr(reader, "read", fake)
    img = b"\xff\xd8\xff" + b"slip-photo" * 50
    r = client.post("/api/expenses/scan", headers=h(tok), data={"outlet_id": ck}, files={"file": ("s.jpg", io.BytesIO(img), "image/jpeg")}).json()
    d = r["drafts"]
    assert r["already"] is None and len(d) == 2
    assert d[0]["qty_m"] == 4000 and d[0]["unit"] == "L" and d[0]["qty_guessed"] and d[0]["stock_item_id"] == milk["id"]   # ₹220 at ₹55/L
    items = [{k: x[k] for k in ("amount_paise", "category", "what", "qty_m", "unit", "stock_item_id", "spent_on")} for x in d]
    items[1]["stock_item_id"] = None
    sv = client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": items, "slip_token": r["slip_token"], "slip_hash": r["slip_hash"]})
    assert sv.status_code == 201, sv.text
    e = sv.json()["expenses"][0]
    assert e["has_slip"] and client.get(f"/api/expenses/{e['id']}/slip", headers=h(tok)).content == img
    again = client.post("/api/expenses/scan", headers=h(tok), data={"outlet_id": ck}, files={"file": ("s.jpg", io.BytesIO(img), "image/jpeg")}).json()
    assert again["already"]["amount_paise"] == 22000                           # same photo twice: flagged
    bad = client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": items, "slip_token": "../../etc/passwd"})
    assert bad.status_code == 422


def test_petty_on_whatsapp(client, monkeypatch, tmp_path):
    import asyncio

    from app.config import get_settings
    from app.expenses import reader
    from app.whatsapp import bills
    from app.whatsapp import router as wr
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    s = setup(client)
    tok, ck, milk, p = s["tok"], s["ck"], s["milk"], s["phone"]
    wr._choice.clear()
    client.post("/api/expenses", headers=h(tok), json={"outlet_id": ck, "items": [
        {"amount_paise": 33000, "category": "Milk", "qty_m": 6000, "unit": "L", "stock_item_id": milk["id"]}]})
    before = _stock(client, tok, ck)["Milk"]["qty_m"]
    outs = client.get("/api/tenant/outlets", headers=h(tok)).json()
    name = next(o["name"] for o in outs if o["id"] == ck).split()[0].lower()

    r = wr.reply_rich("91" + p, f"kharcha {name} doodh 330 6 ltr")
    assert r.text.startswith("✅") and "Milk" in r.text and "6 L" in r.text and "stock +6 L" in r.text, r.text
    assert r.options and r.options[0][1] == "Undo"
    assert _stock(client, tok, ck)["Milk"]["qty_m"] == before + 6_000_000
    r2 = wr.reply_rich("91" + p, f"kharcha {name} milk 165")
    assert "3 L" in r2.text and "same rate" in r2.text, r2.text
    r3 = wr.reply_rich("91" + p, f"kharcha {name} ice 120 counter")
    assert "No shift is open" in r3.text
    u = wr.reply_rich("91" + p, r.options[0][0])
    assert "Undone" in u.text and _stock(client, tok, ck)["Milk"]["qty_m"] == before + 3_000_000
    plain = wr.reply_rich("91" + p, "Brownee 25 pieces 650 rs")              # no 'kharcha': still an expense
    assert plain.text.startswith("✅ ₹650 · Brownee · 25 pc"), plain.text
    assert wr.reply_rich("91" + p, "hi").text.startswith("✅") is False
    bare = wr.reply_rich("91" + p, "kharcha")                                # bare 'kharcha' is still the expenses report
    assert not bare.text.startswith("✅") and all(o[1] != "Undo" for o in bare.options)

    v = client.post("/api/expenses/vendors", headers=h(tok), json={"name": "Raju milkman", "category": "Milk", "amount_paise": 33000, "qty_m": 6000,
                                                                   "unit": "L", "stock_item_id": milk["id"]}).json()
    rv = wr.reply_rich("91" + p, f"kharcha {name} raju")
    assert rv.text.startswith("✅ Raju milkman") and "6 L" in rv.text, rv.text

    async def fake(data, ct, n, today):
        return {"slips": [{"spent_on": today.isoformat(), "amount_paise": 5000, "what": "auto", "qty_m": None, "unit": None, "category": "Transport"}],
                "has_gstin": False, "note": None}
    monkeypatch.setattr(reader, "read", fake)
    chosen = wr._place("91" + p, "hi")[0]
    out = asyncio.run(bills._read_slips(chosen, p, b"\xff\xd8photo-of-slip", "image/jpeg", f"kharcha {name}"))
    assert "Saved 1 slip" in out.text and "₹50" in out.text, out.text
    out2 = asyncio.run(bills._read_slips(chosen, p, b"\xff\xd8photo-of-slip", "image/jpeg", f"kharcha {name}"))
    assert "already saved" in out2.text
    assert v["name"] == "Raju milkman"
