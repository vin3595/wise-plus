"""Match bank lines to what Wise knows. Order: what the person taught us (rules) > exact links (a supplier payment
already in dues, a cash deposit from the drawer, a day's card / UPI sales) > well-known words (Zomato, salary,
charges, ATM…) > a supplier's name in the description (suggested) > left for the person ("To check")."""
from __future__ import annotations

import hashlib
import uuid
import re
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Bill, CashMovement, Payment, Supplier
from ..ledger.models import SupplierEntry
from .models import BankAccount, BankRule, BankTxn

SLACK = 100              # ₹1
FEE = 0.035              # card / UPI settlements arrive up to 3.5% short (MDR + GST)
LAG = 4                  # and up to 4 days late

WORDS_IN = [
    (r"zomato|eternal ltd|swiggy|bundl tech|magicpin|ondc|eatsure|dunzo|zepto", "aggregator", None),
    (r"cash dep|by cash|cash deposit|\bcdm\b|cash-dep|bna dep|cash cdm", "cash_deposit", None),
    (r"\b(refund|reversal|rev |cashback)", "refund", None),
    (r"\b(int\.? ?pd|interest paid|int credit|sb int|credit interest)", "other_in", "Bank interest"),
    (r"\b(pos settl|settlement|setl|msf|pine ?labs|paytm payments|razorpay|phonepe|cashfree|payu|billdesk|mswipe|ezetap|bharatpe|getepay)",
     "sales", None),
]
WORDS_OUT = [
    (r"\b(salary|sal for|wages|payroll)\b", "salary", None),
    (r"\brent\b", "rent", None),
    (r"\b(gst|gstn|cbdt|tds|income tax|cbic|tax payment|challan)\b", "tax", None),
    (r"\b(emi|loan|ach d|nach|bajaj fin|hdfc ltd)\b", "loan", None),
    (r"\b(chrg|chgs|charges?|fee|sms alert|amc|annual fee|min bal|gst on|service tax|debit card fee)\b", "charges", "Bank charges"),
    (r"\b(atm|cash wdl|cash withdrawal|self chq|nwd|awb|eaw)\b", "cash_withdrawal", None),
    (r"\b(jvvnl|jdvvnl|avvnl|electricity|power|bses|tata power|msedcl)\b", "expense", "Electricity"),
    (r"\b(airtel|jio|vodafone|vi |bsnl|act fibernet|broadband)\b", "expense", "Phone & internet"),
    (r"\b(zomato|swiggy)\b", "charges", "Aggregator charges"),
]


def fingerprint(account_id, r: dict, seen: dict) -> str:
    """Same date, amount, balance and description = the same line; identical lines on one day stay separate (n)."""
    base = f"{account_id}|{r['date']}|{r['amount_paise']}|{r.get('balance_paise')}|{re.sub(r'[^a-z0-9]', '', r['narration'].lower())[:120]}"
    seen[base] = seen.get(base, 0) + 1
    return hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()


def words(narration: str) -> str:
    """The stable words of a description: 'UPI/412345/JVVNL JODHPUR/jvvnl@sbi' -> 'jvvnl jodhpur'."""
    t = narration.lower()
    t = re.sub(r"[a-z0-9._-]+@[a-z]+", " ", t)                       # UPI ids
    t = re.sub(r"\b(upi|neft|imps|rtgs|nach|ach|mmt|inb|ib|ft|trf|to|from|by|cr|dr|p2a|p2m|ref|no|txn|chq|transfer|payment|paid)\b", " ", t)
    t = re.sub(r"[^a-z ]", " ", t)
    toks = [w for w in t.split() if len(w) >= 3]
    return " ".join(toks[:4])


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _supplier_in(narr: str, sups: list[Supplier]) -> Supplier | None:
    n = _norm(narr)
    best = None
    for s in sups:
        toks = [t for t in re.findall(r"[a-z0-9]+", s.name.lower()) if len(t) >= 4 and t not in
                {"agency", "agencies", "traders", "trading", "store", "stores", "enterprises", "foods", "products", "private",
                 "limited", "india", "general", "provision", "marketing", "company", "sales", "distributors", "shri", "shree"}]
        if toks and all(t in n for t in toks[:2]):
            if best is None or len(s.name) > len(best.name):
                best = s
    return best


def sales_by_day(db: Session, acct: BankAccount, lo: date, hi: date) -> dict:
    """{(day, 'upi'|'card'): paise} for the outlets whose money lands in this account."""
    q = (select(Bill.business_date, Payment.mode, func.sum(Payment.amount_paise)).join(Bill, Bill.id == Payment.bill_id)
         .where(Bill.status == "paid", Payment.mode.in_(("upi", "card")), Bill.business_date >= lo, Bill.business_date <= hi)
         .group_by(Bill.business_date, Payment.mode))
    if acct.outlet_ids:
        q = q.where(Bill.outlet_id.in_([uuid.UUID(x) for x in acct.outlet_ids]))
    return {(d, m): int(v or 0) for d, m, v in db.execute(q).all()}


def match_account(db: Session, acct: BankAccount, txns: list[BankTxn], user_id=None) -> None:
    """Match these (new or still open) lines. Changes BankTxn rows; may add supplier-dues payments for rules."""
    if not txns:
        return
    lo, hi = min(t.txn_date for t in txns), max(t.txn_date for t in txns)
    rules = db.scalars(select(BankRule)).all()
    sups = db.scalars(select(Supplier).where(Supplier.is_active.is_(True))).all()
    others = [a for a in db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))) if a.id != acct.id and a.last4]
    taken_entries = set(db.scalars(select(BankTxn.supplier_entry_id).where(BankTxn.supplier_entry_id.is_not(None))).all())
    entries = [e for e in db.scalars(select(SupplierEntry).where(
        SupplierEntry.status == "posted", SupplierEntry.kind == "payment", SupplierEntry.mode.in_(("bank", "upi", "cheque", "card", "other")),
        SupplierEntry.entry_date >= lo - timedelta(days=LAG), SupplierEntry.entry_date <= hi + timedelta(days=LAG)))
        if e.id not in taken_entries and (e.bank_account_id in (None, acct.id))]
    deposits = db.scalars(select(CashMovement).where(
        CashMovement.kind == "out", CashMovement.purpose == "drawing", CashMovement.handed == "bank",
        func.date(CashMovement.created_at) >= lo - timedelta(days=LAG), func.date(CashMovement.created_at) <= hi)).all()
    used_dep: set = set()
    sales = sales_by_day(db, acct, lo - timedelta(days=LAG), hi)
    covered: dict = defaultdict(int)        # what of each day's sales has been found

    def done(t: BankTxn, kind: str, status: str = "matched", by: str = "auto", note: str | None = None, **kw):
        t.kind, t.status, t.matched_by = kind, status, by
        if note:
            t.note = note[:160]
        for k, v in kw.items():
            setattr(t, k, v)

    open_ = [t for t in txns if t.status in ("unmatched", "suggested") and t.matched_by != "person"]
    for t in open_:
        narr = t.narration.lower()
        direction = "in" if t.amount_paise > 0 else "out"
        amt = abs(t.amount_paise)
        # 1. taught by the person
        rule = next((r for r in sorted(rules, key=lambda r: -len(r.pattern)) if r.direction == direction and r.pattern in words(t.narration)), None)
        if rule:
            rule.uses += 1
            if rule.kind == "supplier" and rule.supplier_id:
                link_supplier(db, t, rule.supplier_id, user_id, entries, by="rule")
            else:
                done(t, rule.kind, by="rule", category=rule.category)
            continue
        if direction == "out":
            # 2. a supplier payment already in supplier dues
            e = next((e for e in entries if abs(e.amount_paise - amt) <= SLACK and abs((e.entry_date - t.txn_date).days) <= LAG), None)
            if e:
                entries.remove(e)
                sname = next((s.name for s in sups if s.id == e.supplier_id), "supplier")
                done(t, "supplier", supplier_id=e.supplier_id, supplier_entry_id=e.id, note=f"Paid to {sname}")
                continue
            # 3. to one of our own accounts
            o = next((a for a in others if a.last4 and a.last4 in re.sub(r"\D", "", t.narration)), None)
            if o:
                done(t, "transfer", other_account_id=o.id, note=f"To {o.name}")
                continue
            s = _supplier_in(t.narration, sups)
            if s:
                done(t, "supplier", "suggested", supplier_id=s.id, note=f"Looks like a payment to {s.name}")
                continue
            for pat, kind, cat in WORDS_OUT:
                if re.search(pat, narr):
                    done(t, kind, category=cat)
                    break
            continue
        # money in
        d = next((d for d in deposits if d.id not in used_dep and abs(d.amount_paise - amt) <= SLACK
                  and 0 <= (t.txn_date - d.created_at.date()).days <= LAG), None)
        if d:
            used_dep.add(d.id)
            done(t, "cash_deposit", note=f"Cash from the drawer, {d.created_at.date():%d %b}")
            continue
        o = next((a for a in others if a.last4 and a.last4 in re.sub(r"\D", "", t.narration)), None)
        if o:
            done(t, "transfer", other_account_id=o.id, note=f"From {o.name}")
            continue
        hit = None
        for (day, mode), total in sorted(sales.items(), key=lambda kv: -kv[0][0].toordinal()):
            if 0 <= (t.txn_date - day).days <= LAG and total - covered[(day, mode)] > 0:
                left = total - covered[(day, mode)]
                if left * (1 - FEE) - SLACK <= amt <= left + SLACK:      # one settlement for the day
                    hit = (day, mode, amt)
                    break
        if hit:
            day, mode, _ = hit
            covered[(day, mode)] += sales[(day, mode)] - covered[(day, mode)]
            done(t, "sales", note=f"{mode.upper()} sales of {day:%d %b}")
            continue
        for pat, kind, cat in WORDS_IN:
            if re.search(pat, narr):
                if kind == "cash_deposit":
                    done(t, kind, "suggested", note="Cash deposit not entered in a cash drawer")
                else:
                    done(t, kind, category=cat)
                break
        else:
            if re.search(r"\bupi\b", narr):
                done(t, "sales", "suggested", note="UPI received")          # summed by day below

    # UPI paid straight into the account arrives one by one: compare each day's UPI money with UPI sales.
    upi = defaultdict(list)
    for t in open_:
        if t.kind == "sales" and t.status == "suggested" and t.note == "UPI received":
            upi[t.txn_date].append(t)
    for day, lines in upi.items():
        total = sales.get((day, "upi"), 0)
        got = sum(t.amount_paise for t in lines)
        if total and abs(got - total) <= max(SLACK, int(total * 0.02)):
            for t in lines:
                done(t, "sales", note=f"UPI sales of {day:%d %b}")


def link_supplier(db: Session, t: BankTxn, supplier_id, user_id, entries: list | None = None, by: str = "person") -> None:
    """A bank payment to a supplier: link it to that payment in supplier dues, adding the payment if it isn't there."""
    amt = abs(t.amount_paise)
    pool = entries if entries is not None else db.scalars(select(SupplierEntry).where(
        SupplierEntry.supplier_id == supplier_id, SupplierEntry.status == "posted", SupplierEntry.kind == "payment")).all()
    taken = set(db.scalars(select(BankTxn.supplier_entry_id).where(BankTxn.supplier_entry_id.is_not(None), BankTxn.id != t.id)).all())
    e = next((e for e in pool if e.supplier_id == supplier_id and e.id not in taken and abs(e.amount_paise - amt) <= SLACK
              and abs((e.entry_date - t.txn_date).days) <= LAG), None)
    if e is None and user_id is not None:
        e = SupplierEntry(tenant_id=t.tenant_id, supplier_id=supplier_id, entry_date=t.txn_date, kind="payment", amount_paise=amt,
                          mode="bank", bank_account_id=t.bank_account_id, reference=(t.ref or "")[:60] or None,
                          note=f"From bank statement: {t.narration}"[:200], created_by=user_id)
        db.add(e)
        db.flush()
    elif e is not None and e.bank_account_id is None:
        e.bank_account_id = t.bank_account_id
    if e is not None and entries is not None and e in entries:
        entries.remove(e)
    s = db.get(Supplier, supplier_id)
    t.kind, t.status, t.matched_by = "supplier", "matched" if e is not None else "suggested", by
    t.supplier_id, t.supplier_entry_id = supplier_id, e.id if e is not None else None
    t.note = f"Paid to {s.name if s else 'supplier'}"


def missing_money(db: Session, acct: BankAccount, lo: date, hi: date) -> list[dict]:
    """Card / UPI sales days whose money isn't in the account (only days at least LAG days before the statement ends).
    Only for an account that card / UPI money is set to land in."""
    if acct.outlet_ids is None and db.scalar(select(func.count()).select_from(BankAccount).where(BankAccount.is_active.is_(True))) > 1:
        return []
    sales = sales_by_day(db, acct, lo, hi - timedelta(days=LAG))
    found: dict = defaultdict(int)
    for t in db.scalars(select(BankTxn).where(BankTxn.bank_account_id == acct.id, BankTxn.kind == "sales", BankTxn.status == "matched",
                                              BankTxn.txn_date >= lo, BankTxn.txn_date <= hi)):
        m = re.match(r"(UPI|CARD) sales of (\d{1,2} \w{3})", t.note or "")
        if m:
            for (d, mode) in sales:
                if mode == m.group(1).lower() and f"{d:%d %b}" == m.group(2):
                    found[(d, mode)] += t.amount_paise
    out = []
    for (d, mode), total in sorted(sales.items()):
        short = total - found[(d, mode)]
        if short > max(10000, int(total * FEE)):
            out.append({"date": d, "mode": mode, "sales_paise": total, "received_paise": found[(d, mode)], "short_paise": short})
    return out
