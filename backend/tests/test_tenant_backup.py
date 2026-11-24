"""wise-plus: one business's backup and restore; another business is never touched. Plus the private uploads."""
import io
import os
import zipfile

import pytest

from tests.test_api import client, h, migrate  # noqa: F401  (fixtures)
from tests.test_banking import _bill
from tests.test_stock_extras import setup

pytestmark = pytest.mark.usefixtures("client")


def test_backup_and_restore_one_business(client, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.db import system_session
    from app.tenant_backup import restore_zip
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    a = setup(client)
    b = setup(client)                                   # someone else
    tok, ck, v = a["tok"], a["ck"], a["v"]
    client.post("/api/users", headers=h(tok), json={"name": "Asha", "role": "cashier", "pin": "7415", "outlet_ids": [ck]})
    asha = client.post("/api/auth/pin", json={"login_id": a["login"], "outlet_id": ck, "pin": "7415"}).json()["token"]
    client.post("/api/shifts", headers=h(asha), json={"outlet_id": ck, "opening_cash_paise": 0})
    bill = _bill(client, asha, ck, v, "upi")
    from app.models import Tenant
    from sqlalchemy import select
    with system_session() as db:
        tid = db.scalar(select(Tenant.id).where(Tenant.login_id == a["login"]))
    os.makedirs(tmp_path / "scans" / str(tid), exist_ok=True)
    (tmp_path / "scans" / str(tid) / "bill1.jpg").write_bytes(b"photo")

    assert client.get("/api/tenant/backup", headers=h(asha)).status_code in (401, 403)        # owner only
    r = client.get("/api/tenant/backup", headers=h(tok))
    assert r.status_code == 200 and r.headers["content-disposition"].endswith('-backup-' + r.headers["content-disposition"][-15:-5] + '.zip"')
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"data.json.gz", "readable.xlsx", "README.txt", "files/scans/business/bill1.jpg"} <= names
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(z.read("readable.xlsx")))
    assert wb["Bills"]["B2"].value == bill["invoice_no"]

    # after the backup: a new bill, a renamed supplier, the photo lost
    _bill(client, asha, ck, v, "cash")
    sup = client.get("/api/inventory/suppliers", headers=h(tok)).json()[0]
    assert client.patch(f"/api/inventory/suppliers/{sup['id']}", headers=h(tok), json={"name": "Renamed"}).status_code == 200
    (tmp_path / "scans" / str(tid) / "bill1.jpg").unlink()
    other_before = client.get("/api/inventory/suppliers", headers=h(b["tok"])).json()

    with system_session() as db:
        with pytest.raises(ValueError):
            restore_zip(db, r.content, expect_login=b["login"])
    with system_session() as db:
        counts = restore_zip(db, r.content, expect_login=a["login"])
    assert counts["bills"] == 1
    today = client.get(f"/api/bills?outlet_id={ck}", headers=h(tok)).json()
    assert [x["invoice_no"] for x in today] == [bill["invoice_no"]]
    assert client.get("/api/inventory/suppliers", headers=h(tok)).json()[0]["name"] == sup["name"]
    assert (tmp_path / "scans" / str(tid) / "bill1.jpg").read_bytes() == b"photo"
    assert client.get("/api/inventory/suppliers", headers=h(b["tok"])).json() == other_before       # untouched
    assert client.post("/api/auth/pin", json={"login_id": a["login"], "outlet_id": ck, "pin": "7415"}).status_code == 200


def test_private_files_not_public(client, tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    tid = "11111111-2222-3333-4444-555555555555"
    os.makedirs(tmp_path / tid)
    (tmp_path / tid / "logo-abc.png").write_bytes(b"png")
    os.makedirs(tmp_path / "scans" / tid)
    (tmp_path / "scans" / tid / "bill.jpg").write_bytes(b"secret")
    assert client.get(f"/uploads/{tid}/logo-abc.png").content == b"png"
    assert client.get(f"/uploads/scans/{tid}/bill.jpg").status_code == 404
    assert client.get(f"/uploads/{tid}/..%2Fscans").status_code == 404
    # expiring links
    from app.exports import make_link
    url = make_link(b"zipdata", "pack.zip")
    path = url.split("/api", 1)[1]
    assert client.get("/api" + path).content == b"zipdata"
    assert client.get("/api/public/files/short/pack.zip").status_code == 404


def test_removed_owner_token_cannot_download_backup(client):
    """M2: routes without a DB session still check the account live: a disabled user's token stops working."""
    from sqlalchemy import text
    from app.db import system_session
    b = setup(client)
    assert client.get("/api/tenant/backup", headers={"Authorization": f"Bearer {b['tok']}"}).status_code == 200
    with system_session() as db:
        db.execute(text("update users set is_active = false where tenant_id = (select id from tenants where login_id = :l)"), {"l": b["login"]})
    assert client.get("/api/tenant/backup", headers={"Authorization": f"Bearer {b['tok']}"}).status_code == 401
