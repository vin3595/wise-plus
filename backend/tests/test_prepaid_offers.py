"""wise-plus: pay-first WhatsApp offers (Razorpay link → QR → counter hands it over, bill saved as UPI) and offer ROI."""
import hashlib
import hmac
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate  # noqa: E402,F401
from tests.test_stock_extras import setup  # noqa: E402


def _wa(monkeypatch):
    from app.config import get_settings
    from app.whatsapp import client as wa
    monkeypatch.setattr(get_settings(), "public_base_url", "https://plus.example.com")
    for k, val in {"wa_token": "t", "wa_phone_number_id": "123"}.items():
        monkeypatch.setattr(get_settings(), k, val)
    images, texts = [], []
    monkeypatch.setattr(wa, "send_image", lambda to, link, caption=None: images.append((to, link, caption)))
    monkeypatch.setattr(wa, "send_text", lambda to, body: texts.append(body))
    return images, texts


def _offer(client, tok, v, **kw):
    k = "PAY" + uuid.uuid4().hex[:6].upper()
    r = client.post("/api/offers", headers=h(tok), json={"name": "Cold Coffee at ₹29", "keyword": k, "variant_id": v, "price_paise": 2900,
                                                         "per_phone": 2, "prepaid": True} | kw)
    assert r.status_code == 201, r.text
    return r.json()


def test_prepaid_practice_link_to_bill_and_roi(client, monkeypatch):
    from app.whatsapp import router as wr
    images, texts = _wa(monkeypatch)
    s = setup(client)
    tok, ck, v, me = s["tok"], s["ck"], s["v"], s["phone"]
    assert client.post("/api/offers", headers=h(tok), json={"name": "Free", "keyword": "FREE" + uuid.uuid4().hex[:5], "variant_id": v,
                                                           "price_paise": 0, "prepaid": True}).status_code == 422
    o = _offer(client, tok, v)
    assert client.get("/api/offers", headers=h(tok)).json()["online_payments"] == "test"

    wr.handle("91" + "98" + str(uuid.uuid4().int)[:8], o["keyword"])         # practice links: only the business's own people
    assert "isn't available" in texts[-1]
    wr.handle("91" + me, o["keyword"], "Vineet")
    msg = texts[-1]
    assert "Pay here: https://plus.example.com/api/public/offer-pay/" in msg and "₹29" in msg and "practice" in msg
    url = msg.split("Pay here: ")[1].split()[0]
    path = url.replace("https://plus.example.com", "")
    wr.handle("91" + me, o["keyword"])
    assert path in texts[-1]                                                  # the same link again, not a new one
    page = client.get(path)
    assert page.status_code == 200 and "PRACTICE" in page.text and "₹29" in page.text
    n = len(images)
    assert "Paid ✓" in client.post(path).text
    assert len(images) == n + 1 and "Payment received" in images[-1][2]
    client.post(path)
    assert len(images) == n + 1                                               # paying twice doesn't make two codes
    code = images[-1][1].rsplit("/", 1)[1].removesuffix(".png")
    wr.handle("91" + me, o["keyword"])
    assert images[-1][1].endswith(code + ".png")                              # asking again: the same QR
    assert client.get(f"/api/public/offer-qr/{code}.png").status_code == 200

    hit = client.get("/api/barcodes/lookup", headers=h(tok), params={"code": code, "outlet_id": ck}).json()
    assert hit["prepaid_paise"] == 2900
    client.post("/api/shifts", headers=h(tok), json={"outlet_id": ck, "opening_cash_paise": 0})
    od = client.post("/api/orders", headers=h(tok), json={"outlet_id": ck, "order_type": "takeaway",
                                                          "items": [{"variant_id": v, "qty": 1, "offer_code": code}]}).json()
    pv = client.post("/api/bills/preview", headers=h(tok), json={"order_id": od["id"]}).json()
    assert pv["total_paise"] == pv["prepaid_paise"] == 2900
    b = client.post("/api/bills", headers=h(tok), json={"order_id": od["id"], "customer": {}, "payments": []})
    assert b.status_code == 201, b.text
    b = b.json()
    assert b["customer_phone"] == me and [(p["mode"], p["amount_paise"]) for p in b["payments"]] == [("upi", 2900)]
    assert b["payments"][0]["reference"].startswith("Paid online test_")

    # a later visit by the same customer counts as brought back... on a later day; same-day bills don't
    roi = client.get(f"/api/offers/{o['id']}/roi", headers=h(tok)).json()
    assert roi["codes"] == {"sent": 1, "used": 1, "rate_bp": 10000}
    assert roi["links"]["sent"] == 1 and roi["links"]["paid"] == 1 and roi["links"]["collected_paise"] == 2900
    assert roi["sales"]["offer_items_paise"] == 2900 and roi["spent"]["discount_paise"] == max(0, o["regular_paise"] - 2900)
    assert roi["spent"]["razorpay_fees_paise"] == 0 and roi["customers"]["redeemed"] == 1


def test_prepaid_with_razorpay_webhook(client, monkeypatch):
    from app.offers import prepay
    from app.whatsapp import router as wr
    images, texts = _wa(monkeypatch)
    s = setup(client)
    tok, ck, v = s["tok"], s["ck"], s["v"]
    r = client.put("/api/tenant/payments", headers=h(tok), json={"enabled": True, "provider": "razorpay", "key_id": "rzp_test_x",
                                                                 "key_secret": "sec", "webhook_secret": "whsec"})
    assert r.status_code == 200, r.text
    key = r.json()["webhook_url"].rsplit("/", 1)[1]
    o = _offer(client, tok, v)
    made = []

    def fake_link(cfg, op, offer, tenant, item):
        op.link_id, op.short_url = "plink_" + op.id.hex[:10], "https://rzp.io/i/abc"
        made.append(op.link_id)
    monkeypatch.setattr(prepay, "_create_link", fake_link)
    monkeypatch.setattr(prepay, "refresh", lambda db, cfg, op: False)
    cust = "97" + str(uuid.uuid4().int)[:8]
    wr.handle("91" + cust, o["keyword"], "Riya")
    assert "https://rzp.io/i/abc" in texts[-1] and "practice" not in texts[-1]

    def hook(amount):
        ev = json.dumps({"event": "payment_link.paid", "payload": {
            "payment_link": {"entity": {"id": made[-1], "status": "paid"}},
            "payment": {"entity": {"id": "pay_" + uuid.uuid4().hex[:10], "amount": amount, "status": "captured", "fee": 68}}}}).encode()
        sig = hmac.new(b"whsec", ev, hashlib.sha256).hexdigest()
        return client.post(f"/api/public/razorpay/{key}", content=ev, headers={"X-Razorpay-Signature": sig})
    assert hook(2900).json() == {"ok": True}
    assert images and "Payment received" in images[-1][2] and images[-1][0] == "91" + cust
    n = len(images)
    hook(2900)
    assert len(images) == n                                                   # Razorpay retrying: one QR only
    roi = client.get(f"/api/offers/{o['id']}/roi", headers=h(tok)).json()
    assert roi["links"]["paid"] == 1 and roi["spent"]["razorpay_fees_paise"] == 68 and roi["links"]["paid_not_collected"] == 1

    # a short payment isn't honoured
    wr.handle("91" + cust, o["keyword"])          # they still have an unused QR: they get it again
    assert images[-1][1].endswith(".png")
