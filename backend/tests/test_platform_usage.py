"""wise-plus: the platform owner sees every restaurant's WhatsApp messages, credits and scans; nobody else does."""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate  # noqa: E402,F401
from tests.test_stock_extras import setup  # noqa: E402


def test_platform_usage_only_for_house_owner(client, monkeypatch):
    from sqlalchemy import text

    from app.config import get_settings
    from app.db import system_session
    from app.whatsapp import router as wr
    house = setup(client)
    other = setup(client)
    monkeypatch.setattr(get_settings(), "house_tenants", house["login"])
    assert client.get("/api/platform/usage", headers=h(other["tok"])).status_code == 404
    assert client.get("/api/billing", headers=h(other["tok"])).json()["platform_admin"] is False
    assert client.get("/api/billing", headers=h(house["tok"])).json()["platform_admin"] is True
    with system_session() as db:
        tid = db.execute(text("select id from tenants where login_id = :l"), {"l": other["login"]}).scalar()
        for kind, st in (("bill", "sent"), ("bill", "sent"), ("promo", "sent"), ("bill", "failed")):
            db.execute(text("insert into wa_outbox (id, tenant_id, kind, ref, to_phone, payload, status, tries) "
                            "values (cast(:i as uuid), :t, :k, :r, '919999999999', '{}', :s, 1)"), {"i": str(uuid.uuid4()), "r": uuid.uuid4().hex, "t": tid, "k": kind, "s": st})
    u = client.get("/api/platform/usage", headers=h(house["tok"])).json()
    me = next(r for r in u["restaurants"] if r["login_id"] == other["login"])
    assert me["sent"] == {"bill": 2, "promo": 1} and me["failed"] == 1
    assert me["meta_cost_paise"] == round(2 * 11.5 + 86.31) and me["bill_msgs_free"] == 500 * me["outlets"]
    assert next(r for r in u["restaurants"] if r["login_id"] == house["login"])["bill_msgs_free"] is None   # house: unlimited
    assert client.get("/api/platform/usage", headers=h(house["tok"]), params={"month": "bad"}).status_code == 422
    wr._choice.clear()
    r = wr.reply_rich("91" + house["phone"], "credits")
    assert "across all restaurants" in r.text and "Meta cost" in r.text
    r = wr.reply_rich("91" + other["phone"], "credits")
    assert "across all restaurants" not in r.text
