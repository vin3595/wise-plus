import os
import uuid

from app.purchases_ai.router import keep_file


def test_kept_file_name_cannot_escape_folder(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config.get_settings(), "upload_dir", str(tmp_path))
    sid = uuid.uuid4()
    for name in ["bill./../../evil", "x.PNG/../../a", "a.html", "noext"]:
        p = keep_file("t1", sid, b"data", "application/octet-stream", name)
        assert os.path.dirname(os.path.realpath(p)) == os.path.realpath(tmp_path / "scans" / "t1")
        assert p.rsplit(".", 1)[-1] in {"jpg", "png", "webp", "heic", "pdf", "jpeg", "bin"}
    assert keep_file("t1", sid, b"d", "image/jpeg", "a.exe").endswith(".jpg")
