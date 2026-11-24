"""Security: code sending is limited per address and overall; the client can't fake its address."""
import uuid

import os
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(not os.environ.get("WISE_RUN_API_TESTS"), reason="needs Postgres")
from tests.test_api import client, migrate  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _limits_on(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "rate_limits", True)


def _ip():
    return f"203.0.113.{uuid.uuid4().int % 250}" + f"-{uuid.uuid4().hex[:6]}"


def test_client_ip_is_the_entry_caddy_added():
    from starlette.requests import Request
    from app.core.ratelimit import client_ip
    req = lambda xff: Request({"type": "http", "headers": [(b"x-forwarded-for", xff.encode())], "client": ("10.0.0.5", 1)})
    assert client_ip(req("198.51.100.7, 172.18.0.3")) == "198.51.100.7"           # client, caddy
    assert client_ip(req("1.2.3.4, 198.51.100.7, 172.18.0.3")) == "198.51.100.7"   # a forged first entry is ignored


def test_sign_up_codes_limited_per_address(client: TestClient):
    ip = _ip()
    codes = 0
    for i in range(12):
        r = client.post("/api/auth/signup/start", headers={"x-forwarded-for": f"{ip}, 172.18.0.3"},
                        json={"restaurant_name": "Pump Cafe", "owner_name": "Pump", "email": f"p{i}{uuid.uuid4().hex[:5]}@x.com",
                              "phone": "9" + str(uuid.uuid4().int)[:9], "via": "phone"})
        if r.status_code == 200:
            codes += 1
        else:
            assert r.status_code == 429
    assert codes == 8                       # then refused from this address
    other = client.post("/api/auth/signup/start", headers={"x-forwarded-for": f"{_ip()}, 172.18.0.3"},
                        json={"restaurant_name": "Real Cafe", "owner_name": "Real", "email": f"r{uuid.uuid4().hex[:5]}@x.com",
                              "phone": "9" + str(uuid.uuid4().int)[:9], "via": "phone"})
    assert other.status_code == 200         # someone else isn't affected


def test_hourly_cap_stops_all_new_codes(client: TestClient, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "otp_hourly_cap", 0)
    r = client.post("/api/auth/signup/start", headers={"x-forwarded-for": f"{_ip()}, 172.18.0.3"},
                    json={"restaurant_name": "Cap Cafe", "owner_name": "Capo", "email": f"c{uuid.uuid4().hex[:5]}@x.com",
                          "phone": "9" + str(uuid.uuid4().int)[:9], "via": "phone"})
    assert r.status_code == 429 and "a lot of sign-ins" in r.json()["detail"]
