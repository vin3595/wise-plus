"""wise-plus: reading loose petty-cash slips (handwritten or printed, Hindi or English, one or many in a photo)."""
from __future__ import annotations

import base64
import re
import uuid
from datetime import date, timedelta

from .service import CATEGORIES, clean_category

PROMPT = (
    "You read petty-cash slips for an Indian restaurant: loose chits with no proper shop header, handwritten or printed, in "
    "Hindi, Hinglish or English (e.g. 'doodh 300', 'barf 2 silli 120', 'auto 50', 'plumber 400', '6 ltr milk 330'). One photo can "
    "hold several slips, or one slip with several lines. Return STRICT JSON only:\n"
    '{"slips": [{"date": "YYYY-MM-DD" or null, "amount": number (rupees), "what": short English description, '
    '"qty": number or null, "unit": "L"|"ml"|"kg"|"g"|"pc"|"dozen"|"packet"|"block"|"can"|"crate"|other short word or null, '
    '"category": one of ' + ", ".join(CATEGORIES) + ' or a short new one}], '
    '"has_gstin": true if a GSTIN / tax invoice header is printed, "note": anything unclear}\n'
    "Rules: one entry per amount paid. Use the line's own total, not a running total. Don't invent quantities: qty only "
    "when written (6 ltr, 2 kg, 3 silli). doodh/dudh = Milk, barf/baraf = Ice, sabzi = Vegetables, phal = Fruits, "
    "kiraya/auto/rickshaw = Transport, cylinder = Gas, paani/jar/can = Water, safai = Cleaning. "
    "Dates as written (Indian day/month order); null if none. No prose, no code fences."
)


class SlipError(Exception):
    pass


def clean(raw: dict, today: date) -> dict:
    out = []
    for s in raw.get("slips") or []:
        try:
            amt = int(round(float(str(s.get("amount") or 0).replace(",", "")) * 100))
        except (TypeError, ValueError):
            continue
        if amt <= 0 or amt > 50_000_00:
            continue
        d = None
        try:
            d = date.fromisoformat(str(s.get("date"))[:10]) if s.get("date") else None
        except ValueError:
            d = None
        if d and (d > today + timedelta(days=1) or d < today - timedelta(days=120)):
            d = None                                   # a misread year; the day it's entered is safer
        qty = None
        try:
            qty = int(round(float(s.get("qty")) * 1000)) if s.get("qty") not in (None, "", 0) else None
        except (TypeError, ValueError):
            qty = None
        unit = re.sub(r"[^A-Za-z ]", "", str(s.get("unit") or "")).strip()[:20] or None
        unit = {"ltr": "L", "litre": "L", "liter": "L", "l": "L", "kgs": "kg"}.get((unit or "").lower(), unit)
        out.append({"spent_on": (d or today).isoformat(), "amount_paise": amt, "what": str(s.get("what") or "").strip()[:120] or None,
                    "qty_m": qty if qty and qty > 0 else None, "unit": unit if qty else None, "category": clean_category(s.get("category"))})
    return {"slips": out, "has_gstin": bool(raw.get("has_gstin")), "note": str(raw.get("note") or "")[:200] or None}


async def read(data: bytes, content_type: str, name: str, today: date) -> dict:
    from ..purchases_ai.extraction import image_bytes_to_normalized_png, parse_json_relaxed, pdf_to_page_pngs
    from ..purchases_ai.gemini import GEMINI_API_KEY, ImageContent, LlmChat, UserMessage
    if not GEMINI_API_KEY:
        raise SlipError("The slip reader isn't set up on this server")
    is_pdf = content_type == "application/pdf" or name.lower().endswith(".pdf") or data[:4] == b"%PDF"
    try:
        pngs = pdf_to_page_pngs(data, max_pages=4) if is_pdf else [image_bytes_to_normalized_png(data, max_side=2000)]
    except Exception as e:  # noqa: BLE001
        raise SlipError("Couldn't open that photo. Try a clearer one.") from e
    files = [ImageContent(image_base64=base64.b64encode(p).decode()) for p in pngs]
    chat = LlmChat(api_key=GEMINI_API_KEY, session_id=f"slip-{uuid.uuid4()}", system_message=PROMPT)
    msg = UserMessage(text=f"Today is {today.isoformat()}. Read these slips.", file_contents=files)
    for _ in range(2):
        try:
            got = clean(parse_json_relaxed(await chat.send_message(msg)), today)
            if got["slips"]:
                return got
        except Exception:  # noqa: BLE001 - try once more
            continue
    raise SlipError("Couldn't find any amounts on that slip. Try a clearer photo, or type it in.")
