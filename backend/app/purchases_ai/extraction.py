# Ported from Ease My Bill (receipt-analyzer/backend/extraction.py) into wise-plus. Logic unchanged;
# only the import of the Gemini client moved. Keep it in step with EMB when the prompt improves.
"""Bill extraction service using Gemini 3.5 Flash Lite Vision.

Sprint 2026-08 · migration from Claude Sonnet 5 → Gemini 3.5 Flash Lite:
    * The customer explicitly wanted to bypass the Emergent universal key
      and be billed directly on their own Google Cloud project.
    * We swapped the whole OCR pipeline (this file + daily_sheet.py +
      expenses.py) in one shot. The Claude-specific comments below are
      retained for archaeological context only — every LlmChat call now
      routes to `gemini-3.5-flash-lite` via the shim in `gemini_client`.
    * `_provider_for(...)` and the `EMERGENT_LLM_KEY` env var remain
      referenced for API shape parity; the shim ignores both.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import io
import json
import os
import re
import uuid
from typing import Callable, Optional

import pypdfium2 as pdfium
from PIL import Image

from .gemini import LlmChat, UserMessage, ImageContent, GEMINI_API_KEY

# `EMERGENT_LLM_KEY` was the pre-2026-08 credential; retained so any
# downstream code that still checks for it doesn't crash. The active
# credential now lives in `GEMINI_API_KEY` (checked inside gemini_client).
EMERGENT_LLM_KEY = GEMINI_API_KEY or os.environ.get("EMERGENT_LLM_KEY") or ""


# ============================================================
# Document Classifier (Phase 14 — pre-extraction gate)
# Prevents junk documents (notebooks, ID cards, random photos) from
# being force-fitted into the bill schema and polluting the DB.
# ============================================================
CLASSIFIER_SYSTEM = (
    "You are a document classifier. Look at the provided image and decide "
    "what kind of document it is. Return STRICT JSON only, no prose, no fences.\n"
    "\n"
    "Schema: { \"type\": string, \"confidence\": integer 0-100, \"reason\": string }\n"
    "\n"
    "Allowed values for `type` (choose exactly one, uppercase):\n"
    "  INVOICE           - a vendor's tax invoice or GST bill (printed)\n"
    "  RECEIPT           - a printed sales receipt (thermal / POS / retail till)\n"
    "  HANDWRITTEN_BILL  - a small vendor's handwritten bill/kaccha bill/parchi\n"
    "  LEDGER_PAGE       - a cash register, expense diary, khata book, ledger page,\n"
    "                      daily accounts notebook, or any INTERNAL bookkeeping doc\n"
    "                      the shopkeeper wrote themselves (not received from a vendor)\n"
    "  BANK_STATEMENT    - bank passbook page, statement, or transaction listing\n"
    "  ID_DOCUMENT       - PAN card, Aadhaar, GST registration cert, driving license,\n"
    "                      or any government ID\n"
    "  RANDOM_PHOTO      - not a document (screenshot, meme, personal photo, food,\n"
    "                      selfie, empty page, hand, etc.)\n"
    "  OTHER             - a document that is not any of the above\n"
    "\n"
    "CLASSIFICATION RULES (read carefully):\n"
    "1. INVOICE / RECEIPT / HANDWRITTEN_BILL must have a clear VENDOR/SELLER name "
    "and line items or a total that the buyer is paying TO someone.\n"
    "2. A LEDGER_PAGE is written BY the business owner for internal tracking. Signs:\n"
    "   - 'Cash In' / 'Cash Out' columns\n"
    "   - Denomination breakdowns (500 x N, 200 x N, coins, etc.)\n"
    "   - Staff advance/due lists with names\n"
    "   - Category-style expenses (Milk, Snack, Tea, Petrol) rather than SKUs\n"
    "   - Multiple mini-sections on one page\n"
    "   - No single vendor being paid — it's a SUMMARY of the day\n"
    "3. Even if a LEDGER_PAGE contains prices/amounts, it is NOT an invoice.\n"
    "4. If the document has both a clear vendor header AND itemized purchases\n"
    "   billed TO the reader, it is INVOICE/RECEIPT/HANDWRITTEN_BILL.\n"
    "5. Confidence should be >= 80 when the document type is obvious; 50-79 when\n"
    "   somewhat ambiguous; below 50 when you genuinely cannot tell.\n"
    "6. `reason` must be one short sentence describing the visible evidence.\n"
    "\n"
    "Return JSON ONLY. Example: "
    "{\"type\":\"LEDGER_PAGE\",\"confidence\":92,\"reason\":\"Handwritten cash-in/cash-out "
    "register with denomination breakdown and staff advance list\"}"
)

# Document types that Ease My Bill will ACCEPT for extraction.
ACCEPTED_DOC_TYPES: set[str] = {"INVOICE", "RECEIPT", "HANDWRITTEN_BILL"}

# Confidence threshold: below this we still accept but warn the user.
CLASSIFIER_MIN_CONFIDENCE: int = 60


async def classify_document(png_bytes: bytes) -> dict:
    """Classify a single-image document into one of `ACCEPTED_DOC_TYPES` or
    a rejection category. Uses Claude Sonnet 5 (unified OCR provider).

    Returns a dict:
        { "type": str, "confidence": int, "reason": str,
          "accepted": bool, "should_warn": bool }
    On any failure (network, parse error) the fallback is a *permissive*
    result — we don't want the classifier to block legitimate uploads if
    Anthropic is having a bad day.
    """
    if not EMERGENT_LLM_KEY:
        # No LLM key configured — permissive fallback
        return {"type": "OTHER", "confidence": 0, "reason": "classifier disabled",
                "accepted": True, "should_warn": False}
    b64 = base64.b64encode(png_bytes).decode()
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"doc-classify-{uuid.uuid4()}",
            system_message=CLASSIFIER_SYSTEM,
        ).with_model(_provider_for("gemini-3.5-flash-lite"), "gemini-3.5-flash-lite")
        msg = UserMessage(
            text="Classify this document. Return JSON with type, confidence (0-100), reason.",
            file_contents=[ImageContent(image_base64=b64)],
        )
        text = await chat.send_message(msg)
        if not isinstance(text, str):
            text = str(text)
        raw = parse_json_relaxed(text)
        doc_type = str(raw.get("type", "OTHER")).strip().upper()
        try:
            conf = int(raw.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        conf = max(0, min(100, conf))
        reason = str(raw.get("reason", "")).strip()[:200]
        accepted = doc_type in ACCEPTED_DOC_TYPES
        # Even accepted types get a warning if confidence is low.
        should_warn = accepted and conf < CLASSIFIER_MIN_CONFIDENCE
        return {"type": doc_type, "confidence": conf, "reason": reason,
                "accepted": accepted, "should_warn": should_warn}
    except Exception as e:
        # Permissive fallback — never block on classifier errors.
        return {"type": "OTHER", "confidence": 0,
                "reason": f"classifier failed: {type(e).__name__}",
                "accepted": True, "should_warn": False}


EXTRACTION_SYSTEM = (
    "You are an expert receipt and invoice parser. "
    "Given an image of a bill, receipt or invoice, extract the structured data. "
    "Return STRICT JSON only. No prose, no markdown fences. "
    "Schema: {\n"
    '  "vendor": string | null,\n'
    '  "bill_date": string (YYYY-MM-DD) | null,\n'
    '  "invoice_number": string | null,\n'
    '  "currency": string | null,\n'
    '  "gstin": string | null,          // 15-char GSTIN of the seller if printed\n'
    '  "vendor_address": string | null, // Seller/vendor full postal address printed on the bill '
    '(street, city, state, PIN). Concatenate multi-line addresses with commas. Null if not printed.\n'
    '  "vendor_pan": string | null,     // 10-char alphanumeric PAN of the vendor if printed separately '
    '(e.g. "AABCU9603R"). If only GSTIN is visible, DO NOT derive PAN from it — leave null and let the backend derive it.\n'
    '  "vendor_phone": string | null,   // Vendor contact number (mobile / landline) printed on the bill. '
    'Prefer the format printed; if multiple, pick the first mobile-style one (10 digit). Null if not printed.\n'
    '  "vendor_email": string | null,   // Vendor email address printed on the bill. Null if not printed.\n'
    '  "buyer_gstin": string | null,    // 15-char GSTIN of the BUYER (\"Bill to\" / consignee GSTIN). '
    'Only fill if CLEARLY labeled as buyer/purchaser/consignee/Bill To/M/s GSTIN. '
    'CRITICAL: Do NOT fill this with the SELLER/VENDOR GSTIN by mistake. '
    'On most Indian tax invoices the seller GSTIN is printed prominently at the top; the buyer GSTIN '
    'is in the \"Bill To\" / \"Consignee\" / \"Purchaser\" section (usually smaller, lower on the page). '
    'If only ONE GSTIN is printed anywhere on the bill, it is almost always the SELLER GSTIN — '
    'leave buyer_gstin NULL in that case, do not guess. Return null when unsure.\n'
    '  "buyer_name": string | null,     // Buyer/consignee name if printed as \"Bill to\" / \"Consignee\" / \"M/s\". Null otherwise.\n'
    '  "place_of_supply": string | null, // e.g. "Maharashtra" if printed\n'
    '  "taxable_amount": number | null,  // total taxable value (before tax)\n'
    '  "cgst_amount": number | null,     // total CGST charged on the bill\n'
    '  "sgst_amount": number | null,     // total SGST/UTGST charged on the bill\n'
    '  "igst_amount": number | null,     // total IGST charged on the bill\n'
    '  "subtotal": number | null,\n'
    '  "tax": number | null,             // combined tax (CGST+SGST+IGST) if a single tax line printed\n'
    '  "total": number | null,\n'
    '  "global_discount_percent": number | null, // GLOBAL discount printed BELOW the items table '
    'as "Less: Discount X%" / "Cash Disc X%" / "Trade Disc X%" / "Scheme Disc X%" / "T.O. Disc X%". '
    'Applies to the whole bill, not a single line. '
    'Return the % (e.g. 5 for "Less: Discount 5%"). Null if none.\n'
    '  "global_discount_amount": number | null, // If the global discount is printed as an absolute ₹ '
    'amount ("Less: Discount ₹500", "Cash Disc ₹279", "Less Disc 279.35") instead of %, return the amount. '
    'CRITICAL: on distributor / wholesale bills the discount line is often crammed between the items table '
    'and the totals block — scan carefully for words like: Less, Disc, Discount, Cash Disc, Trade Disc, '
    'Scheme Disc, T.O.Disc, TO Disc, C/N (credit note adjust), Rounding-Off is NOT a discount. '
    'Null if % is used or if no discount at all.\n'
    '  "items": [ { "name": string, "quantity": number, "unit_price": number, "total": number, '
    '"mrp": number | null, '
    '"hsn": string | null, "tax_rate": number | null, '
    '"discount_percent": number | null, "discount_amount": number | null } ]\n'
    "}\n"
    "GENERAL RULES:\n"
    "- Numeric fields must be numbers, not strings. Strip currency symbols and thousand separators.\n"
    "- unit_price is the WHOLESALE per-unit rate the buyer PAYS (Basic Rate / Rate); mrp is the RETAIL sticker price. Never swap them.\n"
    "- Do NOT include tax lines, discounts, subtotal, or 'grand total' rows in items.\n"
    "- Do NOT include continuation-page totals, previous-balance-brought-forward rows, or bank-details rows as items.\n"
    "\n"
    "DISTRIBUTOR / WHOLESALE MULTI-COLUMN BILLS (Vadilal, Amul, HUL, Britannia, Havmor, "
    "Shiv Gauri, distributor-style tax invoices — CRITICAL — READ TWICE):\n"
    "These bills have MANY columns per line item. A typical layout has 12-16 columns like:\n"
    "  Sr | Item Code | Product Name | HSN | MRP/Car | MRP/Pcs | UOM | FOC | Basic Rate | "
    "PremDisc | SchDisc | Net Rate | Taxable Amt | GST% | GSTVal | Total Value\n"
    "\n"
    "★★★ THE GOLDEN RULE — GROUND-TRUTH-FIRST EXTRACTION ★★★\n"
    "The single most reliable piece of information on every row is the LAST rupee amount\n"
    "(the RIGHTMOST big number, usually labeled \"Total Value\" / \"Amount\" / \"Total\" /\n"
    "\"Line Total\"). All other numbers are derivable from it. Extract the last amount\n"
    "column first, then work backwards.\n"
    "\n"
    "  FIELDS TO EXTRACT PER LINE (priority order — get the first two RIGHT, others are optional):\n"
    "    🥇 total           ← REQUIRED. LAST rupee amount on the row — \"Total Value\" / \"Amount\" /\n"
    "                          \"Line Total\" / \"Value\". This is the FINAL amount for the row\n"
    "                          INCLUDING GST. It is almost always the RIGHTMOST big number.\n"
    "                          ★ sum(items.total) MUST equal the printed bill grand total ★\n"
    "                          (within ±1% for rounding). If it doesn't, you picked the\n"
    "                          wrong column — RE-READ using the rightmost amount instead.\n"
    "    🥇 quantity        ← REQUIRED. NUMERIC part of the UOM/Qty column ONLY.\n"
    "                          \"2 CAR\" → 2. \"6 PCS\" → 6. \"1 CAR\" → 1.\n"
    "                          IMPORTANT: Ignore packaging tags in the product NAME like\n"
    "                          \"[1*1] 5000ML\", \"12X1 100ML\", \"1x24 200GM\". These describe\n"
    "                          the pack contents, NOT the quantity purchased. The quantity\n"
    "                          purchased is ALWAYS in a separate UOM column with the unit\n"
    "                          suffix (CAR / BOX / PCS / DOZ / KG / LTR).\n"
    "                          MUST be a positive INTEGER for CAR/BOX/PCS/DOZ UOMs.\n"
    "    ①  name            ← Product Name column (INCLUDE the pack-size suffix like\n"
    "                          \"[1*1] 5000ML\" as part of the name — it identifies the SKU).\n"
    "    ②  hsn             ← HSN Code column (6-8 digits).\n"
    "    ③  unit_price      ← \"Basic Rate\" column (distributor's wholesale per-unit rate that\n"
    "                          the BUYER actually pays, BEFORE any scheme/premium discount and\n"
    "                          BEFORE GST). This is typically LESS than MRP and MORE than the\n"
    "                          Net Rate. Column is often labeled \"Basic Rate\" / \"Rate\" /\n"
    "                          \"B.Rate\" / \"Rate/Pcs\" / \"Rate/Car\" (matching the UOM).\n"
    "                          ★ DO NOT put MRP into unit_price — MRP is the retail sticker\n"
    "                          price the shopkeeper resells at, not what they pay. Using it\n"
    "                          as unit_price causes false price-change alerts and wrong cost\n"
    "                          reporting. ★\n"
    "                          IF THE BILL HAS NO \"Basic Rate\" COLUMN (small retail bills\n"
    "                          often don't) fall back to the MRP column — but only if this is\n"
    "                          clearly a retail-style bill without any wholesale rate column.\n"
    "                          IF IN ANY DOUBT which column is Basic Rate, LEAVE THIS NULL.\n"
    "                          The backend will safely derive per-unit rate = total ÷ qty\n"
    "                          (the net incl. GST rate) — infinitely better than a wrong-\n"
    "                          column value.\n"
    "    ④  mrp             ← MRP column (retail list price for reference / price-change\n"
    "                          alerts on RESALE price). Optional metadata; null if not shown.\n"
    "    ⑤  discount_amount ← SUM of PremDisc + SchDisc + any other \"Disc\" ₹ columns for\n"
    "                          this row. Emit 0 (not null) if visibly 0.\n"
    "    ⑤ᵇ discount_percent ← If the row has a \"Disc. %\" / \"Disc%\" / \"Discount %\" column\n"
    "                          with a % rate per line (e.g. \"12 %\", \"12%\", \"12.5\"), extract\n"
    "                          it here. This is a PER-LINE trade discount — often 5% - 20%\n"
    "                          on distributor bills. Null if the column doesn't exist.\n"
    "                          Example — a Shiv Gauri / Shiv Gouri Agencies bill layout:\n"
    "                            Description | Quantity | Rate | per | Disc. % | Amount\n"
    "                            Vanilla Gold  |  10.0 Bx | 430.84| Bx | 12 %   | 3,791.39\n"
    "                          Extract: quantity=10, unit_price=430.84, discount_percent=12,\n"
    "                          total=3791.39. tax_rate stays NULL here — the 12% is a\n"
    "                          discount, NOT GST. GST comes from the SGST/CGST block below.\n"
    "    ⑥  tax_rate        ← GST% ONLY when the COLUMN HEADER explicitly says one of:\n"
    "                          \"GST%\" / \"GST %\" / \"Tax%\" / \"IGST%\" / \"CGST+SGST%\" /\n"
    "                          \"CGST%\" / \"SGST%\" / \"Rate of Tax\". Valid values: 0, 5, 12,\n"
    "                          18, 28. If the header says \"Disc\" / \"Disc %\" / \"Discount\"\n"
    "                          / \"Scheme\" / \"Trade\" — that column is a DISCOUNT, not tax.\n"
    "                          NEVER put a discount % into tax_rate. If no GST% column\n"
    "                          exists per-line (many small distributor bills only show GST\n"
    "                          at the totals block as \"SGST 2.5% + CGST 2.5%\"), leave\n"
    "                          tax_rate NULL — the backend will read the summary block via\n"
    "                          cgst_amount / sgst_amount instead.\n"
    "\n"
    "  COLUMNS TO ACTIVELY IGNORE (mis-reading these is the #1 cause of wrong extractions):\n"
    "    ✗ Sr No / Serial\n"
    "    ✗ Item Code / SKU\n"
    "    ✗ FOC (Free-of-Cost)\n"
    "    ✗ Net Rate (do NOT put this in unit_price — it's post-discount, will corrupt price tracking)\n"
    "    ✗ Taxable Amt (redundant; will be recomputed from total ÷ (1 + GST%/100))\n"
    "    ✗ GSTVal in ₹ (redundant)\n"
    "    ✗ MRP columns as unit_price — put MRP into the `mrp` field, use \"Basic Rate\" for unit_price\n"
    "    ✗ \"Rate/Car\" vs \"Rate/Pcs\" — pick ONE matching the UOM; if unsure, leave unit_price null\n"
    "    ✗ ★★★ \"Disc %\" / \"Disc. %\" / \"Discount %\" column as tax_rate ★★★  This is the\n"
    "        #1 misread on Shiv Gauri / Amul / small distributor bills. If the column header\n"
    "        contains the word \"Disc\" it is a DISCOUNT, put it in discount_percent (or\n"
    "        discount_amount if the column is ₹). tax_rate stays NULL unless a separate\n"
    "        GST% column exists.\n"
    "\n"
    "  ⚠️  ANTI-PATTERNS — DO NOT DO THESE (they've caused real production failures):\n"
    "    ✗ Emitting unit_price=0 with total=0 for a row that clearly has a printed amount.\n"
    "       → If you can't confidently read the row, EMIT quantity=<visible> and\n"
    "         total=<rightmost amount>. Leave unit_price NULL. The backend will derive it.\n"
    "    ✗ Reading the packaging tag \"[1*1]\" or \"1X1\" or \"12*1\" from the name as a\n"
    "       multiplier and dividing unit_price or total by it. NEVER do this.\n"
    "    ✗ Emitting the SAME unit_price you see printed AS the MRP but with the QUANTITY\n"
    "       from a different row. Every field must come from the SAME row.\n"
    "    ✗ Force-fitting numbers to make sum(items.total) equal the grand total when you\n"
    "       weren't sure of the individual line totals. Instead, emit what you actually\n"
    "       read — the backend will flag the bill for user review if the math is off.\n"
    "\n"
    "  FINAL SANITY CHECK before returning:\n"
    "    1. sum(items.total) ≈ bill.total (±1%). If OFF, you almost certainly picked the wrong\n"
    "       amount column. Re-read using the RIGHTMOST amount column on each row.\n"
    "    2. Every quantity is a positive integer for CAR/BOX/PCS/DOZ UOMs.\n"
    "    3. No quantity is a huge number like 100/500/1000 unless the UOM is PCS AND the\n"
    "       total is in matching magnitude. If quantity > 100 for a CAR/BOX row, you\n"
    "       almost certainly read an amount column into quantity — re-read.\n"
    "    4. If a row's unit_price is filled AND quantity × unit_price differs from total by\n"
    "       more than ~30% AND no discount is captured, you probably picked the wrong\n"
    "       column for unit_price. Prefer to LEAVE UNIT_PRICE NULL in that case.\n"
    "\n"
    "GENERAL RULES (continued):\n"
    "- Use ISO date (YYYY-MM-DD). Indian bills ALMOST ALWAYS use DD/MM/YY or DD-MM-YYYY format "
    "(day first, then month, then year). NEVER interpret dd/mm as mm/dd — 02/06/26 is 2nd June 2026, "
    "NOT 6th February 2026 or 2nd August 2026.\n"
    "- A '2-digit year' like 24, 25, 26 almost always means 2024, 2025, 2026 — not 1924/1925/1926.\n"
    "- SANITY-CHECK the date: (a) if it lies more than 12 months in the future from today, re-read the digits — the "
    "month or year is likely misread; prefer null over a wrong future date. (b) If it's more than 3 years in the past AND "
    "the bill is computer-printed with GST columns, re-read the year digits.\n"
    "- If the bill prints multiple dates (invoice date, delivery date, PO date, e-way bill date), always pick the "
    "INVOICE DATE / BILL DATE — never a delivery/dispatch/vehicle date.\n"
    "- If in doubt about the date, leave bill_date null rather than guessing wrong.\n"
    "- Output ONLY the JSON object.\n"
    "\n"
    "DISCOUNT RULES (India retail/wholesale bills — CRITICAL for price tracking):\n"
    "Bills use ONE of these two discount styles — extract accordingly:\n\n"
    "STYLE A — PER-LINE DISCOUNT (each row has a Disc%/Sch%/Scheme % column):\n"
    "- For each row: put the % in `discount_percent` OR the per-unit ₹ in `discount_amount`. Set global_discount_* to null.\n\n"
    "STYLE B — GLOBAL DISCOUNT (single \"Less: Discount X%\" or \"Cash Disc X%\" printed BELOW the items table):\n"
    "- Put the % in `global_discount_percent` (or ₹ in `global_discount_amount`). Set every item's `discount_percent`/`discount_amount` to null.\n"
    "- DO NOT double-count: if a bill uses global discount, do not also fill per-line discount fields (would apply twice).\n\n"
    "GENERAL DISCOUNT PRINCIPLES:\n"
    "- Watch for labels: 'Disc%', 'Sch%', 'Scheme %', 'CD%', 'Cash Disc', 'Trade Disc', 'Less: Discount', 'Discount' (below table).\n"
    "- `unit_price` MUST always be the LIST/MRP rate BEFORE any discount. Do NOT pre-subtract discount into unit_price.\n"
    "- On distributor bills (Vadilal, Amul, Britannia, HUL etc.) the discount column is often labeled 'Sch %' — do not miss it (typically 8–15%).\n"
    "- On smaller retailers (Kanha Food Products, Sharma Distributors etc.) the discount is often a single global line beneath the items block.\n"
    "- If a bill shows both a per-line discount AND a global discount, extract both — but flag this in your best judgement.\n"
    "- The line `total` printed on the bill is the NET amount after discount + tax. It's OK if `qty × unit_price` doesn't equal `total` — the difference is the discount + tax.\n"
    "- On bills with a 'Gross Amount → Discount → Taxable Amount' column layout: Gross ÷ Qty gives unit_price; (Gross − Taxable) ÷ Gross × 100 gives discount_percent for that line.\n"
    "\n"
    "QUANTITY RULES:\n"
    "- Retail/grocery bills in India almost always use INTEGER quantities (1, 2, 5, 10, 24, ...). Prefer integer readings.\n"
    "- If you see '5.00' or '5.0', normalize to 5 (integer). Only emit a decimal quantity when the bill's Qty column "
    "clearly shows a non-integer fractional value (e.g. 2.5 KG, 0.5 L, 1.75).\n"
    "- Bills with UOM = KG/LITRE/METRE/GRAM legitimately have decimals; otherwise prefer integers.\n"
    "\n"
    "GST FIELD RULES (India-specific, CRITICAL):\n"
    "- GSTIN is a 15-character alphanumeric code (e.g. 27AABCU9603R1ZM). Only emit if you clearly see it printed on the bill; otherwise null.\n"
    "- If both CGST and SGST are printed, IGST should almost always be null (intra-state).\n"
    "- If IGST is printed, CGST and SGST should almost always be null (inter-state).\n"
    "- taxable_amount + cgst_amount + sgst_amount + igst_amount \u2248 total (within \u00b12%). If they do not, re-read the numbers.\n"
    "- **TAX-EXTRACTION SANITY CHECK (MANDATORY):** If you extracted a `subtotal` AND a `total` AND their difference is > \u20b95 "
    "AND `cgst_amount`+`sgst_amount`+`igst_amount` are ALL null/zero, then YOU HAVE MISSED THE TAX LINES. "
    "Before returning, re-scan the entire bill BOTTOM SECTION carefully for lines labelled: "
    "`SGST%`, `CGST%`, `IGST%`, `UTGST%`, `GST @ X%`, `Add: GST`, `Tax @ 5%`, or any percentage-labelled line item. "
    "These tax lines can appear in the totals summary block, below the items table, in the right-hand column, "
    "OR sometimes squished under discount lines. Extract the RUPEE amount next to each tax label into "
    "cgst_amount / sgst_amount / igst_amount. Do NOT stuff missed tax into round-off.\n"
    "- If bill shows `SGST 2.5%` and `CGST 2.5%` separately, they are two lines \u2014 read BOTH amounts. Same for CGST/SGST at 9% (=18% GST) or 14% (=28% GST).\n"
    "- Real round-off amounts are ALWAYS small (usually |x| \u2264 \u20b91.00, rarely up to \u20b95). If your computed round-off is > \u20b95, you have missed something.\n"
    "- HSN (or SAC) codes are typically 4\u20138 digit codes shown against each line item. Extract per item only if printed.\n"
    "- tax_rate is a percentage (e.g. 18 for 18%). Combined rate (CGST+SGST or IGST). Leave null if not printed.\n"
    "- If any GST field is not clearly visible on the bill, emit null \u2014 do NOT guess.\n"
    "\n"
    "COLUMN IDENTIFICATION (CRITICAL for accuracy):\n"
    "1) First identify the columns of the line-items table by their headers. Common headers:\n"
    "   Quantity/Qty/Nos/Pieces/Pcs   |   Rate/Unit Price/MRP/Price   |   Amount/Total/Value   |   HSN/SAC   |   GST%/Tax%\n"
    "2) A Quantity is typically a small integer (1 to ~200) or a simple decimal (e.g. 2.5). "
    "It is almost never a value in the hundreds of thousands or a value that equals the amount column.\n"
    "3) A Unit Price is a per-unit price. It typically has 2 decimal places or is a round rupee/dollar amount.\n"
    "4) An Amount/Total is the row total = quantity \u00d7 unit_price. It is usually the LARGEST number in the row.\n"
    "5) NEVER swap columns \u2014 do not confuse the Amount column with Quantity even if they look similar.\n"
    "\n"
    "LINE-ITEM MATH VERIFICATION (do this internally BEFORE emitting JSON):\n"
    "- For EVERY line item verify that quantity \u00d7 unit_price is approximately equal to total (within \u00b11%).\n"
    "- If the math does not hold, re-read the digits with more care. Common OCR mistakes to avoid:\n"
    "    * Reading the Amount column value into Quantity (e.g. an amount of 570 read as qty=570 when the actual qty is 5).\n"
    "    * Extra zeros (10 vs 100, 5 vs 50 or 500).\n"
    "    * Missing decimal points (145 vs 14.5).\n"
    "- If two of the three numbers are consistent and one is obviously wrong, correct the wrong one.\n"
    "- If quantity is not explicit and no math is possible, use quantity = 1.\n"
    "- If unit_price is missing but total and quantity are known, compute unit_price = round(total/quantity, 2).\n"
    "- If total is missing but unit_price and quantity are known, compute total = round(unit_price*quantity, 2).\n"
    "- If ONLY total is present with no clear quantity indicator, use quantity = 1 and unit_price = total.\n"
    "\n"
    "PLAUSIBILITY CHECK:\n"
    "- Quantities above 1000 for a single line are very unusual on a business bill. If you're about to emit "
    "a quantity > 1000, re-check whether that number is actually the amount column, not the quantity column.\n"
    "- If you can't confidently read the quantity, prefer 1 over an implausible large number.\n"
)


def parse_json_relaxed(text: str) -> dict:
    """Extract a JSON object even if wrapped in code fences or prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _sanitize_number(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        s = str(x).strip().replace(",", "")
        s = re.sub(r"[^0-9.\-]", "", s)
        return float(s) if s not in ("", ".", "-") else None
    except Exception:
        return None


def _reconcile_line_item(name: str, qty, unit_price, total, has_discount: bool = False):
    """Post-processing sanity check: fix minor OCR mismatches BUT never
    silently rewrite line items when the discrepancy is huge (that used
    to mask wrong-column picks on distributor bills and produce
    plausible-looking-but-wrong numbers). Returns
    (qty, unit_price, total, reconciled_flag).

    Sprint 2026-07 hotfix (Shiv Gauri / Vadilal case study):
      OCR was picking the wrong column for unit_price on Vadilal-style
      bills (e.g. reading the printed Net Rate ex-GST as unit_price
      while emitting a total that included GST + qty). The old
      reconciler would then back-compute qty = total / unit_price and
      corrupt the quantity to a decimal like 10.56, or leave unit_price
      wildly off while showing a matching grand total. The new rules:

        - If discount info is present, do not touch the values at all
          (net vs list mismatch is legitimate).
        - If qty × unit_price differs from total by ≤15%, we can safely
          reconcile (small OCR noise like a missed decimal).
        - If the mismatch is >15%, DO NOT silently rewrite. Instead
          derive unit_price from total ÷ qty (the net incl. GST per
          unit) so the line at least sums correctly, keep qty & total
          intact, and flag ``reconciled=True`` so callers know a review
          is warranted.
        - Never fabricate a decimal quantity for CAR/BOX/PCS items.
    """
    reconciled = False
    if qty is None:
        qty = 1.0
    try:
        qty = float(qty)
        up = float(unit_price) if unit_price is not None else None
        tot = float(total) if total is not None else None
    except Exception:
        return qty, unit_price, total, reconciled

    # Nothing to reconcile if we don't have enough info
    if up is None and tot is None:
        return qty, up, tot, reconciled
    if up is not None and tot is None:
        # Don't fabricate a total when discount info is present — the real
        # total (net after disc + GST) is different from qty × list_price.
        if has_discount:
            return qty, up, tot, reconciled
        return qty, up, round(qty * up, 2), reconciled
    if up is None and tot is not None:
        # infer unit_price as net-incl-tax per unit (safe fallback that
        # always makes qty × unit_price == total).
        if qty > 0:
            return qty, round(tot / qty, 2), tot, reconciled
        return qty, up, tot, reconciled

    # If discount is printed on the row, the "mismatch" between qty*up and tot
    # is legitimate (net vs list). Leave the extracted values alone.
    if has_discount:
        return qty, round(up, 2), round(tot, 2), reconciled

    # All three present — check consistency.
    if qty <= 0 or up <= 0 or tot <= 0:
        return qty, round(up, 2), round(tot, 2), reconciled

    expected = qty * up
    delta = abs(expected - tot) / max(expected, tot)

    # Case A: tiny noise (≤ 2%) — accept as-is.
    if delta <= 0.02:
        return qty, round(up, 2), round(tot, 2), reconciled

    # Case B: small OCR noise (≤ 15%) — likely a missing decimal on one
    # field. Trust qty (small integer) and total (rightmost printed
    # amount) as the ground truth, derive unit_price.
    if delta <= 0.15:
        up_new = tot / qty if qty > 0 else up
        return qty, round(up_new, 2), round(tot, 2), True

    # Case C: LARGE mismatch (>15%). This is the danger zone where the
    # old reconciler used to invent a decimal quantity like 10.56 or
    # swap columns. NEW behaviour: keep qty + total (the two most
    # visually unambiguous fields on the bill), and derive unit_price
    # from them. Flag reconciled=True so the caller knows to surface
    # this row in the review queue.
    if qty > 0:
        up_new = tot / qty
        return qty, round(up_new, 2), round(tot, 2), True

    # Fallback — should be unreachable given the guards above.
    return qty, round(up, 2), round(tot, 2), reconciled

# ------------------------------------------------------------------
# Legacy candidate-based reconciler (kept for reference / future
# rollback but no longer used). The new _reconcile_line_item above
# supersedes it for all distributor-bill cases.
# ------------------------------------------------------------------
def _reconcile_line_item_legacy(name: str, qty, unit_price, total, has_discount: bool = False):
    reconciled = False
    if qty is None:
        qty = 1.0
    try:
        qty = float(qty)
        up = float(unit_price) if unit_price is not None else None
        tot = float(total) if total is not None else None
    except Exception:
        return qty, unit_price, total, reconciled

    # Nothing to reconcile if we don't have enough info
    if up is None and tot is None:
        return qty, up, tot, reconciled
    if up is not None and tot is None:
        if has_discount:
            return qty, up, tot, reconciled
        return qty, up, round(qty * up, 2), reconciled
    if up is None and tot is not None:
        if qty > 0:
            return qty, round(tot / qty, 2), tot, reconciled
        return qty, up, tot, reconciled

    if has_discount:
        return qty, round(up, 2), round(tot, 2), reconciled

    expected = qty * up
    tolerance = max(abs(tot) * 0.02, 0.5)
    if abs(expected - tot) <= tolerance:
        return qty, round(up, 2), round(tot, 2), reconciled

    candidates = []
    if up != 0:
        q_new = tot / up
        if 0 < q_new <= 1000:
            rounded = round(q_new)
            if abs(q_new - rounded) < 0.05 and rounded > 0:
                candidates.append((abs(q_new - rounded), "qty", float(rounded), up, tot))
            elif q_new > 0:
                candidates.append((0.1, "qty", round(q_new, 3), up, tot))
    if 0 < qty <= 200:
        candidates.append((0.5, "total", qty, up, round(qty * up, 2)))
    if qty > 0:
        up_new = tot / qty
        if up_new > 0:
            candidates.append((0.6, "unit_price", qty, round(up_new, 2), tot))
    if not candidates:
        return qty, round(up, 2), round(tot, 2), reconciled
    candidates.sort(key=lambda c: c[0])
    _, _which, q_out, up_out, tot_out = candidates[0]
    reconciled = True
    return q_out, round(up_out, 2), round(tot_out, 2), reconciled


def _sanitize_str(x) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    return s or None


def _sanity_check_date(iso_date: Optional[str]) -> Optional[str]:
    """Guard against Claude emitting an implausible bill date. If the parsed
    date is more than ~13 months in the future OR older than 20 years,
    treat as unreadable and return None. Also normalizes to YYYY-MM-DD.

    This is the last line of defence after the prompt rules — Claude
    occasionally misreads a smudged digit (e.g. "06" → "08" for June/August).
    We'd rather show "date unknown" than a lie.
    """
    if not iso_date:
        return None
    try:
        s = str(iso_date).strip()
        # Only accept YYYY-MM-DD (10 chars). Anything else is suspect.
        if len(s) != 10 or s[4] != "-" or s[7] != "-":
            return None
        y, m, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
        parsed = _dt.date(y, m, d)
    except Exception:
        return None
    today = _dt.date.today()
    # ~13 months in future (allow post-dated cheque scenarios up to ~1 yr)
    if (parsed - today).days > 400:
        return None
    # 20 years back — anything older is almost certainly a smudged year
    if (today - parsed).days > 20 * 365:
        return None
    return parsed.isoformat()


def normalize_bill(raw: dict) -> dict:
    """Coerce numeric fields, drop obviously bad items, ensure item totals, and
    reconcile line items whose qty/unit_price/total don't agree."""
    out = {
        "vendor": (raw.get("vendor") or None),
        "bill_date": _sanity_check_date(raw.get("bill_date")),
        "invoice_number": (raw.get("invoice_number") or None),
        "currency": (raw.get("currency") or None),
        "subtotal": _sanitize_number(raw.get("subtotal")),
        "tax": _sanitize_number(raw.get("tax")),
        "total": _sanitize_number(raw.get("total")),
        # Sprint C+: bill-wide global discount (Style B). See extraction prompt.
        "global_discount_percent": _sanitize_number(raw.get("global_discount_percent")),
        "global_discount_amount": _sanitize_number(raw.get("global_discount_amount")),
        # GST fields (all optional, may be None)
        "gstin": _sanitize_str(raw.get("gstin")),
        "place_of_supply": _sanitize_str(raw.get("place_of_supply")),
        "taxable_amount": _sanitize_number(raw.get("taxable_amount")),
        "cgst_amount": _sanitize_number(raw.get("cgst_amount")),
        "sgst_amount": _sanitize_number(raw.get("sgst_amount")),
        "igst_amount": _sanitize_number(raw.get("igst_amount")),
        # Vendor + buyer metadata (2026-01 — needed for CA exports, 26AS
        # reconciliation, and WhatsApp vendor contact). All best-effort; may
        # be None. Backend also derives vendor_pan from the vendor's GSTIN
        # (chars 3–12) if the OCR didn't spot a standalone PAN row.
        "vendor_address": _sanitize_str(raw.get("vendor_address")),
        "vendor_pan": _sanitize_str(raw.get("vendor_pan")),
        "vendor_phone": _sanitize_str(raw.get("vendor_phone")),
        "vendor_email": _sanitize_str(raw.get("vendor_email")),
        "buyer_gstin": _sanitize_str(raw.get("buyer_gstin")),
        "buyer_name": _sanitize_str(raw.get("buyer_name")),
        "items": [],
    }
    for it in raw.get("items", []) or []:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        qty = _sanitize_number(it.get("quantity"))
        unit_price = _sanitize_number(it.get("unit_price"))
        total = _sanitize_number(it.get("total"))
        disc_pct = _sanitize_number(it.get("discount_percent"))
        disc_amt = _sanitize_number(it.get("discount_amount"))
        has_discount = bool((disc_pct and disc_pct > 0) or (disc_amt and disc_amt > 0))

        qty, unit_price, total, reconciled = _reconcile_line_item(
            name, qty, unit_price, total, has_discount=(has_discount or bool(out.get("global_discount_percent") or out.get("global_discount_amount"))),
        )

        if unit_price is None:
            continue

        item_out = {
            "name": name,
            "quantity": qty,
            "unit_price": round(float(unit_price), 2),
            "total": round(float(total), 2) if total is not None else None,
        }
        if reconciled:
            item_out["reconciled"] = True
        # Preserve discount fields end-to-end for effective_unit_price computation.
        if disc_pct is not None and disc_pct > 0:
            item_out["discount_percent"] = round(float(disc_pct), 2)
        if disc_amt is not None and disc_amt > 0:
            item_out["discount_amount"] = round(float(disc_amt), 2)
        # Optional per-item GST fields (best-effort)
        hsn = _sanitize_str(it.get("hsn"))
        if hsn:
            item_out["hsn"] = hsn
        tax_rate = _sanitize_number(it.get("tax_rate"))
        if tax_rate is not None:
            item_out["tax_rate"] = round(float(tax_rate), 2)
        # Sprint 2026-07 (updated): unit_price now carries the wholesale
        # "Basic Rate" per the user's requirement (it's what the buyer pays,
        # not the MRP retail sticker). We ALSO capture the MRP separately
        # for retail price-change alerts and reporting.
        #   • Accept `mrp` (new prompt) OR `basic_rate` (legacy prompt)
        #     as the MRP field to be forward+backward compatible with
        #     any saved bills.
        mrp = _sanitize_number(it.get("mrp"))
        if mrp is None:
            # legacy field name used before this sprint — historically some
            # models returned "basic_rate" but it actually held the MRP
            # value on retail bills. Fall through so at least we capture it
            # for reference.
            mrp = _sanitize_number(it.get("basic_rate"))
        if mrp is not None and mrp > 0:
            item_out["mrp"] = round(float(mrp), 2)
        taxable_value = _sanitize_number(it.get("taxable_value"))
        if taxable_value is not None:
            item_out["taxable_value"] = round(float(taxable_value), 2)
        out["items"].append(item_out)

    # ------------------------------------------------------------------
    # Sprint C+ safety net: if OCR extracted NEITHER per-line NOR global
    # discount, but the printed total is materially LOWER than the sum of
    # (qty × unit_price) + implied tax, back-derive an implied global
    # discount %. This catches Claude misses on distributor bills with
    # subtle discount lines. Only triggers when the implied disc is
    # 2%–40% (below 2% is rounding noise; above 40% is almost certainly a
    # mis-extracted total we should NOT act on).
    # ------------------------------------------------------------------
    have_line_disc = any(
        (i.get("discount_percent") or 0) > 0 or (i.get("discount_amount") or 0) > 0
        for i in out["items"]
    )
    have_global_disc = bool(
        (out.get("global_discount_percent") or 0) > 0
        or (out.get("global_discount_amount") or 0) > 0
    )
    if not have_line_disc and not have_global_disc and out.get("items"):
        try:
            gross = 0.0
            for i in out["items"]:
                gross += float(i.get("quantity") or 0) * float(i.get("unit_price") or 0)
            printed_total = _sanitize_number(out.get("total"))
            # Best-guess tax rate for the bill (fallback 0 = no tax)
            bill_tax = 0.0
            for i in out["items"]:
                if i.get("tax_rate"):
                    bill_tax = float(i["tax_rate"])
                    break
            if bill_tax == 0.0 and _sanitize_number(raw.get("tax_rate")):
                bill_tax = float(raw.get("tax_rate"))
            if gross > 0 and printed_total and printed_total > 0:
                expected_no_disc = gross * (1 + bill_tax / 100.0)
                shortfall = expected_no_disc - printed_total
                if shortfall > 0:
                    implied_disc_pct = (shortfall / expected_no_disc) * 100.0
                    if 2.0 <= implied_disc_pct <= 40.0:
                        out["global_discount_percent"] = round(implied_disc_pct, 2)
                        out["global_discount_inferred"] = True
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sprint C+ integrity checks: catch OCR failures and flag the bill
    # with `needs_review=True` so the frontend forces the user into the
    # verify banner. We do NOT drop suspect data (users may still want
    # to fix it inline) — we only flag.
    # ------------------------------------------------------------------
    review_reasons: list[str] = []
    items = out["items"]
    # (1) Duplicate item names — Claude sometimes emits the same row twice
    # when line-wrapping confuses it. Suggests a merge/duplication issue.
    seen_names = {}
    for i in items:
        nm = (i.get("name") or "").strip().lower()
        if not nm:
            continue
        seen_names[nm] = seen_names.get(nm, 0) + 1
    if any(v > 1 for v in seen_names.values()):
        review_reasons.append("duplicate-item-names")

    # (2) Suspicious cross-item name overlap: an item name that's a
    # substring of a previous one usually means row-wrap corruption
    # ("B PACK NUTTY BLGN" + "B PACK NUTTY BLGN CHOCOLATE 1X1 MUDSLIDE ...").
    lowered = [((i.get("name") or "").strip().lower(), idx) for idx, i in enumerate(items)]
    for a, ai in lowered:
        for b, bi in lowered:
            if ai == bi or not a or not b:
                continue
            # Overlap if the shorter is a prefix of the longer AND
            # length difference is smaller than the shorter (i.e. one
            # got extended by a few tokens rather than being a genuinely
            # different item).
            if len(a) < len(b) and b.startswith(a) and (len(b) - len(a)) < max(6, len(a) // 2):
                review_reasons.append("suspected-name-wrap-corruption")
                break
        else:
            continue
        break

    # (3) Sum(qty × unit_price) vs printed subtotal — >8% mismatch is suspect
    try:
        gross = sum(float(i.get("quantity") or 0) * float(i.get("unit_price") or 0) for i in items)
        subtotal = _sanitize_number(out.get("subtotal"))
        if subtotal and subtotal > 0 and gross > 0:
            ratio = abs(gross - subtotal) / subtotal
            # Accept up to 8% (rounding + tax quirks); flag beyond.
            if ratio > 0.08 and not (out.get("global_discount_percent") or 0):
                review_reasons.append("subtotal-mismatch")
    except Exception:
        pass

    # (3b) Sprint 2026-07 — CRITICAL for distributor / wholesale bills.
    # If ``sum(items.total)`` is materially LESS than the printed grand total,
    # the extractor almost certainly picked the WRONG amount column (a common
    # failure on Vadilal / Amul / Havmor / HUL-style multi-column bills where
    # PremDisc / SchDisc / Net Rate / Taxable Amt all look like "amount" cells).
    # Flag hard so the user sees a specific, actionable review reason and the
    # bill gets routed to the review-queue immediately.
    try:
        bill_total = _sanitize_number(out.get("total"))
        items_total_sum = sum(float(i.get("total") or 0) for i in out["items"])
        if bill_total and bill_total > 0 and items_total_sum > 0:
            ratio_it = items_total_sum / bill_total
            # <70% of grand total → obvious wrong-column pick.
            if ratio_it < 0.70:
                review_reasons.append("items-total-under-billed")
            # >130% → also broken (picked the ex-GST + GST double-count etc).
            elif ratio_it > 1.30:
                review_reasons.append("items-total-over-billed")
    except Exception:
        pass

    if review_reasons:
        out["needs_review"] = True
        out["review_reasons"] = review_reasons

    # ------------------------------------------------------------------
    # Sprint 2026-07: per-line math sanity + human-readable reasons.
    # Attach a `review_reason` on each line item whose qty × unit_price
    # deviates from total by >15% (accounting for discount + tax) OR whose
    # quantities/prices look absurd. The UI already renders these next to
    # each line so the user knows exactly which numbers to fix.
    # ------------------------------------------------------------------
    for it in out["items"]:
        try:
            q = float(it.get("quantity") or 0)
            up = float(it.get("unit_price") or 0)
            ln_total = float(it.get("total") or 0)
            reasons_line: list[str] = []
            # Non-positive values — always suspect.
            if q <= 0:
                reasons_line.append("quantity-zero")
            if up <= 0 and ln_total <= 0:
                reasons_line.append("price-and-total-zero")
            # Fractional quantity on a bill printed with integer-style pack
            # sizes (e.g. "1*1 5000ML" or "12x1 100ML") is almost always OCR
            # picking up a slash/dot from an adjacent column.
            if q > 0 and abs(q - round(q)) > 1e-3:
                # Only flag if the item looks like a discrete-unit item —
                # skip for KG / LTR / GM items where fractional qty is normal.
                nm = (it.get("name") or "").upper()
                if not any(u in nm for u in (" KG", " GM", " LTR", " ML", " LITER", " GRM", "/KG", "/LTR")):
                    reasons_line.append("fractional-quantity")
            # Absurd unit price: unit_price >> line total (would mean a
            # negative discount larger than the bill) OR line total >> qty*up
            # by more than 40% without a discount column filled.
            if q > 0 and up > 0 and ln_total > 0:
                expected = q * up
                if expected > 0:
                    delta = abs(expected - ln_total) / max(expected, ln_total)
                    has_disc = bool(it.get("discount_percent") or it.get("discount_amount"))
                    if delta > 0.40 and not has_disc:
                        reasons_line.append("line-math-mismatch")
                    # Very small qty but very large unit_price relative to line
                    # total — suggests up and total got swapped by OCR.
                    if q < 1 and up > (ln_total * 2):
                        reasons_line.append("unit-price-likely-swapped")
            if reasons_line:
                it["review_reason"] = reasons_line[0]     # primary reason (UI-friendly)
                it["review_reasons"] = reasons_line       # full list
        except Exception:
            pass
    # Bubble up to the bill flag if any line was flagged.
    if any(it.get("review_reason") for it in out["items"]):
        out["needs_review"] = True
        existing = list(out.get("review_reasons") or [])
        if "line-math-issues" not in existing:
            existing.append("line-math-issues")
        out["review_reasons"] = existing

    # Vendor PAN derivation (2026-01): if OCR didn't spot a standalone PAN
    # row but we do have a valid-looking GSTIN, extract the PAN embedded at
    # characters 3–12 (positions 2..11, inclusive) of the GSTIN. Same trick
    # for the buyer's GSTIN → derives buyer_pan. Only accept if it matches
    # the 10-char alphanumeric shape of a valid Indian PAN
    # (5 letters + 4 digits + 1 letter).
    import re as _re_pan
    _PAN_RE = _re_pan.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

    def _pan_from_gstin(g: str | None) -> str | None:
        if not g:
            return None
        s = str(g).strip().upper().replace(" ", "")
        if len(s) < 12:
            return None
        candidate = s[2:12]
        return candidate if _PAN_RE.match(candidate) else None

    # Only fill the vendor_pan if the OCR value isn't already a valid PAN.
    _vp_raw = (out.get("vendor_pan") or "").strip().upper() if out.get("vendor_pan") else ""
    if not (_vp_raw and _PAN_RE.match(_vp_raw)):
        derived = _pan_from_gstin(out.get("gstin"))
        if derived:
            out["vendor_pan"] = derived
            out["vendor_pan_derived_from_gstin"] = True
    else:
        out["vendor_pan"] = _vp_raw

    # Derive buyer_pan the same way (only from buyer_gstin — never from the
    # vendor's GSTIN, since that would falsely claim the seller is the buyer).
    if out.get("buyer_gstin"):
        b_derived = _pan_from_gstin(out.get("buyer_gstin"))
        if b_derived:
            out["buyer_pan"] = b_derived

    # ------------------------------------------------------------------
    # GST reconstruction fallback (Layer 2 of tax-extraction fix)
    #
    # If the OCR extracted subtotal + total but missed all GST amounts,
    # AND the (total - subtotal) gap is significant AND matches a common
    # Indian GST rate (5/12/18/28%) within tolerance, we reconstruct the
    # tax fields to prevent the frontend from mis-labelling the gap as a
    # huge round-off. Flag with `tax_reconstructed=true` so the UI can
    # show a "please verify" hint.
    # ------------------------------------------------------------------
    _reconstruct_missing_gst(out)

    return out


def _reconstruct_missing_gst(out: dict) -> None:
    """Best-effort recovery when Claude missed the CGST/SGST/IGST lines.

    Mutates `out` in place. Only runs when:
      • subtotal AND total are both present
      • all of cgst/sgst/igst are null-or-zero
      • total - subtotal > ₹5 (i.e. gap is too large to be genuine round-off)
      • the effective rate = gap / subtotal matches a known GST rate
        within ±0.6% tolerance.
    """
    sub = out.get("subtotal")
    total = out.get("total")
    if sub is None or total is None:
        return
    try:
        sub = float(sub)
        total = float(total)
    except Exception:
        return
    if sub <= 0:
        return

    existing_tax = float(out.get("cgst_amount") or 0) + float(out.get("sgst_amount") or 0) + float(out.get("igst_amount") or 0)
    gap = total - sub
    # Small gap → genuine round-off. Leave alone.
    if abs(gap) <= 5.0:
        return
    # Negative gap → likely global discount, not tax. Leave alone.
    if gap < 0:
        return

    # If existing_tax already covers ≥ 90% of the gap, OCR read enough of
    # the tax lines — do not touch. But if there IS some tax extracted yet
    # the gap is still much larger (typical "only CGST leg extracted"
    # failure mode), fall through and reconstruct from the FULL gap.
    if existing_tax > 0.5 and existing_tax >= (gap * 0.90):
        return

    effective_rate_pct = (gap / sub) * 100.0

    # Indian GST rates we recognise. Standard slabs plus common HALF-rates
    # (when only one of CGST/SGST is printed and the other is missing) and
    # a few compound distributor rates. Tolerance widened to 1.0% because
    # printed bills routinely round taxable amounts before applying %.
    #
    # For each rate we record how we should split it: intra-state (cgst+sgst)
    # vs inter-state (igst). "half"-tagged rates mean OCR probably grabbed
    # one leg of a CGST/SGST pair; we should reconstruct BOTH legs to get to
    # the full standard rate.
    GST_RATES = [
        # (rate_percent, split_kind)
        (0.25, "split"), (0.5, "split"), (1.0, "split"),   # gems, precious metals
        (3.0, "split"),                                    # gold jewellery
        (5.0, "split"),
        (12.0, "split"),
        (18.0, "split"),
        (28.0, "split"),
    ]
    tolerance = 1.0
    matched_rate = None
    for r, _kind in GST_RATES:
        if abs(effective_rate_pct - r) <= tolerance:
            matched_rate = r
            break

    if matched_rate is None:
        # No clean rate match — leave for human review, but at least mark it
        # so the frontend can show a warning instead of falsely labelling
        # the gap as round-off.
        out["tax_reconstruction_attempted"] = True
        out["tax_reconstruction_reason"] = (
            f"Unexplained gap of {gap:.2f} between subtotal and total "
            f"(≈{effective_rate_pct:.2f}%) does not match any standard GST rate."
        )
        return

    # Decide the split. Default = intra-state CGST + SGST (most common for
    # Indian SMBs), but if we can tell the bill is inter-state (vendor and
    # buyer GSTINs are in DIFFERENT state codes), populate IGST instead.
    # ------------------------------------------------------------------
    def _state_code_of(g):
        if not g or not isinstance(g, str) or len(g) < 2:
            return None
        head = g[:2]
        return head if head.isdigit() else None

    v_state = _state_code_of(out.get("gstin"))
    b_state = _state_code_of(out.get("buyer_gstin"))
    is_interstate = bool(v_state and b_state and v_state != b_state)

    if is_interstate:
        out["cgst_amount"] = None
        out["sgst_amount"] = None
        out["igst_amount"] = round(gap, 2)
    else:
        half = round(gap / 2.0, 2)
        # Preserve exact total by putting any 1-paise rounding delta into SGST
        out["cgst_amount"] = half
        out["sgst_amount"] = round(gap - half, 2)
        out["igst_amount"] = None
    out["taxable_amount"] = round(sub, 2)
    out["tax"] = round(gap, 2)
    out["tax_reconstructed"] = True
    out["tax_reconstruction_rate"] = matched_rate
    out["tax_reconstruction_reason"] = (
        f"GST lines were not fully visible in OCR. Reconstructed as {matched_rate}% "
        f"GST ({'IGST inter-state' if is_interstate else 'CGST+SGST intra-state'}) "
        f"from subtotal-to-total gap. Please verify against original bill."
    )


def pdf_to_page_pngs(pdf_bytes: bytes, max_pages: int = 4, scale: float = 2.0) -> list[bytes]:
    """Convert PDF pages to PNG bytes (returns list of PNG bytes for each page)."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    n = min(len(pdf), max_pages)
    pngs: list[bytes] = []
    for i in range(n):
        pil = pdf[i].render(scale=scale).to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        pngs.append(buf.getvalue())
    return pngs


def image_bytes_to_normalized_png(img_bytes: bytes, max_side: int = 1800) -> bytes:
    """Open any supported image, transcode to PNG, downscale to keep payload sane."""
    im = Image.open(io.BytesIO(img_bytes))
    if getattr(im, "is_animated", False):
        im.seek(0)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    w, h = im.size
    m = max(w, h)
    if m > max_side:
        ratio = max_side / m
        im = im.resize((int(w * ratio), int(h * ratio)))
    buf = io.BytesIO()
    if im.mode == "RGBA":
        im = im.convert("RGB")
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_thumbnail_data_uri(png_bytes: bytes, max_side: int = 640) -> str:
    im = Image.open(io.BytesIO(png_bytes))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    w, h = im.size
    m = max(w, h)
    if m > max_side:
        ratio = max_side / m
        im = im.resize((int(w * ratio), int(h * ratio)))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


async def extract_bill_from_png_bytes(png_bytes: bytes, model: str = "gemini-3.5-flash-lite") -> dict:
    """Call the configured vision model with a single PNG page and return normalized bill JSON.

    Sprint 2026-08 · migrated from Claude Sonnet 5 → Gemini 3.5 Flash
    Lite (direct Google billing, not Emergent).  Retries once on parse
    failure with a fresh session — the SDK also retries 429/5xx under
    the hood, so we don't burn a manual attempt on transient errors.
    """
    if not EMERGENT_LLM_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured in backend/.env")
    b64 = base64.b64encode(png_bytes).decode()
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            chat = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=f"bill-extract-{uuid.uuid4()}",
                system_message=EXTRACTION_SYSTEM,
            ).with_model(_provider_for(model), model)
            msg = UserMessage(
                text=("Extract the structured JSON for this bill following the schema strictly. "
                      "Before you respond, silently verify each line item: quantity * unit_price should "
                      "approximately equal total. Fix any OCR misreads (especially of the quantity column) "
                      "so the math is consistent."),
                file_contents=[ImageContent(image_base64=b64)],
            )
            text = await chat.send_message(msg)
            if not isinstance(text, str):
                text = str(text)
            raw = parse_json_relaxed(text)
            return normalize_bill(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            continue
        except Exception:
            # Non-parse errors: don't burn retries, surface immediately
            raise
    raise RuntimeError(f"Failed to parse LLM response after retries: {last_err}")


# ------------------------------------------------------------------
# Sprint 2026-07: Smart-retry logic for tricky distributor bills.
#
# Motivation: on multi-column wholesale bills (Vadilal, Amul, Shiv Gauri
# distributor invoices etc.) the OCR sometimes picks the wrong amount
# column, producing line items that sum to way less (or way more) than
# the printed grand total. When that happens we transparently redo the
# extraction with a fresh session. The user sees an accurate extraction
# on the first try — no manual "rescan" needed.
# Post-2026-08 (Claude → Gemini swap): both attempts use Gemini 3.5
# Flash Lite (unified model); the retry gives Gemini a second look at
# the pixels with a fresh context window, which resolves most
# wrong-column picks — same reasoning as the previous Claude retry.
# ------------------------------------------------------------------
_WRONG_COLUMN_FLAGS: frozenset[str] = frozenset({
    "items-total-under-billed",
    "items-total-over-billed",
    "subtotal-mismatch",
})


def _looks_like_wrong_column_pick(bill: dict) -> bool:
    """Return True if the initial FLASH extraction shows the classic
    wrong-column pick pattern that warrants a PRO retry.
    """
    reasons = set(bill.get("review_reasons") or [])
    if reasons & _WRONG_COLUMN_FLAGS:
        return True
    # Additional heuristic: any single line item with a >30% math mismatch
    # (reconciled=True but reason=line-math-mismatch) also suggests the
    # column pick was wrong. Bail unless we have a clear reason though.
    for it in bill.get("items") or []:
        if it.get("review_reason") == "line-math-mismatch":
            return True
    return False


async def extract_bill_with_smart_retry(
    png_bytes: bytes,
    initial_model: str = "gemini-3.5-flash-lite",
    upgrade_model: str = "gemini-3.5-flash-lite",
) -> dict:
    """First-attempt with ``initial_model``. If the result looks like a
    wrong-column pick (items don't sum to grand total, or a line has a
    large math mismatch), retry ONCE with ``upgrade_model``. Whichever
    result has a better items-sum-to-bill-total ratio wins. This is
    transparent to the caller.

    Returns the winning bill dict, with ``_extracted_by`` set to the
    model that produced it and ``_smart_retry_used`` = True when the
    retry fired.
    """
    first = await extract_bill_from_png_bytes(png_bytes, model=initial_model)
    first["_extracted_by"] = initial_model
    if not _looks_like_wrong_column_pick(first):
        return first
    try:
        second = await extract_bill_from_png_bytes(png_bytes, model=upgrade_model)
        second["_extracted_by"] = upgrade_model
        second["_smart_retry_used"] = True
    except Exception:
        # If the retry fails for any reason, fall back to the first
        # result — never worse than what the user would have got before.
        return first

    # Pick the extraction whose items sum closer to the grand total.
    def _score(b: dict) -> float:
        try:
            bt = float(b.get("total") or 0)
            it = sum(float(i.get("total") or 0) for i in (b.get("items") or []))
            if bt <= 0 or it <= 0:
                return 1.0  # worst
            return abs(bt - it) / bt
        except Exception:
            return 1.0

    return second if _score(second) <= _score(first) else first



def merge_bills(bills: list[dict]) -> dict:
    """Merge multiple page-extractions into a single bill (use first non-null header fields)."""
    if not bills:
        return {"vendor": None, "bill_date": None, "invoice_number": None, "currency": None,
                "subtotal": None, "tax": None, "total": None, "items": [],
                "gstin": None, "place_of_supply": None, "taxable_amount": None,
                "cgst_amount": None, "sgst_amount": None, "igst_amount": None}
    if len(bills) == 1:
        return bills[0]
    out = {
        "vendor": next((b.get("vendor") for b in bills if b.get("vendor")), None),
        "bill_date": next((b.get("bill_date") for b in bills if b.get("bill_date")), None),
        "invoice_number": next((b.get("invoice_number") for b in bills if b.get("invoice_number")), None),
        "currency": next((b.get("currency") for b in bills if b.get("currency")), None),
        "subtotal": next((b.get("subtotal") for b in bills if b.get("subtotal") is not None), None),
        "tax": next((b.get("tax") for b in bills if b.get("tax") is not None), None),
        "total": next((b.get("total") for b in bills if b.get("total") is not None), None),
        "gstin": next((b.get("gstin") for b in bills if b.get("gstin")), None),
        "place_of_supply": next((b.get("place_of_supply") for b in bills if b.get("place_of_supply")), None),
        "taxable_amount": next((b.get("taxable_amount") for b in bills if b.get("taxable_amount") is not None), None),
        "cgst_amount": next((b.get("cgst_amount") for b in bills if b.get("cgst_amount") is not None), None),
        "sgst_amount": next((b.get("sgst_amount") for b in bills if b.get("sgst_amount") is not None), None),
        "igst_amount": next((b.get("igst_amount") for b in bills if b.get("igst_amount") is not None), None),
        "items": [it for b in bills for it in (b.get("items") or [])],
    }
    return out


def get_first_page_png(file_bytes: bytes, content_type: str, filename: str) -> bytes:
    """Return the first-page PNG bytes for any supported document.

    Used by the classifier (which only needs the first page) so that we
    don't waste tokens/latency on multi-page PDFs before deciding whether
    to accept the upload at all.
    """
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    is_pdf = "pdf" in ct or name.endswith(".pdf")
    if is_pdf:
        pngs = pdf_to_page_pngs(file_bytes, max_pages=1)
        if not pngs:
            raise RuntimeError("Empty PDF or unreadable")
        return pngs[0]
    return image_bytes_to_normalized_png(file_bytes)


async def extract_bill_from_file(
    file_bytes: bytes, content_type: str, filename: str,
    model: str = "gemini-3.5-flash-lite",
) -> tuple[dict, str]:
    """Handle image or PDF -> return (normalized_bill_dict, thumbnail_data_uri).

    Sprint 2026-08: Claude Sonnet 5 is the default OCR engine (user verified
    it reads distributor bills to the paisa across 20+ real samples). The
    chosen model is echoed back into the returned bill dict as
    `_extracted_by` so callers can persist it.

    For multi-page PDFs this merges all pages into ONE bill (legacy behavior).
    Callers who want split-by-invoice behavior should use `extract_pdf_pages`
    followed by `detect_bill_boundaries`.
    """
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    is_pdf = "pdf" in ct or name.endswith(".pdf")
    if is_pdf:
        page_pngs = pdf_to_page_pngs(file_bytes, max_pages=MAX_PDF_PAGES)
        if not page_pngs:
            raise RuntimeError("Empty PDF or unreadable")
        bills = await _gather_page_extractions(page_pngs, model=model)
        merged = merge_bills(list(bills))
        merged["_extracted_by"] = model
        thumb = make_thumbnail_data_uri(page_pngs[0])
        return merged, thumb
    # Image path — Claude Sonnet 5 is accurate enough that a retry
    # ladder is no longer needed. Single call, correct result.
    png = image_bytes_to_normalized_png(file_bytes)
    bill = await extract_bill_from_png_bytes(png, model=model)
    bill["_extracted_by"] = model
    thumb = make_thumbnail_data_uri(png)
    return bill, thumb
    return bill, thumb


# Safety upper bound on PDF page count. Prevents runaway Claude spend.
MAX_PDF_PAGES: int = 25
# Bound concurrent Claude calls per PDF upload to keep the provider happy.
_PAGE_EXTRACT_CONCURRENCY: int = 4


async def _gather_page_extractions(
    page_pngs: list[bytes],
    model: str,
    *,
    per_page_timeout: float = 55.0,
    on_page_done: Optional[Callable[[int, bool, Optional[str]], None]] = None,
) -> list[dict]:
    """Extract every page in parallel, bounded by _PAGE_EXTRACT_CONCURRENCY.

    Sprint 2026-07 hotfix (multi-page PDF stuck at "page 0 of 2"):
      Previously this was a plain ``asyncio.gather`` — if a single page's
      Claude call hung (network blip, model overload), the entire PDF
      upload hung with it, and the frontend "Extracting page 0 of N…"
      modal stayed stuck forever. New behaviour:

        • Each page has its own hard timeout (``per_page_timeout``) so
          one stuck page can't stall the whole PDF.
        • Failed / timed-out pages return an EMPTY normalized bill dict
          (no items, no total) instead of raising — the caller keeps the
          successful pages and just notes the failure. Partial results
          are strictly better than a total wipe.
        • Callers can pass ``on_page_done`` to receive live progress
          updates (page_index, ok, error) after each page finishes.
          ``_run_preview_extraction`` uses this to update the shared
          preview cache so the UI shows real per-page progress.
    """
    sem = asyncio.Semaphore(_PAGE_EXTRACT_CONCURRENCY)

    async def _one(idx: int, png: bytes) -> dict:
        try:
            async with sem:
                return await asyncio.wait_for(
                    extract_bill_from_png_bytes(png, model=model),
                    timeout=per_page_timeout,
                )
        except asyncio.TimeoutError:
            if on_page_done is not None:
                try:
                    on_page_done(idx, False, f"timeout after {per_page_timeout:.0f}s")
                except Exception:
                    pass
            return normalize_bill({"items": [], "_extraction_failed": True,
                                   "_extraction_error": f"timeout after {per_page_timeout:.0f}s"})
        except Exception as e:  # noqa: BLE001 — surface via return dict
            if on_page_done is not None:
                try:
                    on_page_done(idx, False, str(e))
                except Exception:
                    pass
            return normalize_bill({"items": [], "_extraction_failed": True,
                                   "_extraction_error": str(e)})
        finally:
            # Fires on success too (the two except branches above already
            # notified for failures, but this outer catch also handles the
            # happy path).
            pass

    async def _one_with_progress(idx: int, png: bytes) -> dict:
        result = await _one(idx, png)
        if not result.get("_extraction_failed") and on_page_done is not None:
            try:
                on_page_done(idx, True, None)
            except Exception:
                pass
        return result

    return await asyncio.gather(*[_one_with_progress(i, p) for i, p in enumerate(page_pngs)])


async def extract_pdf_pages(
    pdf_bytes: bytes,
    model: str = "gemini-3.5-flash-lite",
    *,
    on_page_done: Optional[Callable[[int, bool, Optional[str]], None]] = None,
    on_pages_rendered: Optional[Callable[[int], None]] = None,
) -> tuple[list[dict], list[bytes], list[str]]:
    """Extract each PDF page independently WITHOUT merging.

    Returns:
        (pages, page_pngs, thumbnails) where each list has one entry per page
        (up to MAX_PDF_PAGES). `pages[i]` is the normalized extraction dict for
        page i; `page_pngs[i]` is the rendered PNG bytes; `thumbnails[i]` is a
        data-URI thumbnail suitable for the UI.
    """
    page_pngs = pdf_to_page_pngs(pdf_bytes, max_pages=MAX_PDF_PAGES)
    if not page_pngs:
        raise RuntimeError("Empty PDF or unreadable")
    # Let the caller know how many pages we actually rasterised — the
    # preview cache uses this to initialise `progress.total` immediately
    # (previously the UI showed "0 of 2" until every page had extracted).
    if on_pages_rendered is not None:
        try:
            on_pages_rendered(len(page_pngs))
        except Exception:
            pass
    pages = await _gather_page_extractions(
        page_pngs, model=model, on_page_done=on_page_done,
    )
    for p in pages:
        p["_extracted_by"] = model
    thumbnails = [make_thumbnail_data_uri(png) for png in page_pngs]
    return pages, page_pngs, thumbnails


def _norm(x: Optional[str]) -> str:
    return (x or "").strip().lower()


def _amounts_close(a: Optional[float], b: Optional[float], tol: float = 0.5) -> bool:
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= max(tol, abs(float(a)) * 0.01)
    except (TypeError, ValueError):
        return False


def detect_bill_boundaries(pages: list[dict]) -> list[dict]:
    """Group consecutive PDF pages into candidate bills.

    Rules:
      1. A new bill starts when `(vendor_key_or_name, invoice_number, bill_date)`
         differs from the previous page.
      2. If the same tuple continues, the page is appended to the current bill
         (items concatenated). This handles legitimate multi-page invoices.
      3. Duplicate-copy detection: if a page has the exact same
         `(vendor, invoice_number, total)` as the previous page's *first* page,
         it is treated as a duplicate copy (recipient / transporter). Its items
         are NOT re-added \u2014 but the page index is still recorded.

    Returns list of candidate bill dicts with:
        - `pages`: [int, ...] 1-indexed page numbers included
        - `duplicate_pages`: [int, ...] pages considered duplicates
        - `bill`: merged bill dict (vendor/date/invoice#/items/etc.)
    """
    if not pages:
        return []

    groups: list[dict] = []
    current: Optional[dict] = None

    def _key(p: dict) -> tuple:
        vk = _norm(p.get("vendor"))
        inv = _norm(p.get("invoice_number"))
        dt = _norm(p.get("bill_date"))
        return (vk, inv, dt)

    for idx, page in enumerate(pages):
        page_num = idx + 1  # 1-indexed for humans
        k = _key(page)

        # empty page (all header fields missing) — attach to current group if any
        if not any(k):
            if current is not None:
                current["pages"].append(page_num)
                current["bill"] = merge_bills([current["bill"], page])
                continue
            # otherwise start a lone group for it
            current = {"pages": [page_num], "duplicate_pages": [], "bill": page}
            groups.append(current)
            continue

        if current is None:
            current = {"pages": [page_num], "duplicate_pages": [], "bill": page}
            groups.append(current)
            continue

        prev_key = _key(current["bill"])
        # Same tuple → same bill
        if k == prev_key and all(k):
            # Duplicate-copy: same vendor+invoice+total as current first page
            if _amounts_close(page.get("total"), current["bill"].get("total")):
                current["duplicate_pages"].append(page_num)
                # do NOT re-append items
                continue
            # Genuine continuation of a multi-page invoice
            current["pages"].append(page_num)
            current["bill"] = merge_bills([current["bill"], page])
            continue

        # Boundary → new bill
        current = {"pages": [page_num], "duplicate_pages": [], "bill": page}
        groups.append(current)

    return groups


def _provider_for(model: str) -> str:
    """Route model IDs to their provider for LlmChat.with_model().

    Sprint 2026-08 · Claude → Gemini hard swap: every ID (legacy Claude,
    GPT, or new Gemini) routes to Google.  The shim in `gemini_client`
    ignores the provider anyway, but returning "google" here keeps the
    logs honest.
    """
    _ = (model or "")  # accepted for interface parity
    return "google"


ALLOWED_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "claude-haiku-5-5", "claude-sonnet-5-5",   # wise-plus: Claude through the Anthropic API
    # Legacy IDs kept in the allow-list so any historical caller/test that
    # passes them still works — the shim resolves them to Gemini.
    "claude-sonnet-5",
    "gpt-4o",
)
DEFAULT_MODEL: str = "gemini-3.5-flash-lite"


def normalize_item_key(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_vendor_key(vendor: str) -> str:
    return normalize_item_key(vendor or "")
