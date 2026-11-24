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


def test_pin_lockout_is_per_address_and_grows(client: TestClient):
    """M3: wrong PINs lock that address (longer each time); the counter elsewhere keeps working."""
    from tests.test_stock_extras import setup
    from tests.test_api import h
    b = setup(client)
    client.post("/api/users", headers=h(b["tok"]), json={"name": "Asha", "role": "cashier", "pin": "7415", "outlet_ids": [b["ck"]]})
    bad, good = {"x-forwarded-for": f"{_ip()}, 172.18.0.3"}, {"x-forwarded-for": f"{_ip()}, 172.18.0.3"}
    pin = lambda p, hd: client.post("/api/auth/pin", headers=hd, json={"login_id": b["login"], "outlet_id": b["ck"], "pin": p})  # noqa: E731
    codes = [pin("0000", bad).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert pin("7415", bad).status_code == 429                  # locked even with the right PIN
    assert pin("7415", good).status_code == 200                 # the real counter isn't locked out
    from app.db import system_session
    from app.models import AuthThrottle
    from datetime import datetime, timezone, timedelta
    with system_session() as db:                                 # second lock in a row lasts twice as long
        row = next(r for r in db.query(AuthThrottle).filter(AuthThrottle.key.like(f"pin:%:{b['ck']}:%")).all() if bad["x-forwarded-for"].split(",")[0] in r.key)
        row.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    for _ in range(5):
        pin("0000", bad)
    with system_session() as db:
        row = next(r for r in db.query(AuthThrottle).filter(AuthThrottle.key.like(f"pin:%:{b['ck']}:%")).all() if bad["x-forwarded-for"].split(",")[0] in r.key)
        assert row.locked_until - datetime.now(timezone.utc) > timedelta(minutes=15)


def test_lead_form_limit_uses_the_real_address(client: TestClient):
    """M4: a forged first X-Forwarded-For doesn't get a fresh allowance."""
    real = _ip()
    statuses = []
    for i in range(8):
        r = client.post("/api/public/growth/leads", headers={"x-forwarded-for": f"{_ip()}, {real}, 172.18.0.3"},
                        json={"name": "Lead Tester", "phone": "9" + str(uuid.uuid4().int)[:9], "consent": True})
        statuses.append(r.status_code)
    assert 429 in statuses


def test_admin_link_survives_a_wrong_code(client: TestClient):
    """Low: a wrong guess no longer burns the real one-time admin link; the right code still works once."""
    import hashlib, time
    from sqlalchemy import text
    from app.db import system_session
    code = "x" * 32
    with system_session() as db:
        db.execute(text("delete from platform_secrets where key = 'admin_link'"))
        db.execute(text("insert into platform_secrets (key, value_enc) values ('admin_link', :v)"),
                   {"v": hashlib.sha256(code.encode()).hexdigest() + ":" + str(int(time.time()) + 900)})
    hd = {"x-forwarded-for": f"{_ip()}, 172.18.0.3"}
    def count():
        with system_session() as db:
            return db.execute(text("select count(*) from platform_secrets where key = 'admin_link'")).scalar()
    assert client.post("/api/admin/login/link", headers=hd, json={"code": "y" * 32}).status_code == 400
    assert count() == 1                          # still there after a wrong guess
    client.post("/api/admin/login/link", headers=hd, json={"code": code})
    assert count() == 0                          # used once by the right code
