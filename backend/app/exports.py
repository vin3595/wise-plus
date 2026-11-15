"""wise-plus: files made for someone (CA pack, a restaurant's backup) behind an unguessable link that expires.

WhatsApp can only send a document from a link Meta can fetch, so the file sits on disk for a few hours under a
random 32-character name. Nothing lists these files; after expiry the link answers 404 and the file is deleted."""
from __future__ import annotations

import os
import re
import secrets
import time

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from .config import get_settings

HOURS = 6
router = APIRouter(prefix="/public/files", tags=["files"])


def _dir() -> str:
    d = os.path.join(get_settings().upload_dir, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def sweep() -> None:
    now = time.time()
    for n in os.listdir(_dir()):
        p = os.path.join(_dir(), n)
        if now - os.path.getmtime(p) > HOURS * 3600:
            try:
                for f in os.listdir(p):
                    os.remove(os.path.join(p, f))
                os.rmdir(p)
            except OSError:
                pass


def make_link(data: bytes, filename: str) -> str:
    sweep()
    token = secrets.token_urlsafe(24)
    d = os.path.join(_dir(), token)
    os.makedirs(d)
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", filename)[:120] or "file"
    with open(os.path.join(d, safe), "wb") as f:
        f.write(data)
    return f"{get_settings().public_base_url.rstrip('/')}/api/public/files/{token}/{safe}"


@router.get("/{token}/{name}")
def fetch(token: str, name: str) -> FileResponse:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    p = os.path.join(_dir(), token, name)
    if not os.path.isfile(p) or time.time() - os.path.getmtime(p) > HOURS * 3600:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link has expired. Ask for the file again.")
    return FileResponse(p, filename=name)
