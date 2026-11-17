"""wise-plus: an order served before it's billed stays on Orders until it's billed (or cancelled)."""
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate  # noqa: E402,F401
from tests.test_stock_extras import setup  # noqa: E402


def test_served_order_stays_until_billed(client):
    s = setup(client)
    tok, ck, v = s["tok"], s["ck"], s["v"]
    od = client.post("/api/orders", headers=h(tok), json={"outlet_id": ck, "order_type": "takeaway", "items": [{"variant_id": v, "qty": 1}]}).json()
    for k in od["kots"]:
        for st in ("preparing", "ready", "served"):
            assert client.post(f"/api/kds/kots/{k['id']}/status", headers=h(tok), json={"status": st}).status_code == 200
    ids = lambda: [o["id"] for o in client.get("/api/orders", headers=h(tok), params={"outlet_id": ck}).json()]  # noqa: E731
    got = client.get(f"/api/orders/{od['id']}", headers=h(tok)).json()
    assert got["status"] == "completed"
    assert od["id"] in ids()                                      # served, not paid: still there to bill
    client.post("/api/shifts", headers=h(tok), json={"outlet_id": ck, "opening_cash_paise": 0})
    tot = client.post("/api/bills/preview", headers=h(tok), json={"order_id": od["id"]}).json()["total_paise"]
    b = client.post("/api/bills", headers=h(tok), json={"order_id": od["id"], "customer": {"declined": True},
                                                        "payments": [{"mode": "cash", "amount_paise": tot}]})
    assert b.status_code in (200, 201), b.text
    assert od["id"] not in ids()
