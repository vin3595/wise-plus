"""Check a supplier's statement (their ledger of our account) against what's in the POS.

Upload the supplier's ledger (PDF or photo). We read it, then:
  - each of their bills: is it saved in the POS (Stock > Purchases)? matched by bill number, else by amount + date
  - each payment they received from us: is it in our dues ledger? the missing ones can be added in one tap (mode: bank)
  - their opening and closing balance against ours for the same dates
Nothing is written until the person taps "Add" (apply())."""
from __future__ import annotations

import difflib
import io
import json
import re
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CashMovement, Purchase, Supplier
from .models import SupplierEntry

PAISA_SLACK = 100            # ₹1 rounding
BILL_DAYS = 5                # a bill saved a few days off the supplier's date still counts
PAY_DAYS = 7


class StatementError(Exception):
    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


PROMPT = """You are reading a SUPPLIER'S LEDGER / ACCOUNT STATEMENT. The supplier (the business whose name and
address are in the header, e.g. "SHRI TIRUPATI AGENCY") sent it to their customer (the account it's for,
e.g. "Shakewallah"). Read every row and return ONLY JSON:
{
 "supplier": "name of the business that issued the statement (header), not the customer",
 "supplier_gstin": "their GSTIN or null",
 "customer": "whose account this is",
 "period_from": "YYYY-MM-DD or null", "period_to": "YYYY-MM-DD or null",
 "opening": {"amount": number, "customer_owes": true|false} or null,
 "closing": {"amount": number, "customer_owes": true|false} or null,
 "rows": [ {"date": "YYYY-MM-DD", "type": "bill|payment|credit_note|debit_note|other",
            "ref": "voucher / bill number exactly as printed, or null",
            "amount": number, "particulars": "text in the particulars column", "bank": "bank name + account if a payment, else null"} ]
}
Rules:
- In the supplier's books the customer's account is DEBITED for sales/bills and CREDITED for receipts/payments.
  A "Sales"/"Tax Invoice"/"Bill" row is type "bill". A "Receipt"/"Payment received"/bank/cash/UPI row is "payment".
  A "Credit Note"/"Sales Return" (reduces what the customer owes) is "credit_note". A "Debit Note" (adds to it) is "debit_note".
- Do NOT include the opening balance, closing balance or column-total lines in "rows".
- customer_owes is true when the balance is in the customer's debit side (they owe the supplier).
  A line like "Dr Closing Balance" printed in the CREDIT column is the balancing figure: it means the customer owes.
  Decide by arithmetic: closing = opening + bills + debit notes - payments - credit notes.
- Amounts are plain numbers in rupees, no commas. Dates like 9-Oct-26 are 2026-10-09.
- Read all pages. Never invent rows."""


# ------------------------------------------------------------------ reading

def _pdf_text(data: bytes) -> str:
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        out = []
        for i in range(min(len(pdf), 12)):
            out.append(pdf[i].get_textpage().get_text_range())
        return "\n\n".join(out)
    except Exception:
        return ""


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise StatementError("Couldn't read that statement. Try a clearer photo or the PDF.")
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        raise StatementError("Couldn't read that statement. Try again.") from e


async def read_statement(data: bytes, content_type: str, name: str) -> dict:
    """Text PDFs (Tally, Busy, Marg exports) are read as text: cheaper and exact. Photos and scans as images."""
    from ..purchases_ai.gemini import GEMINI_API_KEY, ImageContent, LlmChat, UserMessage
    from ..purchases_ai.extraction import image_bytes_to_normalized_png, pdf_to_page_pngs
    import base64

    is_pdf = content_type == "application/pdf" or name.lower().endswith(".pdf") or data[:4] == b"%PDF"
    text = _pdf_text(data) if is_pdf else ""
    files = []
    if len(re.sub(r"\s", "", text)) < 80:          # scanned PDF or a photo
        text = ""
        pngs = pdf_to_page_pngs(data, max_pages=6) if is_pdf else [image_bytes_to_normalized_png(data, max_side=2000)]
        files = [ImageContent(image_base64=base64.b64encode(p).decode()) for p in pngs]
    if not GEMINI_API_KEY:
        raise StatementError("The bill reader isn't set up on this server", 503)
    chat = LlmChat(api_key=GEMINI_API_KEY, session_id=f"stmt-{uuid.uuid4()}", system_message=PROMPT)
    msg = UserMessage(text=("Here is the statement text:\n\n" + text[:60000]) if text else "Here is the statement.", file_contents=files)
    raw = None
    for _ in range(2):
        try:
            raw = _parse(await chat.send_message(msg))
            break
        except StatementError:
            continue
    if raw is None:
        raise StatementError("Couldn't read that statement. Try a clearer photo or the PDF.")
    return clean(raw)


def _d(x) -> date | None:
    try:
        return date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def _p(x) -> int:
    try:
        return int(round(float(str(x).replace(",", "")) * 100))
    except (TypeError, ValueError):
        return 0


def clean(raw: dict) -> dict:
    rows = []
    for r in raw.get("rows") or []:
        t = str(r.get("type") or "other").lower()
        amt = abs(_p(r.get("amount")))
        if amt == 0 or _d(r.get("date")) is None:
            continue
        rows.append({"date": _d(r.get("date")), "type": t if t in ("bill", "payment", "credit_note", "debit_note") else "other",
                     "ref": (str(r.get("ref")).strip() or None) if r.get("ref") not in (None, "") else None, "amount_paise": amt,
                     "particulars": (r.get("particulars") or "")[:120] or None, "bank": (r.get("bank") or None)})
    rows.sort(key=lambda r: r["date"])

    def bal(b):
        if not isinstance(b, dict) or b.get("amount") in (None, ""):
            return None
        v = abs(_p(b.get("amount")))
        return v if b.get("customer_owes", True) else -v
    out = {"supplier": (raw.get("supplier") or "").strip() or None, "supplier_gstin": raw.get("supplier_gstin") or None,
           "customer": raw.get("customer") or None, "period_from": _d(raw.get("period_from")), "period_to": _d(raw.get("period_to")),
           "opening_paise": bal(raw.get("opening")), "closing_paise": bal(raw.get("closing")), "rows": rows}
    if rows:
        out["period_from"] = out["period_from"] or rows[0]["date"]
        out["period_to"] = out["period_to"] or rows[-1]["date"]
    # Their own arithmetic, to catch a misread: opening + bills - payments should equal closing.
    move = sum(r["amount_paise"] * (1 if r["type"] in ("bill", "debit_note") else -1 if r["type"] in ("payment", "credit_note") else 0) for r in rows)
    out["adds_up"] = (out["opening_paise"] is None or out["closing_paise"] is None
                      or abs((out["opening_paise"] + move) - out["closing_paise"]) <= PAISA_SLACK)
    return out


# ------------------------------------------------------------------ matching

def _key(s: str) -> str:
    s = re.sub(r"\b(m/s|messrs|the|pvt|private|ltd|limited|llp|and|&|co|company)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def find_supplier(db: Session, name: str | None, gstin: str | None) -> tuple[Supplier | None, list[dict]]:
    sups = db.scalars(select(Supplier).where(Supplier.is_active.is_(True))).all()
    if gstin:
        for s in sups:
            if s.gstin and s.gstin.upper() == gstin.upper():
                return s, []
    k = _key(name or "")
    scored = sorted(((difflib.SequenceMatcher(None, k, _key(s.name)).ratio() + (0.3 if k and (k in _key(s.name) or _key(s.name) in k) else 0), s)
                     for s in sups), key=lambda x: -x[0])
    best = scored[0] if scored else None
    others = [{"id": str(s.id), "name": s.name} for _, s in scored[:5]]
    if best and best[0] >= 0.85:
        return best[1], others
    return None, others


def _digits(ref: str | None) -> str:
    return re.sub(r"\D", "", ref or "").lstrip("0")


def _ref_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if re.sub(r"[^a-z0-9]", "", a.lower()) == re.sub(r"[^a-z0-9]", "", b.lower()):
        return True
    da, db_ = _digits(a), _digits(b)
    # "E-1353" vs "1353", "2627/TS000322" vs "322": the number part must agree (and be long enough to mean something)
    return bool(da and db_ and len(min(da, db_, key=len)) >= 3 and (da == db_ or da.endswith(db_) or db_.endswith(da)))


def compare(db: Session, supplier: Supplier, st: dict) -> dict:
    lo = (st["period_from"] or date.min) - timedelta(days=BILL_DAYS)
    hi = (st["period_to"] or date.max) + timedelta(days=BILL_DAYS)
    pos_bills = db.scalars(select(Purchase).where(Purchase.supplier_id == supplier.id, Purchase.status == "posted")).all()
    used: set = set()

    def pick_bill(r):
        for p in pos_bills:                                   # 1. by bill number
            if p.id not in used and _ref_match(r["ref"], p.invoice_no):
                return p, "number"
        for p in pos_bills:                                   # 2. same amount, close date
            if (p.id not in used and abs(p.total_paise - r["amount_paise"]) <= PAISA_SLACK
                    and abs((p.invoice_date - r["date"]).days) <= BILL_DAYS):
                return p, "amount"
        return None, None

    bills = []
    for r in st["rows"]:
        if r["type"] != "bill":
            continue
        p, how = pick_bill(r)
        row = {**r, "in_pos": p is not None}
        if p is not None:
            used.add(p.id)
            row.update(purchase_id=str(p.id), pos_invoice_no=p.invoice_no, pos_date=p.invoice_date, pos_total_paise=p.total_paise,
                       matched_by=how, differs_paise=p.total_paise - r["amount_paise"] if abs(p.total_paise - r["amount_paise"]) > PAISA_SLACK else 0)
        bills.append(row)
    only_pos = [{"purchase_id": str(p.id), "invoice_no": p.invoice_no, "date": p.invoice_date, "total_paise": p.total_paise}
                for p in pos_bills if p.id not in used and lo <= p.invoice_date <= hi]

    # Payments and notes we already have: dues-ledger entries and cash paid from a drawer to this supplier.
    ours = [{"date": e.entry_date, "amount_paise": e.amount_paise, "kind": e.kind, "id": str(e.id)}
            for e in db.scalars(select(SupplierEntry).where(SupplierEntry.supplier_id == supplier.id, SupplierEntry.status == "posted",
                                                           SupplierEntry.kind.in_(("payment", "debit_note", "credit_note"))))]
    from sqlalchemy import func
    ours += [{"date": m.created_at.date(), "amount_paise": m.amount_paise, "kind": "payment", "id": str(m.id)}
             for m in db.scalars(select(CashMovement).where(CashMovement.kind == "out", CashMovement.purpose == "supplier",
                                                            func.lower(func.trim(CashMovement.paid_to)) == supplier.name.strip().lower()))]
    taken: set = set()
    payments = []
    for r in st["rows"]:
        if r["type"] not in ("payment", "credit_note"):
            continue
        want = "payment" if r["type"] == "payment" else "credit_note"
        hit = next((o for o in ours if o["id"] not in taken and (o["kind"] == want or {o["kind"], want} == {"debit_note", "credit_note"}) and abs(o["amount_paise"] - r["amount_paise"]) <= PAISA_SLACK
                    and abs((o["date"] - r["date"]).days) <= PAY_DAYS), None)
        if hit:
            taken.add(hit["id"])
        payments.append({**r, "recorded": hit is not None, "entry_id": hit["id"] if hit else None,
                         "mode": _mode(r) if want == "payment" else None, "kind": want})

    # Balances on the same dates, from our side.
    def ours_on(d: date | None) -> int | None:
        if d is None:
            return None
        from .balances import statement
        rows = [x for x in statement(db, supplier.id, limit=100000)["entries"] if x["date"] <= d]
        return rows[-1]["balance_paise"] if rows else 0
    pf = st["period_from"]
    ours_open = ours_on(pf - timedelta(days=1)) if pf else None
    ours_close = ours_on(st["period_to"])
    has_history = db.scalar(select(func.count()).select_from(SupplierEntry).where(
        SupplierEntry.supplier_id == supplier.id, SupplierEntry.kind == "opening", SupplierEntry.status == "posted")) > 0 or any(
        p.invoice_date < pf for p in pos_bills) if pf else True
    return {
        "supplier": {"id": str(supplier.id), "name": supplier.name},
        "bills": bills, "bills_total": len(bills), "bills_in_pos": sum(1 for b in bills if b["in_pos"]),
        "missing_paise": sum(b["amount_paise"] for b in bills if not b["in_pos"]),
        "only_in_pos": only_pos,
        "payments": payments, "payments_new": sum(1 for p in payments if not p["recorded"]),
        "opening": {"theirs": st["opening_paise"], "ours": ours_open,
                    "suggest": st["opening_paise"] if (st["opening_paise"] and not has_history) else None},
        "closing": {"theirs": st["closing_paise"], "ours": ours_close},
    }


def _mode(r: dict) -> str:
    t = f"{r.get('bank') or ''} {r.get('particulars') or ''}".lower()
    if "cash" in t:
        return "cash"
    if "upi" in t or "gpay" in t or "phonepe" in t or "paytm" in t:
        return "upi"
    if "cheque" in t or "chq" in t:
        return "cheque"
    return "bank"


# ------------------------------------------------------------------ adding what's missing

def apply(db: Session, tenant_id, user_id, supplier: Supplier, payments: list[dict], opening: dict | None) -> list[str]:
    """Add the chosen payments / credit notes (and an opening balance) to the dues ledger. Skips anything already there."""
    made = []
    existing = db.scalars(select(SupplierEntry).where(SupplierEntry.supplier_id == supplier.id, SupplierEntry.status == "posted")).all()
    if opening and not any(e.kind == "opening" for e in existing):
        e = SupplierEntry(tenant_id=tenant_id, supplier_id=supplier.id, entry_date=opening["date"], kind="opening",
                          amount_paise=opening["amount_paise"], note="From the supplier's statement", created_by=user_id)
        db.add(e)
        made.append(e)
    for p in payments:
        kind = p.get("kind") or "payment"
        if any(e.kind == kind and abs(e.amount_paise - p["amount_paise"]) <= PAISA_SLACK and abs((e.entry_date - p["date"]).days) <= PAY_DAYS
               for e in existing + made):
            continue
        ref = (p.get("ref") or "")[:60] or None
        digits = re.sub(r"\D", "", p.get("bank") or "")
        from ..banking.models import BankAccount
        bank = next((a for a in db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True)))
                     if a.last4 and digits.endswith(a.last4)), None) if len(digits) >= 4 else None
        e = SupplierEntry(tenant_id=tenant_id, supplier_id=supplier.id, entry_date=p["date"], kind=kind, amount_paise=p["amount_paise"],
                          mode=(p.get("mode") or "bank") if kind == "payment" else None,
                          reference=ref, note=(("From statement · " + (p.get("bank") or p.get("particulars") or "")).strip(" ·"))[:200],
                          created_by=user_id, bank_account_id=bank.id if (bank and kind == "payment") else None)
        db.add(e)
        made.append(e)
    db.flush()
    return [str(e.id) for e in made]


# ------------------------------------------------------------------ used by the bill scanner and WhatsApp

def looks_like_statement(data: bytes, content_type: str, name: str, caption: str = "") -> bool:
    """Free check before asking the AI: a typed PDF from Tally/Busy/Marg says so in words."""
    if re.search(r"\b(ledger|khata|account copy)\b", f"{caption} {name}", re.I):
        return True
    if not (content_type == "application/pdf" or data[:4] == b"%PDF"):
        return False
    t = _pdf_text(data)[:6000].lower()
    return bool(re.search(r"ledger account|statement of account|account statement|party ledger", t)
                and re.search(r"opening balance|closing balance", t))


def check(db: Session, tenant_id, st: dict, supplier_id=None, new_supplier: bool = True) -> dict:
    """Find (or add) the supplier, then compare. Returns the same shape as the app's check."""
    sup, others = (db.get(Supplier, supplier_id), []) if supplier_id else find_supplier(db, st["supplier"], st["supplier_gstin"])
    out = {"statement": st, "supplier_added": False}
    if sup is None and new_supplier and st["supplier"]:
        sup = Supplier(tenant_id=tenant_id, name=st["supplier"][:120], gstin=(st["supplier_gstin"] or None) and st["supplier_gstin"][:15])
        db.add(sup)
        db.flush()
        out["supplier_added"] = True
    if sup is None:
        return out | {"supplier": None, "choices": others}
    return out | compare(db, sup, st)


def _r(p: int | None) -> str:
    if p is None:
        return "—"
    v = abs(p) / 100
    return ("-" if p < 0 else "") + "₹" + (f"{v:,.0f}" if v == int(v) else f"{v:,.2f}")


def _dd(d) -> str:
    d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    return f"{d.day} {d.strftime('%b')}"


def summary_text(res: dict, added: int = 0) -> str:
    """The WhatsApp answer: bills in the POS, the missing ones, payments, and balances."""
    st = res["statement"]
    if not res.get("supplier"):
        return "Couldn't tell which supplier this statement is from. Check it in the app under Money › Supplier dues."
    per = f"{_dd(st['period_from'])} – {_dd(st['period_to'])}" if st.get("period_from") and st.get("period_to") else ""
    lines = [f"📒 *{res['supplier']['name']}* statement {per}".strip()]
    if res.get("supplier_added"):
        lines.append("(New supplier, added to Wise)")
    n, ok = res["bills_total"], res["bills_in_pos"]
    lines.append(f"\n🧾 Bills saved in the POS: *{ok} of {n}*")
    miss = [b for b in res["bills"] if not b["in_pos"]]
    if miss:
        lines.append(f"Missing ({_r(res['missing_paise'])}):")
        lines += [f"• {_dd(b['date'])} {b['ref'] or ''} {_r(b['amount_paise'])}".replace("  ", " ") for b in miss[:15]]
        if len(miss) > 15:
            lines.append(f"• …and {len(miss) - 15} more")
    diff = [b for b in res["bills"] if b["in_pos"] and b.get("differs_paise")]
    if diff:
        lines.append("Saved with a different amount:")
        lines += [f"• {b['ref'] or _dd(b['date'])}: theirs {_r(b['amount_paise'])}, POS {_r(b['pos_total_paise'])}" for b in diff[:8]]
    if res["only_in_pos"]:
        lines.append(f"In the POS but not on their statement: " + ", ".join(f"{x['invoice_no'] or _dd(x['date'])} {_r(x['total_paise'])}" for x in res["only_in_pos"][:6]))
    pays = res["payments"]
    if pays:
        lines.append(f"\n🏦 Payments on the statement: {len(pays)}")
        if added:
            lines.append(f"Added to supplier dues: {added}")
        for p in pays[:10]:
            lines.append(f"• {_dd(p['date'])} {_r(p['amount_paise'])} {p.get('bank') or p.get('particulars') or ''}".rstrip())
    c = res["closing"]
    if c["theirs"] is not None:
        lines.append(f"\n💰 They say you owe {_r(c['theirs'])}" + (f"; Wise says {_r(c['ours'])}" if c["ours"] is not None else ""))
        if c["ours"] is not None and abs(c["theirs"] - c["ours"]) > PAISA_SLACK:
            lines.append(f"Difference {_r(c['theirs'] - c['ours'])}" + (" — mostly the missing bills" if miss else ""))
    if not st.get("adds_up", True):
        lines.append("\n⚠️ Their totals didn't add up when read. Check the statement in the app.")
    return "\n".join(lines)


def auto_apply(db: Session, tenant_id, user_id, res: dict) -> int:
    """WhatsApp has no tick boxes: add the payments not yet recorded (and the opening balance for a supplier
    with no history). Each can be cancelled under Money › Supplier dues."""
    if not res.get("supplier"):
        return 0
    sup = db.get(Supplier, uuid.UUID(res["supplier"]["id"]))
    pays = [p for p in res["payments"] if not p["recorded"]]
    op = res["opening"]
    opening = ({"date": res["statement"]["period_from"] - timedelta(days=1), "amount_paise": op["suggest"]}
               if op.get("suggest") and res["statement"].get("period_from") else None)
    n = len(apply(db, tenant_id, user_id, sup, pays, opening))
    if n:
        for p in res["payments"]:
            p["recorded"] = True
        from .balances import statement
        bal = [x for x in statement(db, sup.id, limit=100000)["entries"] if x["date"] <= res["statement"]["period_to"]]
        res["closing"]["ours"] = bal[-1]["balance_paise"] if bal else 0
    return n
