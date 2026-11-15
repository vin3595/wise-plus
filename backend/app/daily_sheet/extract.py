# Ported from Ease My Bill (receipt-analyzer/backend/daily_sheet.py) into wise-plus; only imports changed.
"""
Daily-sheet OCR extractor for Ease My Bill.

Ports the proven Claude prompt from the standalone "Shakewallah Ledger"
app (https://github.com/vin3595/SCANNER) so we can consume the same fixed
sheet layout inside EMB.

The extractor is intentionally **owner-gated at the endpoint layer** (not
here) — this module is pure logic and could be reused for other single-shop
templates in future.

Public entry point:
    extract_daily_sheet(image_base64: str) -> dict

Returns the same 10-field JSON contract as Shakewallah:
    date, cash_in, cash_out,
    vijay_ras_bhandar, expenses, expense_payouts, vijay_ras,
    absent, advances, dues, notes
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import uuid
from typing import Any

from PIL import Image, ImageOps
from ..purchases_ai.gemini import ImageContent, LlmChat, UserMessage, GEMINI_API_KEY

from .constants import (
    DENOMS,
    EXPENSE_ITEMS,
    VIJAY_RAS_BHANDAR_ITEMS,
    VIJAY_RAS_ITEMS,
)

logger = logging.getLogger("emb.daily_sheet")

# Sprint 2026-08 · Claude → Gemini hard swap.  `EMERGENT_LLM_KEY` is kept
# so downstream checks stay green; the active credential is
# `GEMINI_API_KEY` and lives inside gemini_client.
EMERGENT_LLM_KEY = GEMINI_API_KEY or os.environ.get("EMERGENT_LLM_KEY") or ""

# ---------------------------------------------------------------------------
# Prompt (ported verbatim from Shakewallah)
# ---------------------------------------------------------------------------
# This prompt has been battle-tested on real handwritten sheets. Key
# guardrails baked in:
#   - YY like "26" → 2026 (not 2024) so we never regress the date
#   - "denomination × count" arithmetic for cash grid
#   - "16 + 12" auto-sum for combined counts
#   - Never merge adjacent labels (food+parcel != foodparcel)
#   - "food" vs "good" handwriting quirk
#   - Stray margin words → notes, not expenses
#   - Blank cell → amount 0
EXTRACTION_SYSTEM = f"""You extract data from a photo of the Shakewallah daily
handwritten sheet (a Jodhpur milkshake shop). The sheet has fixed sections
and fixed item labels. Return ONLY a valid JSON object matching this schema
exactly (numbers, no currency symbols; sum any "a+b" expressions):

{{
  "date": "DD/MM/YY or YYYY-MM-DD if present, else null (IMPORTANT: 2-digit year YY like '26' means year 2000+YY = 2026, NOT 2024; the shop is in 2026 — never output a past year unless the sheet clearly shows a 4-digit year)",
  "cash_in":  {{"d500": int, "d200": int, "d100": int, "d50": int,
                "d20": int, "d10": int, "coins": number, "total": number}},
  "cash_out": {{"d500": int, "d200": int, "d100": int, "d50": int,
                "d20": int, "d10": int, "coins": number, "total": number}},
  "vijay_ras_bhandar": [ {{"name": "STRAWBERRY", "amount": number}}, ... ],
  "expenses":          [ {{"name": "MILK", "amount": number}}, ... ],
  "expense_payouts":   [ {{"name": "MUMMY|BHAVESH|VINEET", "amount": number}}, ... ],
  "vijay_ras":         [ {{"name": "KITKAT", "amount": number}}, ... ],
  "absent": ["staff name", ...],
  "advances": [ {{"name": "staff name", "amount": number}} ],
  "dues":     [ {{"name": "customer name", "amount": number}} ],
  "notes": "any handwritten stray notes worth keeping"
}}

Cash In/Out rows are "denomination × count" (e.g. "500 × 12" means twelve
500-rupee notes). Copy the count for d500/d200/d100/d50/d20/d10, the total
rupee value written next to COINS for "coins", and the SIGN/TOTAL number for
"total". If no total is written on the sheet, put 0. Never work it out yourself.

Fixed item lists you MUST include (with amount 0 if blank on sheet):
- Vijay Ras Bhandar: {VIJAY_RAS_BHANDAR_ITEMS}
- Expenses:          {EXPENSE_ITEMS} (includes MUMMY, BHAVESH, VINEET as fixed rows on the right side of the expenses table)
- Vijay Ras:         {VIJAY_RAS_ITEMS} (items SENT OUT to other store)

Any ADDITIONAL handwritten items in those sections should also be included
(e.g. "blueberry", "Vanilla") appended to the appropriate list. Do not
include explanations or markdown — JSON only.

CRITICAL RULES to prevent common mistakes:
1. NEVER merge two adjacent handwritten labels into one. Each cell in the
   sheet has ONE label and ONE number. If you see two words on separate
   rows (like "food" above "parcel", or "ice cream" above "sugar"), they
   are TWO separate expense items with their OWN amounts on their OWN
   rows — never combine them (do NOT output "food parcel" with a summed
   amount). Read left-to-right, top-to-bottom, one row at a time.
2. The Expenses table has a RIGHT-SIDE extras column where the user may
   write additional item labels (examples the user commonly uses:
   "ice cream", "sugar", "food", "parcel", "iron"). Each of these is a
   separate row with its own amount to the immediate right of the label.
3. Handwritten "food" can look like "good" — if the label ends in "ood"
   and appears in the expenses extras column, prefer "food".
4. In Cash In/Out, "16 + 12" means count = 28 (sum them). The SIGN/TOTAL
   value may also be written as "13,647 + 6000" — sum those too (=19647).
5. Stray words at the top or margins (like "Ankul" written by hand
   outside any table) are NOT expenses or items — put them in "notes".
6. If a cell is blank, amount = 0. Do not invent values."""


# ---------------------------------------------------------------------------
# JSON parsing + normalization helpers (ported)
# ---------------------------------------------------------------------------
def _num(x: Any, integer: bool = False) -> float | int:
    """Best-effort numeric coercion. Handles str/None/negatives/floats.

    We use this rather than raw ``float(x)`` because Claude occasionally
    returns strings like "1,280" or "₹455". The regex strips currency +
    thousands separators before parsing.

    Also supports "a+b" style sums (e.g. "16+3" written by the shop
    owner in a single denomination-count cell). This matched Shakewallah's
    original behavior and was a **material accuracy bug** in the first
    port — a "19+0" written in the 200-note count was being read as 0
    (the ``float()`` call failed) instead of 19. Chained "+" also works
    (e.g. "13647+6000" for the SIGN/TOTAL cell).
    """
    if x is None or x == "":
        return 0 if integer else 0.0
    if isinstance(x, (int, float)):
        return int(x) if integer else float(x)
    s = str(x).strip()
    # Remove common currency symbols and thousands separators (but keep '+').
    s = re.sub(r"[₹$,\s]", "", s)
    # "a+b" auto-sum (matches Shakewallah). Guard against a bare trailing
    # "+" which some LLMs emit for unclear cells.
    if "+" in s:
        parts = [p for p in s.split("+") if p.strip()]
        try:
            total = sum(float(p) for p in parts)
            return int(total) if integer else total
        except ValueError:
            return 0 if integer else 0.0
    try:
        val = float(s)
    except ValueError:
        return 0 if integer else 0.0
    return int(val) if integer else val


def _pdf_first_page_to_pil(pdf_bytes: bytes) -> Image.Image:
    """Render the first page of a PDF to a PIL image (RGB) via pypdfium2.

    Daily-sheet PDFs are almost always single-page scans / exports, so we
    only render page 1 — anything else is ignored (with a warning). Scale
    2.0 matches the invoicing extractor and gives Claude plenty of pixel
    density for handwritten numbers.
    """
    import pypdfium2 as pdfium  # local import — heavy native dep

    pdf = pdfium.PdfDocument(pdf_bytes)
    if len(pdf) == 0:
        raise ValueError("PDF has no pages")
    if len(pdf) > 1:
        logger.info("[daily_sheet] PDF has %d pages — extracting page 1 only", len(pdf))
    pil = pdf[0].render(scale=2.0).to_pil()
    if pil.mode not in ("RGB", "L"):
        pil = pil.convert("RGB")
    return pil


def _preprocess_image(image_base64: str) -> str:
    """Server-side clean-up before handing to Claude.

    Ports Shakewallah's ``_normalize_to_jpeg_b64``. Key operations:
      1. Strip any ``data:image/...;base64,`` prefix the client sent.
      2. If the payload is a PDF (``%PDF`` magic bytes or ``application/pdf``
         data-URL), render page 1 to a PIL image via pypdfium2.
      3. EXIF-orient the image (phone photos are usually orientation=6).
      4. Convert to RGB (drops alpha, HEIC → RGB).
      5. Resize with **LANCZOS resampling** to max **1600px** on the long
         side. LANCZOS is materially sharper than the browser's default
         canvas bilinear filter for handwritten pen strokes.
      6. Re-encode as **JPEG quality 85** (better than 0.82 for OCR while
         keeping payloads reasonable).

    This function is DEFENSIVE — if anything fails we return the input
    unchanged so the caller can still attempt extraction.
    """
    try:
        # Strip data-URL prefix if present, and remember if it was PDF.
        s = image_base64.strip()
        is_pdf_hint = "application/pdf" in s[:64].lower()
        i = s.find("base64,")
        if i >= 0:
            s = s[i + 7:]
        raw = base64.b64decode(s + "===")  # tolerate missing padding

        # Detect PDF from magic bytes as well (robust against missing prefix).
        if is_pdf_hint or raw[:4] == b"%PDF":
            try:
                img = _pdf_first_page_to_pil(raw)
            except Exception as e:
                logger.warning("[daily_sheet] PDF render failed: %s", e)
                raise
        else:
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

        MAX = 1600
        w, h = img.size
        scale = min(1.0, MAX / max(w, h))
        if scale < 1.0:
            img = img.resize(
                (int(w * scale), int(h * scale)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.warning("[daily_sheet] PIL preprocess failed: %s — using original bytes", e)
        return image_base64


def _parse_json_blob(text: str) -> dict:
    """Extract a JSON object from an LLM response.

    Handles the common patterns Claude / GPT emit:
      - Bare JSON: ``{"a": 1}``
      - Markdown fenced: ```json\\n{"a": 1}\\n```
      - JSON preceded/followed by prose ("Here is the extraction: {...}")
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def _normalize_named(items: list | None, fixed_list: list[str],
                     integer: bool = False) -> list[dict]:
    """Ensure the extraction contains every fixed_list row (with 0 if blank).

    Also uppercases + dedupes names. Sort order: fixed rows first (in the
    canonical order defined by ``fixed_list``), then any user-added extras
    alphabetically. This matches the on-sheet reading order and keeps the
    preview UI stable across scans.
    """
    result: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip().upper()
        if not name:
            continue
        if name in seen:
            continue  # de-dupe if Claude emits the same row twice
        result.append({"name": name, "amount": _num(it.get("amount"), integer)})
        seen.add(name)
    # Ensure every fixed item is present (default 0).
    for fixed in fixed_list:
        if fixed.upper() not in seen:
            result.append({"name": fixed, "amount": 0})
    # Sort: fixed order first, extras alphabetically.
    order = {n.upper(): i for i, n in enumerate(fixed_list)}
    result.sort(key=lambda x: (order.get(x["name"], 999), x["name"]))
    return result


def _cash_total(section: dict) -> float:
    """Recompute the SIGN/TOTAL for a cash section from the denomination
    breakdown. Used when the sheet's total cell is blank."""
    total = 0.0
    for d in DENOMS:
        total += d * _num(section.get(f"d{d}"), integer=True)
    total += _num(section.get("coins"))
    return total


def _normalize_cash(sec: dict | None) -> dict:
    """Normalize one cash section (in or out).

    Guarantees:
      - Every d500..d10 key exists as an int (0 if missing).
      - ``coins`` is a float.
      - ``total`` is only what is written on the sheet (0 when blank); the app adds up the notes itself.
    """
    c = sec or {}
    out = {f"d{k}": _num(c.get(f"d{k}"), True) for k in DENOMS}
    out["coins"] = _num(c.get("coins"))
    stated_total = _num(c.get("total"))
    out["total"] = stated_total
    return out


def _normalize_date(raw: Any) -> str | None:
    """Coerce a date string to YYYY-MM-DD (or None).

    Handles the sheet's DD/MM/YY and DD-MM-YYYY conventions. Two-digit
    years assume 2000+YY per the prompt guardrail.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", raw.strip())
    if not m:
        # Already ISO-ish?
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw.strip()):
            return raw.strip()
        return None
    dd, mm, yy = m.groups()
    if len(yy) == 2:
        yy = "20" + yy
    return f"{yy}-{int(mm):02d}-{int(dd):02d}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def extract_daily_sheet(image_base64: str) -> dict:
    """Extract structured daily-sheet data from a photo.

    Args:
        image_base64: Base64-encoded image bytes (JPEG/PNG). The caller is
            expected to have already run any format normalization (the EMB
            frontend already compresses/converts images before upload —
            see frontend/src/lib/imageCompress.js).

    Returns:
        A normalized dict with all 7 sections + date + notes. Every fixed
        item row is guaranteed to be present (0-valued if blank on the
        sheet). Cash totals are recomputed if the sheet's cell was empty.

    Raises:
        RuntimeError: EMERGENT_LLM_KEY missing.
        ValueError: image payload too small to be valid.
        RuntimeError: All three fallback models failed (with the last
            error chained via ``__cause__``).
    """
    if not EMERGENT_LLM_KEY:
        raise RuntimeError("EMERGENT_LLM_KEY is not configured")
    if not image_base64 or len(image_base64) < 100:
        raise ValueError("Image payload is too small to be a valid photo")

    # Server-side clean-up: EXIF-rotate, downscale with LANCZOS to 1600px,
    # re-encode JPEG 85. This is a *material* accuracy fix — the initial
    # port relied on the browser's canvas compression, which used bilinear
    # resampling and softened handwritten pen strokes just enough for
    # Claude to miscount notes (e.g. reading "19" as "17"). LANCZOS matches
    # the sharper output that the Shakewallah reference app produced.
    prepared_b64 = _preprocess_image(image_base64)

    msg = UserMessage(
        text="Extract the sheet data. JSON only.",
        file_contents=[ImageContent(image_base64=prepared_b64)],
    )

    # Sprint 2026-08 · Claude → Gemini hard swap. The provider chain is
    # now a single Gemini attempt (with 5-retry backoff baked into
    # gemini_client for transient 408/429/5xx). Kept as a list so we can
    # re-introduce a fallback later without touching the loop body.
    providers = [
        ("google", "gemini-3.5-flash-lite"),
    ]

    raw_response: str | None = None
    last_err: Exception | None = None
    for provider, model in providers:
        try:
            attempt = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=f"daily-sheet-scan-{uuid.uuid4()}",
                system_message=EXTRACTION_SYSTEM,
            ).with_model(provider, model)
            raw_response = await attempt.send_message(msg)
            logger.info("daily-sheet scan OK with %s/%s", provider, model)
            break
        except Exception as e:  # pragma: no cover — provider-level errors
            last_err = e
            logger.warning("daily-sheet scan failed with %s/%s: %s",
                           provider, model, str(e)[:200])
            continue

    if raw_response is None:
        raise RuntimeError(
            "All OCR providers failed to extract the daily sheet"
        ) from last_err

    # Some LLM responses are lenient JSON; recover if parse fails.
    try:
        d = _parse_json_blob(str(raw_response))
    except Exception:
        # Return the raw text so the caller can surface it to the user
        # for a "Please retry with a clearer photo" nudge.
        return {"raw_text": str(raw_response), "parse_error": True}

    return {
        "date": _normalize_date(d.get("date")),
        "cash_in": _normalize_cash(d.get("cash_in")),
        "cash_out": _normalize_cash(d.get("cash_out")),
        "vijay_ras_bhandar": _normalize_named(
            d.get("vijay_ras_bhandar"), VIJAY_RAS_BHANDAR_ITEMS
        ),
        "expenses": _normalize_named(d.get("expenses"), EXPENSE_ITEMS),
        # expense_payouts is legacy from Shakewallah; EMB re-uses the
        # MUMMY/BHAVESH/VINEET rows already inside expenses, so we keep
        # this array empty by default and only pass through if the OCR
        # populated it.
        "expense_payouts": _normalize_named(d.get("expense_payouts"), []),
        "vijay_ras": _normalize_named(d.get("vijay_ras"), VIJAY_RAS_ITEMS),
        "absent": [
            str(x).strip()
            for x in (d.get("absent") or [])
            if str(x).strip()
        ],
        "advances": [
            {"name": str(x.get("name", "")).strip(), "amount": _num(x.get("amount"))}
            for x in (d.get("advances") or [])
            if isinstance(x, dict) and str(x.get("name", "")).strip()
        ],
        "dues": [
            {"name": str(x.get("name", "")).strip(), "amount": _num(x.get("amount"))}
            for x in (d.get("dues") or [])
            if isinstance(x, dict) and str(x.get("name", "")).strip()
        ],
        "notes": (d.get("notes") or "").strip() if isinstance(d.get("notes"), str) else "",
    }
