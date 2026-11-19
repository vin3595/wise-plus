import os
import time

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")

from tests.test_api import client, h, migrate, signup  # noqa: E402,F401


def test_push_subscribe_notify_and_cleanup(client, monkeypatch):
    from app.push import service
    t = signup(client, "pushcafe", "9811177001")["token"]
    k = client.get("/api/push/key", headers=h(t)).json()
    assert len(k["key"]) > 80 and k["for_role"] is True
    assert client.get("/api/push/key", headers=h(t)).json()["key"] == k["key"]      # same key every time
    assert client.post("/api/push/test", headers=h(t), json={}).status_code == 409   # nothing subscribed yet
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "p256dh": "B" * 87, "auth": "a" * 22}
    assert client.post("/api/push/subscribe", headers=h(t), json=sub).status_code == 200
    sent = []
    monkeypatch.setattr(service, "send_one", lambda ep, p, a, payload: sent.append((ep, payload["title"])) or False)
    assert client.post("/api/push/test", headers=h(t), json={}).json()["sent"] == 0
    tid = client.get("/api/tenant/me", headers=h(t)).json()["tenant"]["id"]
    import uuid
    service._send_all(uuid.UUID(tid), "Hello", "World", "/", None, service.ROLES)
    assert ("https://fcm.googleapis.com/fcm/send/abc123", "Hello") in sent
    # the push service said the phone is gone: the row is removed
    sent.clear()
    service._send_all(uuid.UUID(tid), "Again", "x", "/", None, service.ROLES)
    assert sent == []
