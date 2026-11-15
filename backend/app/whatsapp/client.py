"""Sending WhatsApp messages through Meta's Cloud API (wise-plus). Settings: WISE_WA_* in the server's .env."""
from __future__ import annotations

import logging
import re

import httpx

from ..config import get_settings

log = logging.getLogger("wise.whatsapp")


def enabled() -> bool:
    s = get_settings()
    return bool(s.wa_token and s.wa_phone_number_id)


def to_wa(phone: str) -> str:
    """'98290 00001' / '+91 9829000001' -> '919829000001' (Indian numbers; others as given)."""
    d = re.sub(r"\D", "", phone or "")
    return "91" + d if len(d) == 10 else d


def last10(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def _post(payload: dict) -> dict:
    s = get_settings()
    url = f"https://graph.facebook.com/{s.wa_graph_version}/{s.wa_phone_number_id}/messages"
    r = httpx.post(url, json={"messaging_product": "whatsapp", **payload}, timeout=20,
                   headers={"Authorization": f"Bearer {s.wa_token}"})
    if r.status_code >= 300:
        log.warning("whatsapp send failed %s: %s", r.status_code, r.text[:300])
        r.raise_for_status()
    return r.json()


def send_text(to: str, body: str) -> dict:
    """Free text: only inside the 24-hour window after the person messaged us (replies to the owner's questions)."""
    return _post({"to": to_wa(to), "type": "text", "text": {"preview_url": False, "body": body[:4000]}})


def send_template(to: str, name: str, params: list[str] | None = None, lang: str = "en") -> dict:
    """An approved template: anytime (bills, order ready, reminders). Body variables {{1}}, {{2}}… in order."""
    comp = [{"type": "body", "parameters": [{"type": "text", "text": str(p)[:1000]} for p in params]}] if params else []
    return _post({"to": to_wa(to), "type": "template", "template": {"name": name, "language": {"code": lang}, "components": comp}})


def send_document(to: str, link: str, filename: str, caption: str | None = None) -> dict:
    return _post({"to": to_wa(to), "type": "document",
                  "document": {"link": link, "filename": filename, **({"caption": caption} if caption else {})}})
