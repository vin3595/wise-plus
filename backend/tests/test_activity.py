import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate, signup  # noqa: E402,F401


def test_activity_log_shows_who_changed_a_price(client):
    t = signup(client, "logcafe", "9811188001")["token"]
    me = client.get("/api/tenant/me", headers=h(t)).json()
    brand_id = client.get("/api/tenant/brands", headers=h(t)).json()[0]["id"]
    cat = client.post("/api/menu/categories", json={"brand_id": brand_id, "name": "Shakes"}, headers=h(t)).json()["id"]
    item = client.post("/api/menu/items", headers=h(t), json={"brand_id": brand_id, "category_id": cat, "name": "Oreo Shake",
                       "variants": [{"name": "300 ML", "price_paise": 14900}]}).json()
    body = {"brand_id": brand_id, "category_id": cat, "name": "Oreo Shake",
            "variants": [{"id": item["variants"][0]["id"], "name": "300 ML", "price_paise": 16900}]}
    assert client.put(f"/api/menu/items/{item['id']}", headers=h(t), json=body).status_code == 200
    rows = client.get("/api/activity", headers=h(t)).json()["rows"]
    price = next(r for r in rows if r["entity"] == "item" and r["action"] == "update")
    assert price["who"] == "Owner" and price["role"] == "owner"
    assert any("₹149 → ₹169" in c for c in price["changes"]), price["changes"]
    # a change with no detailed entry of its own is still logged (by the request), secrets hidden
    n = len(rows)
    client.post("/api/push/subscribe", headers=h(t), json={"endpoint": "https://x.example/abc123", "p256dh": "B" * 87, "auth": "a" * 22})
    assert len(client.get("/api/activity", headers=h(t)).json()["rows"]) == n      # push isn't a business change
    # another business can't see it
    o = signup(client, "logcafe2", "9811188002")["token"]
    assert not any(r["entity"] == "item" for r in client.get("/api/activity", headers=h(o)).json()["rows"])
    assert me["tenant"]["login_id"] == "logcafe"
    r = client.post("/api/expenses/vendors", headers=h(t), json={"name": "Milkman", "category": "Milk", "amount_paise": 30000})
    assert r.status_code == 201, r.text
    v = next(x for x in client.get("/api/activity", headers=h(t)).json()["rows"] if x["entity"] == "expenses_vendors")
    assert v["who"] == "Owner" and v["details"]["sent"]["name"] == "Milkman"
