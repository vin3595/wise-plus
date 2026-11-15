"""wise-plus: the bill on WhatsApp for customers, with feedback buttons."""
import hashlib
import hmac
import json
import uuid

import pytest

from tests.test_api import client, h  # noqa: F401  (fixture)
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")


def test_bill_and_feedback(client, monkeypatch):
    from app.config import get_settings
    from app.custwa import service as cw
    from app.stockplus.models import WaAlert  # noqa: F401
    from app.whatsapp import client as wa

    s = get_settings()
    for k, v in {"wa_token": "t", "wa_phone_number_id": "123", "wa_app_secret": "shh", "wa_bill_template": "bill_receipt"}.items():
        monkeypatch.setattr(s, k, v)
    posts, texts = [], []
    monkeypatch.setattr(wa, "_post", lambda body: posts.append(body) or {"messages": [{"id": "x"}]})
    monkeypatch.setattr(wa, "send_text", lambda to, body: texts.append((wa.last10(to), body)))

    st = setup(client)
    tok, ck, v = st["tok"], st["ck"], st["v"]
    o = client.get("/api/tenant/outlets", headers=h(tok)).json()
    ck_out = next(x for x in o if x["id"] == ck)
    body = {k: ck_out[k] for k in ("brand_id", "name", "code")} | {"wa_bills": True, "google_review_url": "https://g.page/r/shakewallah/review"}
    r = client.patch(f"/api/tenant/outlets/{ck}", headers=h(tok), json=body)
    assert r.status_code == 200 and r.json()["wa_bills"] is True, r.text

    client.post("/api/users", headers=h(tok), json={"name": "Asha", "role": "cashier", "pin": "7411", "outlet_ids": [ck]})
    asha = client.post("/api/auth/pin", json={"login_id": st["login"], "outlet_id": ck, "pin": "7411"}).json()["token"]
    client.post("/api/shifts", headers=h(asha), json={"outlet_id": ck, "opening_cash_paise": 0})
    cust = "98" + str(uuid.uuid4().int)[:8]

    def make_bill():
        od = client.post("/api/orders", headers=h(asha), json={"outlet_id": ck, "order_type": "takeaway", "items": [{"variant_id": v, "qty": 1}]}).json()
        tot = client.post("/api/bills/preview", headers=h(asha), json={"order_id": od["id"]}).json()["total_paise"]
        b = client.post("/api/bills", headers=h(asha), json={"order_id": od["id"], "customer": {"phone": cust, "name": "Ravi"},
                                                            "payments": [{"mode": "cash", "amount_paise": tot}]})
        assert b.status_code in (200, 201), b.text
        return b.json()

    bill = make_bill()
    assert cw.send_due() >= 1
    sent = next(p for p in posts if p["to"].endswith(cust))
    t = sent["template"]
    assert t["name"] == "bill_receipt" and [x["text"] for x in t["components"][0]["parameters"]][1] == bill["invoice_no"]
    pays = [c["parameters"][0]["payload"] for c in t["components"][1:]]
    assert pays == [f"fb:{bill['id']}:5", f"fb:{bill['id']}:3", f"fb:{bill['id']}:1"]
    assert cw.send_due() == 0                                         # sent once

    def tap(payload, phone=cust, typ="button"):
        msg = {"id": uuid.uuid4().hex, "from": "91" + phone}
        msg.update({"type": "button", "button": {"payload": payload, "text": "x"}} if typ == "button" else {"type": "text", "text": {"body": payload}})
        raw = json.dumps({"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()
        client.post("/api/whatsapp/webhook", content=raw, headers={"x-hub-signature-256": "sha256=" + hmac.new(b"shh", raw, hashlib.sha256).hexdigest()})

    tap(f"fb:{bill['id']}:5")
    assert "Google review" in texts[-1][1] and "g.page" in texts[-1][1]
    tap(f"fb:{bill['id']}:5", phone="97" + str(uuid.uuid4().int)[:8])   # someone else's tap on this bill: ignored
    fb = client.get("/api/feedback", headers=h(tok)).json()
    assert fb["counts"] == {"loved": 1, "okay": 0, "bad": 0} and fb["happy_pct"] == 100

    # Not good: sorry + owner alert, then the reason is kept and the owner hears it
    bill2 = make_bill()
    tap(f"fb:{bill2['id']}:1")
    assert "What went wrong" in texts[-1][1]
    assert any(p == st["phone"] and "Not good" in b for p, b in texts)
    tap("The shake was warm", typ="text")
    assert texts[-1][1].startswith("Thank you, we've passed this on")
    assert any(p == st["phone"] and "The shake was warm" in b for p, b in texts)
    fb = client.get("/api/feedback", headers=h(tok), params={"rating": 1}).json()
    assert fb["counts"]["bad"] == 1 and fb["items"][0]["comment"] == "The shake was warm" and fb["items"][0]["phone"] == "…" + cust[-4:]
    tap("hello again", typ="text")                                     # nothing pending now: no reply to a stranger
    assert texts[-1][1].startswith("Thank you, we've passed this on")
    # Outlet switched off: no WhatsApp bill
    client.patch(f"/api/tenant/outlets/{ck}", headers=h(tok), json=body | {"wa_bills": False})
    cw.send_due()
    n = len(posts)
    make_bill()
    cw.send_due()
    assert len(posts) == n
