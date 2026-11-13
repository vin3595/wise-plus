"""Turns a bill read by the AI (Ease My Bill's format, rupees as floats) into a draft Wise purchase:
supplier matched by GSTIN or name, each line matched to a stock item with its unit, money in paise.
Nothing is saved here: the person checks the draft on Stock > Purchases and saves it the normal way."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..core.stock import STANDARD_UNITS

GST_RATES_BP = (0, 500, 1200, 1800, 2800)

# Words on bills that say nothing about which item it is.
_NOISE = {"pcs", "pc", "nos", "no", "box", "pkt", "packet", "pack", "bag", "case", "ctn", "carton", "jar", "tin", "the", "and",
          "of", "x", "kg", "kgs", "gm", "gms", "g", "grm", "ltr", "lt", "l", "ml", "litre", "liter", "ltrs"}

# Unit words printed on bills -> a unit Wise knows.
_UNIT_WORDS = {"kg": "kg", "kgs": "kg", "kilo": "kg", "g": "g", "gm": "g", "gms": "g", "grm": "g", "gram": "g", "grams": "g",
               "l": "l", "lt": "l", "ltr": "l", "ltrs": "l", "litre": "l", "liter": "l", "ml": "ml",
               "pc": "pc", "pcs": "pc", "nos": "pc", "no": "pc", "piece": "pc", "pieces": "pc", "dozen": "dozen", "dz": "dozen"}


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _NOISE and not w.isdigit()]


def name_score(bill_name: str, stock_name: str) -> float:
    """0..1: how sure we are that a bill line is this stock item."""
    a, b = " ".join(_words(bill_name)), " ".join(_words(stock_name))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    wa, wb = set(a.split()), set(b.split())
    overlap = len(wa & wb) / max(1, len(wb))           # how much of the stock item's name is on the bill
    ratio = SequenceMatcher(None, a, b).ratio()
    contained = 0.9 if (f" {b} " in f" {a} " or f" {a} " in f" {b} ") else 0.0
    return round(max(contained, 0.6 * overlap + 0.4 * ratio), 3)


def best_stock_item(bill_name: str, items: list[dict], remembered: dict[str, str]) -> tuple[dict | None, float]:
    """remembered: bill line name (lower) -> stock_item_id, from this supplier's earlier purchases."""
    key = bill_name.strip().lower()
    if key in remembered:
        for it in items:
            if it["id"] == remembered[key]:
                return it, 1.0
    best, score = None, 0.0
    for it in items:
        s = name_score(bill_name, it["name"])
        if s > score:
            best, score = it, s
    return (best, score) if score >= 0.55 else (None, score)


def unit_from_name(name: str) -> str | None:
    """'Amul Butter 500 GM' -> 'g'; 'Milk 1 LTR' -> 'l'. Only the unit word, never the pack size."""
    for w in re.findall(r"[a-z]+", (name or "").lower()):
        if w in _UNIT_WORDS:
            return _UNIT_WORDS[w]
    return None


def pick_unit(item: dict, bill_name: str) -> str:
    """The unit the quantity on the bill is most likely in, among the units this stock item can be bought in."""
    names = [u["name"] for u in (item.get("purchase_units") or [])]
    lower = (bill_name or "").lower()
    for n in names:                       # a pack size named on the bill ("box of 24")
        if n and n.lower() in lower:
            return n
    guess = unit_from_name(bill_name)
    if guess and (guess == item["unit"] or STANDARD_UNITS.get(guess, ("", 0))[0] == item["unit"]):
        return guess
    if names:                             # bills usually count packs
        return names[0]
    return {"g": "kg", "ml": "l", "pc": "pc"}[item["unit"]]


def nearest_gst_bp(rate: float | None) -> int:
    if rate is None:
        return 0
    bp = round(float(rate) * 100)
    return min(GST_RATES_BP, key=lambda r: abs(r - bp))


def to_paise(x) -> int:
    try:
        return max(0, round(float(x) * 100))
    except (TypeError, ValueError):
        return 0


def to_qty_m(x) -> int:
    try:
        return max(0, round(float(x) * 1000))
    except (TypeError, ValueError):
        return 0


def match_supplier(bill: dict, suppliers: list[dict]) -> dict | None:
    g = (bill.get("gstin") or "").strip().upper()
    if g:
        for s in suppliers:
            if (s.get("gstin") or "").strip().upper() == g:
                return {**s, "how": "gstin"}
    name = bill.get("vendor") or ""
    best, score = None, 0.0
    for s in suppliers:
        sc = name_score(name, s["name"])
        if sc > score:
            best, score = s, sc
    return {**best, "how": "name"} if best and score >= 0.7 else None


def build_draft(bill: dict, items: list[dict], suppliers: list[dict], remembered: dict[str, str]) -> dict:
    """bill: normalized output of extraction.normalize_bill. items/suppliers: plain dicts from the database."""
    sup = match_supplier(bill, suppliers)
    lines = []
    for ln in bill.get("items") or []:
        name = (ln.get("name") or "").strip()
        if not name:
            continue
        it, score = best_stock_item(name, items, remembered)
        qty = ln.get("quantity")
        line_total = ln.get("total")
        if line_total is None and qty is not None and ln.get("unit_price") is not None:
            line_total = float(qty) * float(ln["unit_price"])
        lines.append({
            "bill_name": name,
            "qty": qty, "qty_m": to_qty_m(qty),
            "unit_price_paise": to_paise(ln.get("unit_price")),
            "amount_paise": to_paise(line_total),
            "gst_bp": nearest_gst_bp(ln.get("tax_rate")),
            "hsn": ln.get("hsn"),
            "stock_item_id": it["id"] if it else None,
            "stock_item_name": it["name"] if it else None,
            "unit": pick_unit(it, name) if it else None,
            "match": round(score, 2),
            "check": ln.get("review_reason"),
        })
    # One GST rate for the whole bill when lines don't say (common on small bills): spread it from the totals.
    taxable = to_paise(bill.get("taxable_amount") or bill.get("subtotal"))
    tax = to_paise(bill.get("tax")) or sum(to_paise(bill.get(k)) for k in ("cgst_amount", "sgst_amount", "igst_amount"))
    if lines and all(ln["gst_bp"] == 0 for ln in lines) and taxable and tax:
        bp = nearest_gst_bp(tax * 100 / taxable)
        for ln in lines:
            ln["gst_bp"] = bp
    return {
        "vendor": bill.get("vendor"), "gstin": bill.get("gstin"), "vendor_phone": bill.get("vendor_phone"),
        "supplier": {"id": sup["id"], "name": sup["name"], "how": sup["how"]} if sup else None,
        "invoice_no": (bill.get("invoice_number") or "")[:40] or None,
        "invoice_date": bill.get("bill_date"),
        "total_paise": to_paise(bill.get("total")),
        "taxable_paise": taxable or None,
        "tax_paise": tax or None,
        "lines": lines,
        "needs_review": bool(bill.get("needs_review")),
        "review_reasons": list(bill.get("review_reasons") or []),
    }
