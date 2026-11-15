"""wise-plus: water / waiter / bill from the table QR, alerts on the POS."""
import uuid

import pytest

from tests.test_api import client, h  # noqa: F401  (fixture)
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")


def test_table_service(client):
    st = setup(client)
    tok, ck, v = st["tok"], st["ck"], st["v"]
    o = next(x for x in client.get("/api/tenant/outlets", headers=h(tok)).json() if x["id"] == ck)
    key = o["qr_key"]
    body = {k: o[k] for k in ("brand_id", "name", "code")} | {"table_labels": ["T1", "T2"], "order_types": ["dine_in", "takeaway"],
                                                               "upi_vpa": "shakewallah@okaxis"}
    assert client.get(f"/api/public/table/{key}", params={"t": "T1"}).status_code == 404     # off until switched on
    r = client.patch(f"/api/tenant/outlets/{ck}", headers=h(tok), json=body | {"table_service": True})
    assert r.status_code == 200 and r.json()["table_service"], r.text
    assert client.get(f"/api/public/table/{key}", params={"t": "T9"}).status_code == 404     # not a table here

    page = client.get(f"/api/public/table/{key}", params={"t": "t1"}).json()
    assert page["table"] == "T1" and page["tab"] is None and page["upi"] is True and page["ordering"] is False
    assert client.post(f"/api/public/table/{key}/requests", json={"table": "T1", "kind": "bill"}).status_code == 409   # nothing to bill

    w = client.post(f"/api/public/table/{key}/requests", json={"table": "T1", "kind": "water", "device": "d1"})
    assert w.status_code == 201 and w.json()["kind"] == "water"
    again = client.post(f"/api/public/table/{key}/requests", json={"table": "T1", "kind": "water"}).json()
    assert again["again"] and again["id"] == w.json()["id"] and again["times_asked"] == 1        # bumped at most once a minute
    client.post(f"/api/public/table/{key}/requests", json={"table": "T1", "kind": "waiter"})

    # The table orders (staff takes it), then asks for the bill
    client.post("/api/users", headers=h(tok), json={"name": "Raju", "role": "captain", "pin": "7511", "outlet_ids": [ck]})
    od = client.post("/api/orders", headers=h(tok), json={"outlet_id": ck, "order_type": "dine_in", "table_label": "T1",
                                                         "items": [{"variant_id": v, "qty": 2}]})
    assert od.status_code in (200, 201), od.text
    page = client.get(f"/api/public/table/{key}", params={"t": "T1"}).json()
    assert page["tab"]["items"][0]["qty"] == 2 and page["tab"]["total_paise"] > 0
    b = client.post(f"/api/public/table/{key}/requests", json={"table": "T1", "kind": "bill", "note": "paid_upi"})
    assert b.status_code == 201
    page = client.get(f"/api/public/table/{key}", params={"t": "T1"}).json()
    assert page["bill"]["paid"] is False and page["bill"]["upi_link"].startswith("upi://pay?pa=shakewallah%40okaxis")
    assert page["bill"]["total_paise"] == page["tab"]["total_paise"] and page["requests"]["water"]["status"] == "open"
    assert client.post(f"/api/public/table/{key}/requests", json={"table": "T1", "kind": "water"}).json()["again"]
    # A 4th open request for the same table isn't taken
    # POS: all three, oldest first; the bill shows its amount and "paid by UPI"
    reqs = client.get("/api/table-requests", headers=h(tok), params={"outlet_id": ck}).json()
    assert [r["kind"] for r in reqs["open"]] == ["water", "waiter", "bill"]
    bill_req = reqs["open"][2]
    assert bill_req["total_paise"] == page["tab"]["total_paise"] and bill_req["note"] == "paid_upi"
    d = client.post(f"/api/table-requests/{reqs['open'][0]['id']}/done", headers=h(tok)).json()
    assert d["status"] == "done"
    reqs = client.get("/api/table-requests", headers=h(tok), params={"outlet_id": ck}).json()
    assert len(reqs["open"]) == 2 and reqs["done"][0]["kind"] == "water" and reqs["done"][0]["done_by"]
    # The cashier makes the bill: the customer's page shows it
    client.post("/api/shifts", headers=h(tok), json={"outlet_id": ck, "opening_cash_paise": 0})
    tot = client.post("/api/bills/preview", headers=h(tok), json={"order_id": od.json()["id"]}).json()["total_paise"]
    bb = client.post("/api/bills", headers=h(tok), json={"order_id": od.json()["id"], "customer": {"declined": True},
                                                        "payments": [{"mode": "upi", "amount_paise": tot}]})
    assert bb.status_code in (200, 201), bb.text
    page = client.get(f"/api/public/table/{key}", params={"t": "T1"}).json()
    assert page["bill"]["paid"] is True and "/b/" in page["bill"]["bill_url"] and page["tab"] is None
    # Another table works on its own
    assert client.post(f"/api/public/table/{key}/requests", json={"table": "T2", "kind": "waiter"}).status_code == 201
    # Nightly line
    from app.core.kds import business_date
    from app.db import tenant_session
    from app.tableqr.router import day_line
    from app.models import Tenant
    from app.db import system_session
    with system_session() as db:
        tid = db.query(Tenant.id).filter(Tenant.login_id == st["login"]).scalar()
    with tenant_session(tid) as db:
        line = day_line(db, uuid.UUID(ck), business_date())
    assert line and "Table calls: 4" in line
