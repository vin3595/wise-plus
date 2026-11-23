"""WhatsApp templates through Fast2SMS: Meta-style payload -> GET /dev/whatsapp with the right values."""
import json

import pytest

from app.config import get_settings
from app.whatsapp import client


class _R:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, json.dumps(data)

    def json(self):
        return self._d


@pytest.fixture
def f2s(monkeypatch):
    s = get_settings()
    for k, v in {"wa_provider": "fast2sms", "fast2sms_api_key": "KEY", "wa_f2s_number_id": "555",
                 "wa_f2s_templates": json.dumps({"login_code": "11", "wise_update": "12"})}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(client, "_record", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: calls.append((url, kw)) or _R(200, {"status": True, "request_id": "r1"}))
    return calls


def test_template_goes_through_fast2sms(f2s):
    assert client.f2s_ready() and client.enabled()
    r = client.send_template("98290 00001", "wise_update", ["Shakewallah", "Line one\nline|two"])
    url, kw = f2s[0]
    assert url == "https://www.fast2sms.com/dev/whatsapp"
    assert kw["headers"] == {"Authorization": "KEY"}
    assert kw["params"] == {"message_id": "12", "phone_number_id": "555", "numbers": "9829000001",
                            "variables_values": "Shakewallah|Line one · line/two"}
    assert r["messages"][0]["id"] == "r1"


def test_code_only_in_button_and_header_media(f2s):
    client._post({"to": "919829000001", "type": "template", "template": {"name": "login_code", "components": [
        {"type": "header", "parameters": [{"type": "image", "image": {"link": "https://x/p.jpg"}}]},
        {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "123456"}]}]}})
    p = f2s[0][1]["params"]
    assert p["variables_values"] == "123456" and p["media_url"] == "https://x/p.jpg"


def test_missing_template_id_and_refusal(f2s, monkeypatch):
    with pytest.raises(client.WaError, match="no Fast2SMS ID"):
        client.send_template("9829000001", "promo_offer", ["a"])
    monkeypatch.setattr(client.httpx, "get", lambda url, **kw: _R(400, {"status": False, "message": "Invalid Message ID"}))
    with pytest.raises(client.WaError, match="Invalid Message ID"):
        client.send_template("9829000001", "login_code", ["1"])


def test_free_text_goes_as_update_template(f2s, monkeypatch):
    seen = []
    monkeypatch.setattr(client, "_post_meta", lambda p, _f=False: seen.append(p) or {})
    client.send_text("9829000001", "Your order\nis ready")
    assert not seen
    p = f2s[0][1]["params"]
    assert p["message_id"] == "12" and p["variables_values"] == "Ease My Bill|Your order · is ready"


def test_replies_through_fast2sms_webhook(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.whatsapp import router as wr
    monkeypatch.setattr(get_settings(), "wa_f2s_hook", "s3cret-hook-key")
    got = []
    monkeypatch.setattr(wr, "handle", lambda *a: got.append(("text",) + a))
    monkeypatch.setattr(wr, "handle_media", lambda *a: got.append(("media",) + a))
    monkeypatch.setattr(wr, "_seen_now", lambda *a: None)
    c = TestClient(app)
    assert c.post("/api/whatsapp/fast2sms/wrong", json={}).status_code == 403
    import uuid
    m1, m2 = uuid.uuid4().hex, uuid.uuid4().hex
    base = {"webhook_type": "incoming_message", "from": "919829000001", "phone_number_id": "555"}
    assert c.post("/api/whatsapp/fast2sms/s3cret-hook-key", json={**base, "message_id": m1, "message_type": "button", "body": "Stop offers"}).status_code == 200
    c.post("/api/whatsapp/fast2sms/s3cret-hook-key", data={**base, "message_id": m2, "message_type": "image", "media_url": "https://x/p.jpg", "mime_type": "image/jpeg"})
    c.post("/api/whatsapp/fast2sms/s3cret-hook-key", json={**base, "message_id": m1, "body": "dup"})
    c.post("/api/whatsapp/fast2sms/s3cret-hook-key", json={"webhook_type": "status_update", "status": "read"})
    assert got[0] == ("text", "919829000001", "Stop offers", "", None)
    assert got[1][:3] == ("media", "919829000001", "url:https://x/p.jpg") and len(got) == 2
