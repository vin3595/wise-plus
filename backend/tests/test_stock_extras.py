"""wise-plus: stock app extras - transfers, spot checks, storage places, daily-order items,
menu profit, trends and the nightly WhatsApp summary."""
import uuid
from datetime import timedelta

import pytest

from tests.test_api import client, h, signup  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("client")


def setup(client):
    login = "stk" + uuid.uuid4().hex[:6]
    phone = "96" + str(uuid.uuid4().int)[:8]
    tok = signup(client, login, phone)["token"]
    brand = client.get("/api/tenant/brands", headers=h(tok)).json()[0]["id"]
    ck = client.post("/api/tenant/outlets", headers=h(tok), json={"brand_id": brand, "name": "Central Kitchen", "code": "CK"}).json()["id"]
    sp = client.post("/api/tenant/outlets", headers=h(tok), json={"brand_id": brand, "name": "Sardarpura", "code": "SP"}).json()["id"]
    cat = client.post("/api/menu/categories", json={"brand_id": brand, "name": "Shakes"}, headers=h(tok)).json()["id"]
    tg = client.get("/api/menu/tax-groups", headers=h(tok)).json()[0]["id"]
    item = client.post("/api/menu/items", headers=h(tok), json={"brand_id": brand, "category_id": cat, "name": "Kitkat Shake",
                       "tax_group_id": tg, "variants": [{"name": "300 ML", "price_paise": 18900}]}).json()
    v = item["variants"][0]["id"]
    mk = lambda body: client.post("/api/inventory/items", headers=h(tok), json=body).json()  # noqa: E731
    kitkat = mk({"name": "Kitkat", "unit": "pc", "locations": ["Counter", "Fridge in use", " fridge in use ", "Carton store", ""]})
    bread = mk({"name": "Bread", "unit": "pc", "order_daily": True})
    milk = mk({"name": "Milk", "unit": "ml"})
    sup = client.post("/api/inventory/suppliers", headers=h(tok), json={"name": "Metro"}).json()
    r = client.post("/api/inventory/purchases", headers=h(tok), json={"outlet_id": ck, "supplier_id": sup["id"], "invoice_no": "M1",
        "invoice_date": "2026-11-10", "lines": [
            {"stock_item_id": kitkat["id"], "qty_m": 200_000, "unit": "pc", "amount_paise": 400000, "gst_bp": 0},      # ₹20 each
            {"stock_item_id": milk["id"], "qty_m": 20_000_000, "unit": "ml", "amount_paise": 120000, "gst_bp": 0}]})  # ₹60/l
    assert r.status_code == 201, r.text
    client.put(f"/api/inventory/recipes/variant/{v}", headers=h(tok), json={"lines": [
        {"stock_item_id": kitkat["id"], "qty_m": 2000}, {"stock_item_id": milk["id"], "qty_m": 250_000}]})
    return {"tok": tok, "login": login, "phone": phone, "ck": ck, "sp": sp, "v": v, "kitkat": kitkat, "bread": bread, "milk": milk}


def test_transfers_and_storage_places(client):
    s = setup(client)
    tok, ck, sp, kk, milk = s["tok"], s["ck"], s["sp"], s["kitkat"], s["milk"]
    assert kk["locations"] == ["Counter", "Fridge in use", "Carton store"]
    t = client.post("/api/inventory/transfers", headers=h(tok), json={"from_outlet_id": ck, "to_outlet_id": sp, "note": "morning run",
                    "lines": [{"stock_item_id": kk["id"], "qty_m": 48_000}, {"stock_item_id": milk["id"], "qty_m": 5_000_000}]})
    assert t.status_code == 201, t.text
    t = t.json()
    assert t["status"] == "sent" and t["value_paise"] == 48 * 2000 + 30000
    stock = lambda o: {r["name"]: r["qty_m"] for r in client.get("/api/inventory/stock", headers=h(tok), params={"outlet_id": o}).json()["items"]}  # noqa: E731
    assert stock(ck)["Kitkat"] == 152_000 and stock(sp)["Kitkat"] == 0          # on the way
    inbox = client.get("/api/inventory/transfers", headers=h(tok), params={"outlet_id": sp}).json()
    assert inbox[0]["id"] == t["id"] and inbox[0]["incoming"] and inbox[0]["from"] == "Central Kitchen"
    # 2 Kitkats short on arrival
    r = client.post(f"/api/inventory/transfers/{t['id']}/receive", headers=h(tok), json={"lines": [{"stock_item_id": kk["id"], "received_m": 46_000}]})
    assert r.status_code == 200, r.text
    assert r.json()["short_paise"] == 4000 and stock(sp)["Kitkat"] == 46_000 and stock(sp)["Milk"] == 5_000_000
    assert client.post(f"/api/inventory/transfers/{t['id']}/receive", headers=h(tok), json={}).status_code == 409
    cost = {r["name"]: r for r in client.get("/api/inventory/stock", headers=h(tok), params={"outlet_id": sp}).json()["items"]}
    ckc = {r["name"]: r for r in client.get("/api/inventory/stock", headers=h(tok), params={"outlet_id": ck}).json()["items"]}
    assert cost["Kitkat"]["cost_k"] == ckc["Kitkat"]["cost_k"] > 0                 # arrives at the kitchen's cost
    # A cancelled transfer goes back to the kitchen
    t2 = client.post("/api/inventory/transfers", headers=h(tok), json={"from_outlet_id": ck, "to_outlet_id": sp,
                     "lines": [{"stock_item_id": kk["id"], "qty_m": 10_000}]}).json()
    assert client.post(f"/api/inventory/transfers/{t2['id']}/cancel", headers=h(tok)).json()["status"] == "cancelled"
    assert stock(ck)["Kitkat"] == 152_000
    assert client.post("/api/inventory/transfers", headers=h(tok), json={"from_outlet_id": ck, "to_outlet_id": ck,
                       "lines": [{"stock_item_id": kk["id"], "qty_m": 1000}]}).status_code == 422
    # Counted place by place; the places must add up
    r = client.post("/api/inventory/counts", headers=h(tok), json={"outlet_id": sp, "lines": [
        {"stock_item_id": kk["id"], "counted_m": 45_000, "parts": {"Counter": 5000, "Fridge in use": 10_000, "Carton store": 30_000}}]})
    assert r.status_code == 200 and r.json()["lines"][0]["diff_m"] == -1000
    assert client.post("/api/inventory/counts", headers=h(tok), json={"outlet_id": sp, "lines": [
        {"stock_item_id": kk["id"], "counted_m": 45_000, "parts": {"Counter": 5000}}]}).status_code == 422


def test_spot_checks_and_daily_items(client):
    from app.core.kds import business_date
    from app.db import tenant_session
    from app.models import StockMove
    from app.stockplus.daily import tomorrow_need

    s = setup(client)
    tok, ck, kk = s["tok"], s["ck"], s["kitkat"]
    pick = client.get("/api/inventory/spot-check", headers=h(tok), params={"outlet_id": ck, "n": 2}).json()["items"]
    assert len(pick) == 2 and all("qty_m" not in x for x in pick)
    r = client.post("/api/inventory/counts", headers=h(tok), json={"outlet_id": ck, "kind": "spot", "lines": [
        {"stock_item_id": kk["id"], "counted_m": 195_000}]})
    assert r.status_code == 200
    spots = client.get("/api/inventory/spot-checks", headers=h(tok), params={"outlet_id": ck}).json()
    assert spots[0]["off"] == 1 and spots[0]["short_paise"] == 10000 and spots[0]["lines"][0]["diff_m"] == -5000
    # A spot check isn't the closing count
    day = client.get(f"/api/day-close/{ck}", headers=h(tok)).json()
    assert any(c["key"] == "stock" for c in day["checks"]), day["checks"]

    # Bread is bought every day: 40, 44, 36 used on the last three same weekdays -> 40 + 10% = 44, less 4 in stock
    today = business_date()
    from app.db import system_session
    from app.models import Tenant
    with system_session() as db:
        tid = str(db.query(Tenant.id).filter(Tenant.login_id == s["login"]).scalar())
    with tenant_session(tid) as db:
        from app.models import StockItem
        bread = db.get(StockItem, uuid.UUID(s["bread"]["id"]))
        for w, used in ((1, 40), (2, 44), (3, 36)):
            db.add(StockMove(tenant_id=uuid.UUID(tid), outlet_id=uuid.UUID(ck), stock_item_id=bread.id, business_date=today + timedelta(days=1) - timedelta(weeks=w),
                             kind="sale", qty_m=-used * 1000, cost_k=0, value_paise=0))
        db.flush()
        need = tomorrow_need(db, uuid.UUID(ck), bread, 4000, today)
    assert need["usual_m"] == 40_000 and need["order_m"] == 40_000 and need["basis"].startswith("usual"), need
    re = client.get("/api/inventory/reorder", headers=h(tok), params={"outlet_id": ck}).json()
    line = next(x for g in re for x in g["lines"] if x["name"] == "Bread")
    assert line["order_m"] == 44_000 and line["daily"]["usual_m"] == 40_000


def test_menu_profit_trends_and_nightly(client, monkeypatch):
    from app.config import get_settings
    from app.stockplus import nightly
    from app.whatsapp import client as wa

    s = setup(client)
    tok, ck, v = s["tok"], s["ck"], s["v"]
    client.post("/api/users", headers=h(tok), json={"name": "Asha", "role": "cashier", "pin": "7311", "outlet_ids": [ck]})
    asha = client.post("/api/auth/pin", json={"login_id": s["login"], "outlet_id": ck, "pin": "7311"}).json()["token"]
    client.post("/api/shifts", headers=h(asha), json={"outlet_id": ck, "opening_cash_paise": 0})
    o = client.post("/api/orders", headers=h(asha), json={"outlet_id": ck, "order_type": "takeaway", "items": [{"variant_id": v, "qty": 3}]}).json()
    total = client.post("/api/bills/preview", headers=h(asha), json={"order_id": o["id"]}).json()["total_paise"]
    assert client.post("/api/bills", headers=h(asha), json={"order_id": o["id"], "customer": {"declined": True},
                       "payments": [{"mode": "cash", "amount_paise": total}]}).status_code in (200, 201)
    mp = client.get("/api/inventory/menu-profit", headers=h(tok), params={"outlet_id": ck}).json()
    row = mp["items"][0]
    # 2 Kitkats ₹40 + 250 ml milk ₹15 = ₹55 a shake
    assert row["qty"] == 3 and row["unit_cost_paise"] == 5500 and row["cost_paise"] == 16500
    assert row["profit_paise"] == row["sales_paise"] - 16500 and mp["total"]["profit_paise"] == row["profit_paise"]
    assert client.get("/api/inventory/menu-profit", headers=h(asha), params={"outlet_id": ck}).status_code == 403
    tr = client.get("/api/inventory/trends", headers=h(tok), params={"outlet_id": ck, "days": 7}).json()
    today = tr["days"][-1]
    assert today["sales_paise"] == row["sales_paise"] and today["cost_paise"] == 16500 and today["food_cost_bp"]
    assert tr["movers"][0]["name"] == "Kitkat"

    # Nightly summary: switched on in the app, sent once
    assert client.put("/api/whatsapp-alerts/me", headers=h(tok), json={"nightly": True}).json()["nightly"] is True
    st = get_settings()
    monkeypatch.setattr(st, "wa_nightly_template", "nightly_summary")
    sent = []
    monkeypatch.setattr(wa, "send_options", lambda to, body, options, button="Choose": sent.append(("text", to, body)))
    monkeypatch.setattr(wa, "send_template", lambda to, name, params=None, lang="en": sent.append(("tpl", to, name, params)))
    from app.core.kds import business_date
    from app.db import system_session
    from app.stockplus.models import NightlyRun
    day = business_date() + timedelta(days=400)          # a day no other test uses
    with system_session() as db:
        db.query(NightlyRun).filter(NightlyRun.day == day).delete()
    n = nightly.run(day)
    mine = [x for x in sent if x[1].endswith(s["phone"])]
    assert n >= 1 and mine and mine[0][0] == "tpl" and mine[0][2] == "nightly_summary"   # hasn't written in 24h
    assert nightly.run(day) == 0                                                          # once a night
    nightly.seen(s["phone"])
    sent.clear()
    nightly.run(business_date(), force=True)
    body = next(x for x in sent if x[1].endswith(s["phone"]))
    assert body[0] == "text" and "*Central Kitchen*: Sales" in body[2] and "food cost" in body[2], body
    ask = client.post("/api/assistant/ask", headers=h(tok), json={"q": "summary"}).json()
    assert "🌙" in ask["text"] and "*Sardarpura*: Sales ₹0 (0 bills)" in ask["text"]
    assert client.put("/api/whatsapp-alerts/me", headers=h(tok), json={"nightly": False}).json()["nightly"] is False
