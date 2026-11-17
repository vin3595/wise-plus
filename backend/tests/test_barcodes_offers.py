"""wise-plus: barcodes (own EAN-13, maker's codes, lookup at the counter) and WhatsApp keyword offers (QR, offer price, once)."""
import uuid

import pytest

from tests.test_api import client, h, migrate  # noqa: F401  (fixtures)
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")


def test_ean13():
    from app.offers.router import ean13_check, new_ean13, valid_ean13
    assert ean13_check("400638133393") == "1" and valid_ean13("4006381333931")       # a real product's EAN-13
    assert not valid_ean13("4006381333932")
    c = new_ean13(set())
    assert valid_ean13(c) and c[0] == "2"


def test_barcodes(client):
    s = setup(client)
    tok, ck, v, milk = s["tok"], s["ck"], s["v"], s["milk"]
    r = client.post("/api/barcodes/generate", headers=h(tok), json={"variant_ids": [v], "stock_item_ids": [milk["id"]]})
    assert r.json()["made"] == 2
    assert client.post("/api/barcodes/generate", headers=h(tok), json={"variant_ids": [v]}).json()["made"] == 0   # never replaced
    allb = client.get(f"/api/barcodes?outlet_id={ck}", headers=h(tok)).json()
    code = next(x["barcode"] for x in allb["items"] if x["variant_id"] == v)
    assert code.startswith("2") and len(code) == 13
    hit = client.get("/api/barcodes/lookup", headers=h(tok), params={"code": code, "outlet_id": ck}).json()
    assert hit == {"kind": "variant", "variant_id": v, "item_id": hit["item_id"]}
    menu = client.get(f"/api/menu/outlet/{ck}", headers=h(tok))
    if menu.status_code == 200:
        assert any(var.get("barcode") == code for it in menu.json()["items"] for var in it["variants"])
    # the maker's barcode on a bought-in product
    assert client.put(f"/api/barcodes/variants/{v}", headers=h(tok), json={"barcode": "4006381333932"}).status_code == 422   # misread
    assert client.put(f"/api/barcodes/variants/{v}", headers=h(tok), json={"barcode": "4006381333931"}).json()["barcode"] == "4006381333931"
    clash = client.put(f"/api/barcodes/stock/{milk['id']}", headers=h(tok), json={"barcode": "4006381333931"})
    assert clash.status_code == 409 and "already on" in clash.text
    stock = client.get("/api/barcodes/lookup", headers=h(tok), params={"code": allb["stock"][0]["barcode"] or "x", "outlet_id": ck})
    assert stock.status_code in (200, 404)
    assert client.get("/api/barcodes/lookup", headers=h(tok), params={"code": "123456789", "outlet_id": ck}).status_code == 404


def test_whatsapp_offer_to_bill(client, monkeypatch):
    from app.config import get_settings
    from app.whatsapp import client as wa
    from app.whatsapp import router as wr
    s = setup(client)
    tok, ck, v = s["tok"], s["ck"], s["v"]
    monkeypatch.setattr(get_settings(), "public_base_url", "https://plus.example.com")
    for k, val in {"wa_token": "t", "wa_phone_number_id": "123"}.items():
        monkeypatch.setattr(get_settings(), k, val)
    images, texts = [], []
    monkeypatch.setattr(wa, "send_image", lambda to, link, caption=None: images.append((to, link, caption)))
    monkeypatch.setattr(wa, "send_text", lambda to, body: texts.append(body))
    kw = "COFFEE" + uuid.uuid4().hex[:5].upper()
    r = client.post("/api/offers", headers=h(tok), json={"name": "Cold Coffee at ₹29", "keyword": kw.lower(), "variant_id": v,
                                                         "price_paise": 2900, "per_phone": 1, "code_days": 7})
    assert r.status_code == 201, r.text
    offer = r.json()
    assert offer["keyword"] == kw
    other = setup(client)
    assert client.post("/api/offers", headers=h(other["tok"]), json={"name": "xx", "keyword": kw, "variant_id": other["v"], "price_paise": 100}).status_code == 409

    cust = "97" + str(uuid.uuid4().int)[:8]
    wr.handle("91" + cust, f" {kw.lower()} ")
    to, link, caption = images[-1]
    code = link.rsplit("/", 1)[1].removesuffix(".png")
    assert link.startswith("https://plus.example.com/api/public/offer-qr/OF") and "₹29" in caption and code in caption
    wr.handle("91" + cust, kw)
    assert images[-1][1] == link                                       # same code again, not a second one
    png = client.get(f"/api/public/offer-qr/{code}.png")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert client.get("/api/public/offer-qr/OFAAAAAAAA.png").status_code == 404

    # the counter scans it
    hit = client.get("/api/barcodes/lookup", headers=h(tok), params={"code": code, "outlet_id": ck}).json()
    assert hit["kind"] == "offer" and hit["price_paise"] == 2900 and hit["variant_id"] == v
    line = {"variant_id": v, "qty": 1, "offer_code": code}
    q = client.post("/api/orders/quote", headers=h(tok), json={"outlet_id": ck, "items": [line]}).json()
    assert q["subtotal_paise"] == 2900
    assert client.post("/api/orders/quote", headers=h(tok), json={"outlet_id": ck, "items": [line | {"qty": 2}]}).status_code == 422
    od = client.post("/api/orders", headers=h(tok), json={"outlet_id": ck, "order_type": "takeaway", "items": [line]})
    assert od.status_code in (200, 201), od.text
    od = od.json()
    again = client.post("/api/orders", headers=h(tok), json={"outlet_id": ck, "order_type": "takeaway", "items": [line]})
    assert again.status_code == 409 and "already used" in again.text
    # cancelled before billing: the customer can use it again
    client.post(f"/api/orders/{od['id']}/cancel", headers=h(tok), json={"reason": "Customer left"})
    od2 = client.post("/api/orders", headers=h(tok), json={"outlet_id": ck, "order_type": "takeaway", "items": [line]})
    assert od2.status_code in (200, 201), od2.text
    tot = client.post("/api/bills/preview", headers=h(tok), json={"order_id": od2.json()["id"]}).json()
    assert tot["gross_paise"] == 2900
    # one per number: asking again after using it
    wr.handle("91" + cust, kw)
    assert "already used this offer" in texts[-1]
    lst = client.get("/api/offers", headers=h(tok)).json()["offers"][0]
    assert lst["issued"] == 1 and lst["used"] == 1
    # a code from another business doesn't exist here
    assert client.get("/api/barcodes/lookup", headers=h(other["tok"]), params={"code": code, "outlet_id": other["ck"]}).status_code == 409
    # ended offers give no codes
    client.put(f"/api/offers/{offer['id']}", headers=h(tok), json={"name": "xx", "keyword": kw, "variant_id": v, "price_paise": 2900, "is_active": False})
    wr.handle("91" + "96" + str(uuid.uuid4().int)[:8], kw)
    assert "ended" in texts[-1]
