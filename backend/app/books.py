"""wise-plus: the rest of the books for Tally and the CA, from what Wise already knows.

Each money movement is exported from exactly ONE place, so nothing is counted twice:
  - customer refunds          → Credit Note vouchers (from credit notes; a cash refund's drawer entry is skipped)
  - supplier payments         → Payment vouchers (from supplier dues, any mode; the matching bank line is skipped)
  - debit / credit notes from suppliers, goods returned → Debit Note vouchers
  - cash drawer               → expenses / drawings (Payment), deposits to the bank (Contra), cash to suppliers (Payment)
  - bank lines                → everything else that was sorted: card/UPI money (Receipt), payouts, salary, rent, charges,
                                tax, EMI, drawings, withdrawals and transfers (Contra). Lines still "to check" are left out
                                and listed in the README, as are bank deposits already exported from the drawer.
"""
from __future__ import annotations

import io
import re
import uuid
import zipfile
from collections import defaultdict
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .core import tally
from .core.tally import Voucher, ledger_name

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

L = ledger_name


def _bank_ledger(acct, ledgers: dict) -> str:
    return (acct.tally_ledger or acct.name) if acct is not None else L(ledgers, "bank")


def scope(db: Session, outlets: list[uuid.UUID], whole: bool):
    """Bank accounts in scope: all when exporting the whole business; else those taking money for these outlets."""
    from .banking.models import BankAccount
    accts = db.scalars(select(BankAccount)).all()
    if whole:
        return accts
    keep = {str(o) for o in outlets}
    return [a for a in accts if a.outlet_ids and keep & set(a.outlet_ids)]


def credit_note_vouchers(db: Session, outlets, d1: date, d2: date, ledgers: dict, accts) -> list[Voucher]:
    from .creditnotes.models import CreditNote
    from .models import Bill
    out = []
    mode_ledger = {"cash": L(ledgers, "cash"), "upi": L(ledgers, "upi"), "card": L(ledgers, "card"),
                   "bank": _bank_ledger(accts[0] if len(accts) == 1 else None, ledgers), "none": L(ledgers, "due")}
    for cn, inv in db.execute(select(CreditNote, Bill.invoice_no).join(Bill, Bill.id == CreditNote.bill_id).where(
            CreditNote.outlet_id.in_(outlets), CreditNote.note_date >= d1, CreditNote.note_date <= d2, CreditNote.status == "issued")
            .order_by(CreditNote.note_date, CreditNote.seq)):
        v = Voucher("Credit Note", cn.note_date, cn.number, f"Against bill {inv}: {cn.reason}")
        by_rate: dict[int, int] = defaultdict(int)
        for ln in cn.lines:
            by_rate[ln["rate_bp"]] += ln["taxable"]
        for rate, tx in sorted(by_rate.items()):
            v.add(L(ledgers, "sales", rate), -tx)
        v.add(L(ledgers, "cgst"), -cn.cgst_paise)
        v.add(L(ledgers, "sgst"), -cn.sgst_paise)
        v.add(L(ledgers, "igst"), -cn.igst_paise)
        v.add(L(ledgers, "round_off"), -cn.round_off_paise)
        v.add(mode_ledger.get(cn.refund_mode, L(ledgers, "cash")), cn.total_paise)
        out.append(v)
    return out


def supplier_vouchers(db: Session, d1: date, d2: date, ledgers: dict, accts, whole: bool) -> list[Voucher]:
    """Payments to suppliers (Payment) and debit / credit notes (Debit Note), from supplier dues."""
    from .ledger.models import SupplierEntry
    from .models import Supplier
    ids = {a.id for a in accts}
    by_id = {a.id: a for a in accts}
    sups = {s.id: s.name for s in db.scalars(select(Supplier))}
    mode_ledger = {"cash": L(ledgers, "cash"), "upi": L(ledgers, "upi"), "card": L(ledgers, "card"), "cheque": L(ledgers, "bank"),
                   "bank": L(ledgers, "bank"), "other": L(ledgers, "suspense")}
    out = []
    for e in db.scalars(select(SupplierEntry).where(SupplierEntry.status == "posted", SupplierEntry.kind.in_(("payment", "debit_note", "credit_note")),
                                                    SupplierEntry.entry_date >= d1, SupplierEntry.entry_date <= d2).order_by(SupplierEntry.entry_date)):
        if not whole and not (e.bank_account_id and e.bank_account_id in ids):
            continue                    # a firm's export: only payments from that firm's own bank accounts
        party = sups.get(e.supplier_id, "Supplier")
        num = f"{'PAY' if e.kind == 'payment' else 'DN'}-{str(e.id)[:8]}"
        if e.kind == "payment":
            src = _bank_ledger(by_id[e.bank_account_id], ledgers) if e.bank_account_id in by_id else mode_ledger.get(e.mode or "cash", L(ledgers, "cash"))
            v = Voucher("Payment", e.entry_date, num, f"Paid {party}" + (f" · {e.reference}" if e.reference else "") + (f" · {e.note}" if e.note else ""), party=party)
            v.add(party, -e.amount_paise)
            v.add(src, e.amount_paise)
        else:
            what = "Goods returned / debit note" if e.kind == "debit_note" else "Supplier's credit note"
            v = Voucher("Debit Note", e.entry_date, num, f"{what}: {party}" + (f" · {e.reference}" if e.reference else "") + (f" · {e.note}" if e.note else ""), party=party)
            v.add(party, -e.amount_paise)
            v.add(L(ledgers, "purchase_return"), e.amount_paise)
        out.append(v)
    return out


def cash_vouchers(db: Session, outlets, d1: date, d2: date, ledgers: dict, accts) -> list[Voucher]:
    """The cash drawers: expenses and drawings (Payment), deposits to the bank (Contra), cash to suppliers (Payment)."""
    from .models import CashMovement, Shift, Supplier
    cday = func.date(func.timezone("Asia/Kolkata", CashMovement.created_at))
    sups = {s.name.strip().lower(): s.name for s in db.scalars(select(Supplier))}
    by_name = {a.name.strip().lower(): a for a in accts} | {(a.bank or "").strip().lower(): a for a in accts if a.bank}
    cash = L(ledgers, "cash")
    out = []
    for m in db.scalars(select(CashMovement).join(Shift, Shift.id == CashMovement.shift_id).where(
            Shift.outlet_id.in_(outlets), cday >= d1, cday <= d2).order_by(CashMovement.created_at)):
        day = m.created_at.astimezone(IST).date()
        num = f"CASH-{str(m.id)[:8]}"
        note = m.note or m.reason
        if m.reason and m.reason.startswith("Refund CN/"):
            continue                    # in the credit note voucher
        if m.kind == "out":
            if m.purpose == "expense":
                v = Voucher("Payment", day, num, f"Cash expense: {note}")
                v.add((m.category or "Cash Expenses").strip(), -m.amount_paise)
            elif m.purpose == "drawing" and m.handed == "bank":
                acct = by_name.get((m.paid_to or "").strip().lower()) or (accts[0] if len(accts) == 1 else None)
                v = Voucher("Contra", day, num, f"Cash deposited in the bank: {note}")
                v.add(_bank_ledger(acct, ledgers), -m.amount_paise)
            elif m.purpose == "drawing":
                v = Voucher("Payment", day, num, f"Drawings: {m.paid_to or ''} {note}".strip())
                v.add(L(ledgers, "drawings"), -m.amount_paise)
            elif m.purpose == "supplier":
                party = sups.get((m.paid_to or "").strip().lower(), (m.paid_to or "Supplier").strip())
                v = Voucher("Payment", day, num, f"Cash paid to {party}", party=party)
                v.add(party, -m.amount_paise)
            elif m.paid_to == "Petty cash":
                v = Voucher("Contra", day, num, "Cash to petty cash")
                v.add(PETTY, -m.amount_paise)
            else:
                v = Voucher("Payment", day, num, f"Cash out: {note}")
                v.add(L(ledgers, "suspense"), -m.amount_paise)
            v.add(cash, m.amount_paise)
        elif m.reason == "Back from petty cash":
            v = Voucher("Contra", day, num, "Back from petty cash")
            v.add(cash, -m.amount_paise)
            v.add(PETTY, m.amount_paise)
        else:
            v = Voucher("Contra" if m.purpose in ("topup", "change") else "Receipt", day, num, f"Cash put in: {note}")
            v.add(cash, -m.amount_paise)
            v.add(L(ledgers, "capital") if m.purpose == "topup" else L(ledgers, "suspense"), m.amount_paise)
        out.append(v)
    return out


PETTY = "Petty Cash"


def petty_vouchers(db: Session, outlets, d1: date, d2: date, ledgers: dict, accts) -> list[Voucher]:
    """wise-plus: petty expenses not paid from the counter (those are cash movements already), and petty cash top-ups."""
    from .expenses.models import PettyExpense, PettyTopup
    bank = _bank_ledger(accts[0], ledgers) if len(accts) == 1 else L(ledgers, "suspense")
    out = []
    for x in db.scalars(select(PettyExpense).where(PettyExpense.outlet_id.in_(outlets), PettyExpense.status == "ok",
                                                   PettyExpense.paid_from != "counter", PettyExpense.spent_on >= d1,
                                                   PettyExpense.spent_on <= d2).order_by(PettyExpense.spent_on)):
        what = x.what or x.category
        if x.qty_m and x.unit:
            what += f" ({x.qty_m / 1000:g} {x.unit})"
        v = Voucher("Payment", x.spent_on, f"PETTY-{str(x.id)[:8]}", f"Petty expense: {what}")
        v.add(x.category.strip(), -x.amount_paise)
        v.add({"petty": PETTY, "owner": L(ledgers, "capital"), "upi": bank}.get(x.paid_from, PETTY), x.amount_paise)
        out.append(v)
    tday = func.date(func.timezone("Asia/Kolkata", PettyTopup.created_at))
    for t in db.scalars(select(PettyTopup).where(PettyTopup.outlet_id.in_(outlets), PettyTopup.source != "counter", tday >= d1, tday <= d2)):
        day = t.created_at.astimezone(IST).date()
        v = Voucher("Contra" if t.source == "bank" else "Receipt", day, f"PETTYIN-{str(t.id)[:8]}", f"Petty cash top-up ({t.source})")
        v.add(PETTY, -t.amount_paise)
        v.add(bank if t.source == "bank" else L(ledgers, "capital"), t.amount_paise)
        out.append(v)
    return out


BANK_OUT = {"expense": None, "salary": "salary", "rent": "rent", "charges": "bank_charges", "tax": "tax_paid", "loan": "loan",
            "drawing": "drawings", "other_out": "suspense"}
BANK_IN = {"aggregator": "aggregator", "capital": "capital", "refund": "refund_in", "other_in": "suspense"}


def bank_vouchers(db: Session, d1: date, d2: date, ledgers: dict, accts) -> tuple[list[Voucher], dict]:
    from .banking.models import BankTxn
    by_id = {a.id: a for a in accts}
    skipped = {"to_check": 0, "from_drawer": 0, "supplier_in_dues": 0}
    out = []
    if not by_id:
        return out, skipped
    for t in db.scalars(select(BankTxn).where(BankTxn.bank_account_id.in_(list(by_id)), BankTxn.txn_date >= d1, BankTxn.txn_date <= d2)
                        .order_by(BankTxn.txn_date, BankTxn.created_at)):
        if t.status in ("unmatched", "suggested"):
            skipped["to_check"] += 1
            continue
        if t.status == "ignored":
            continue
        bank = _bank_ledger(by_id[t.bank_account_id], ledgers)
        amt = abs(t.amount_paise)
        num = f"BNK-{str(t.id)[:8]}"
        text = t.narration[:200]
        k = t.kind
        if k == "supplier":
            skipped["supplier_in_dues"] += 1          # the Payment voucher comes from supplier dues
            continue
        if k == "cash_deposit" and (t.note or "").startswith("Cash from the drawer"):
            skipped["from_drawer"] += 1               # the Contra comes from the drawer
            continue
        if k == "transfer":
            if t.amount_paise > 0 and t.other_account_id in by_id:
                continue                              # the other account's outgoing line makes the Contra
            other = by_id.get(t.other_account_id)
            other_l = _bank_ledger(other, ledgers) if other else L(ledgers, "suspense")
            v = Voucher("Contra", t.txn_date, num, text)
            if t.amount_paise < 0:
                v.add(other_l, -amt)
                v.add(bank, amt)
            else:
                v.add(bank, -amt)
                v.add(other_l, amt)
            out.append(v)
            continue
        if t.amount_paise > 0:
            if k == "sales":
                cr = L(ledgers, "card") if (t.note or "").startswith("CARD") else L(ledgers, "upi")
                v = Voucher("Receipt", t.txn_date, num, f"{t.note or 'Sales money'}: {text}")
            elif k == "cash_deposit":
                cr, v = L(ledgers, "cash"), Voucher("Contra", t.txn_date, num, f"Cash deposited: {text}")
            else:
                cr = L(ledgers, "interest") if t.category == "Bank interest" else L(ledgers, BANK_IN.get(k, "suspense"))
                v = Voucher("Receipt", t.txn_date, num, text)
            v.add(bank, -amt)
            v.add(cr, amt)
        else:
            if k == "cash_withdrawal":
                dr, v = L(ledgers, "cash"), Voucher("Contra", t.txn_date, num, f"Cash withdrawn: {text}")
            else:
                if k == "expense":
                    dr = (t.category or "Expenses").strip()
                elif k == "charges" and t.category == "Aggregator charges":
                    dr = L(ledgers, "aggregator_charges")
                else:
                    dr = L(ledgers, BANK_OUT.get(k) or "suspense")
                v = Voucher("Payment", t.txn_date, num, text)
            v.add(dr, -amt)
            v.add(bank, amt)
        out.append(v)
    return out, skipped


def extra_masters(db: Session, ledgers: dict, accts) -> list[tuple[str, str, str | None]]:
    """Ledgers the new vouchers use: bank accounts, expense heads, drawings, salary…"""
    from .banking.models import BankTxn
    from .models import CashMovement
    names = [(_bank_ledger(a, ledgers), "Bank Accounts", None) for a in accts]
    for key in ("bank", "drawings", "capital", "salary", "rent", "bank_charges", "tax_paid", "loan", "suspense", "aggregator",
                "interest", "purchase_return", "refund_in", "aggregator_charges"):
        names.append((L(ledgers, key), tally.PARENTS[key], None))
    heads = set(db.scalars(select(BankTxn.category).where(BankTxn.kind == "expense", BankTxn.category.is_not(None)).distinct()).all())
    heads |= set(db.scalars(select(CashMovement.category).where(CashMovement.purpose == "expense", CashMovement.category.is_not(None)).distinct()).all())
    from .expenses.models import PettyExpense
    heads |= set(db.scalars(select(PettyExpense.category).where(PettyExpense.status == "ok").distinct()).all())
    names += [(h.strip(), "Indirect Expenses", None) for h in sorted(heads) if h and h.strip()]
    names.append(("Cash Expenses", "Indirect Expenses", None))
    names.append((PETTY, "Cash-in-Hand", None))
    return names


# ------------------------------------------------------------------ Excel sheets for the CA

def extra_sheets(db: Session, add_sheet, outlets, d1: date, d2: date, accts, whole: bool) -> dict:
    """Credit notes, supplier payments and notes, bank lines, and the GST summary. Returns figures for the README."""
    from .banking.models import KIND_LABEL, BankTxn
    from .creditnotes.models import CreditNote
    from .ledger.models import SupplierEntry
    from .models import Bill, BillLine, Purchase, Supplier
    r = lambda p: round((p or 0) / 100, 2)  # noqa: E731
    cns = db.execute(select(CreditNote, Bill.invoice_no, Bill.customer_gstin).join(Bill, Bill.id == CreditNote.bill_id).where(
        CreditNote.outlet_id.in_(outlets), CreditNote.note_date >= d1, CreditNote.note_date <= d2).order_by(CreditNote.note_date, CreditNote.seq)).all()
    add_sheet("Credit notes", [("Date", 11, False), ("Credit note", 18, False), ("Against bill", 18, False), ("Customer GSTIN", 17, False),
                               ("Status", 10, False), ("Reason", 26, False), ("Paid back by", 12, False), ("Taxable", 11, True), ("CGST", 9, True),
                               ("SGST", 9, True), ("IGST", 9, True), ("Total", 11, True)],
              [[str(c.note_date), c.number, inv, g or "", c.status, c.reason, c.refund_mode] + ([r(c.taxable_paise), r(c.cgst_paise), r(c.sgst_paise),
               r(c.igst_paise), r(c.total_paise)] if c.status == "issued" else [0, 0, 0, 0, 0]) for c, inv, g in cns],
              "Money given back on bills. GSTR-1: B2C credit notes reduce that month's B2CS; B2B ones go in CDNR.")
    sups = {s.id: s for s in db.scalars(select(Supplier))}
    ids = {a.id for a in accts}
    names = {a.id: a.name for a in accts}
    ent = [e for e in db.scalars(select(SupplierEntry).where(SupplierEntry.status == "posted", SupplierEntry.kind != "opening",
                                                             SupplierEntry.entry_date >= d1, SupplierEntry.entry_date <= d2).order_by(SupplierEntry.entry_date))
           if whole or (e.bank_account_id in ids)]
    label = {"payment": "Payment", "debit_note": "Debit note / return", "credit_note": "Their credit note"}
    add_sheet("Supplier payments & notes", [("Date", 11, False), ("Supplier", 22, False), ("Type", 18, False), ("Mode / bank", 16, False),
                                            ("Reference", 16, False), ("Note", 30, False), ("Amount", 12, True)],
              [[str(e.entry_date), sups[e.supplier_id].name if e.supplier_id in sups else "", label.get(e.kind, e.kind),
                names.get(e.bank_account_id) or (e.mode or "").upper(), e.reference or "", e.note or "", r(e.amount_paise)] for e in ent],
              "From Money › Supplier dues. Payments become Payment vouchers; debit and credit notes become Debit Note vouchers.")
    lines = db.scalars(select(BankTxn).where(BankTxn.bank_account_id.in_(list(ids) or [uuid.uuid4()]), BankTxn.txn_date >= d1, BankTxn.txn_date <= d2)
                       .order_by(BankTxn.bank_account_id, BankTxn.txn_date, BankTxn.created_at)).all()
    status_txt = {"matched": "Sorted", "suggested": "To check", "unmatched": "To check", "ignored": "Left out"}
    add_sheet("Bank lines", [("Date", 11, False), ("Account", 16, False), ("Description", 40, False), ("Ref", 14, False), ("What", 20, False),
                             ("Head / note", 24, False), ("Status", 10, False), ("Money in", 12, True), ("Money out", 12, True)],
              [[str(t.txn_date), names.get(t.bank_account_id, ""), t.narration, t.ref or "", KIND_LABEL.get(t.kind or "", ""),
                t.category or t.note or "", status_txt.get(t.status, t.status), r(t.amount_paise) if t.amount_paise > 0 else 0,
                r(-t.amount_paise) if t.amount_paise < 0 else 0] for t in lines],
              "Every bank statement line in the period and what it was. Lines 'To check' aren't in the Tally file yet.")
    # GST summary (GSTR-3B style)
    paid = [Bill.outlet_id.in_(outlets), Bill.business_date >= d1, Bill.business_date <= d2, Bill.status == "paid"]
    out_rate = {int(rt): (int(tx), int(c), int(s), int(i)) for rt, tx, c, s, i in db.execute(
        select(BillLine.rate_bp, func.sum(BillLine.taxable_paise), func.sum(BillLine.cgst_paise), func.sum(BillLine.sgst_paise), func.sum(BillLine.igst_paise))
        .join(Bill, Bill.id == BillLine.bill_id).where(*paid).group_by(BillLine.rate_bp))}
    cn_rate: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for c, _inv, _g in cns:
        if c.status != "issued":
            continue
        for ln in c.lines:
            x = cn_rate[ln["rate_bp"]]
            x[0] += ln["taxable"]; x[1] += ln["cgst"]; x[2] += ln["sgst"]; x[3] += ln["igst"]  # noqa: E702
    rows = []
    for rate in sorted(set(out_rate) | set(cn_rate)):
        o = out_rate.get(rate, (0, 0, 0, 0))
        c = cn_rate.get(rate, [0, 0, 0, 0])
        rows.append([f"Outward supplies @ {rate / 100:g}%", r(o[0]), r(c[0]), r(o[0] - c[0]), r(o[1] - c[1]), r(o[2] - c[2]), r(o[3] - c[3])])
    pur = db.scalars(select(Purchase).where(Purchase.outlet_id.in_(outlets), Purchase.invoice_date >= d1, Purchase.invoice_date <= d2, Purchase.status == "posted")).all()
    itc = sum(p.tax_paise for p in pur)
    rows.append(["Input tax on purchases (check against GSTR-2B)", r(sum(p.amount_paise for p in pur)), 0, r(sum(p.amount_paise for p in pur)),
                 r(itc - itc // 2), r(itc // 2), 0])
    add_sheet("GST summary", [("Line", 44, False), ("Taxable", 13, True), ("Less credit notes", 13, True), ("Net taxable", 13, True),
                              ("CGST", 11, True), ("SGST", 11, True), ("IGST", 11, True)], rows,
              "Outward supplies net of credit notes (GSTR-3B 3.1a) and input tax from purchases entered (4A). The totals row adds both; read each line.")
    return {"credit_notes": len(cns), "bank_lines": len(lines), "to_check": sum(1 for t in lines if t.status in ("suggested", "unmatched")),
            "purchases": len(pur)}


def bill_files(db: Session, outlets, d1: date, d2: date) -> list[tuple[str, str]]:
    """(path on disk, name in the ZIP) for the kept purchase bill photos / PDFs of the period."""
    import os
    from .models import Purchase, Supplier
    from .purchases_ai.models import ScannedBill
    sups = {s.id: s.name for s in db.scalars(select(Supplier))}
    out = []
    for sb, p in db.execute(select(ScannedBill, Purchase).join(Purchase, Purchase.id == ScannedBill.purchase_id).where(
            Purchase.outlet_id.in_(outlets), Purchase.invoice_date >= d1, Purchase.invoice_date <= d2, Purchase.status == "posted")):
        if sb.file_path and os.path.exists(sb.file_path):
            ext = sb.file_path.rsplit(".", 1)[-1]
            nm = re.sub(r"[^A-Za-z0-9 ._-]", "", f"{p.invoice_date} {sups.get(p.supplier_id, 'supplier')} {p.invoice_no or ''}").strip()[:90]
            out.append((sb.file_path, f"purchase-bills/{nm}.{ext}"))
    return out


def zip_pack(files: dict[str, bytes], disk: list[tuple[str, str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
        seen = set()
        for path, name in disk:
            n, i = name, 1
            while n in seen:
                i += 1
                n = name.replace(".", f" ({i}).", 1) if "." in name else f"{name} ({i})"
            seen.add(n)
            z.write(path, n)
    return buf.getvalue()
