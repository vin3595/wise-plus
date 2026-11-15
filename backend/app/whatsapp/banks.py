"""Banks on WhatsApp: *bank* (balances), *bank check* (one line at a time, tap what it was), statements sent as files.

Taps come back as commands: "bk <line id> <what>" where what = s:<supplier id> | e | e:<category> | d (drawing) |
sal | sales | cap | oin | oout | skip."""
from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..assistant.answers import Reply
from ..banking import service as svc
from ..banking.models import BankTxn
from ..models import Supplier

WORDS = {"bank", "banks", "balance bank", "bank balance", "banks balance"}
CHECK = {"bank check", "check bank", "bank to check", "tocheck", "to check"}
CATS = ["Electricity", "Rent", "Repairs", "Gas", "Packaging", "Marketing", "Transport", "Groceries", "Phone & internet"]


def handle(db: Session, user_id: str, word: str) -> Reply | None:
    if word in WORDS:
        n = db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.status.in_(("suggested", "unmatched")))) or 0
        return Reply(svc.balances_text(db), [("bank check", f"To check ({n})")] if n else [])
    if word in CHECK:
        return next_line(db)
    m = re.fullmatch(r"bk ([0-9a-f-]{36}) (.+)", word)
    if not m:
        return None
    t = db.get(BankTxn, uuid.UUID(m.group(1)))
    if t is None:
        return Reply("That line isn't here any more.")
    what = m.group(2).strip()
    uid = uuid.UUID(user_id)
    if what == "e":
        cats = sorted(set(CATS) | set(db.scalars(select(BankTxn.category).where(BankTxn.category.is_not(None)).distinct()).all()))[:9]
        return Reply(svc.check_text(t) + "\n\nWhat was it for?", [(f"bk {t.id} e:{c}", c) for c in cats] + [(f"bk {t.id} skip", "Leave it")])
    if what == "skip":
        t.status, t.matched_by = "ignored", "person"
        return next_line(db, done="Left it.")
    if what == "later":
        return Reply("OK. Send *bank check* any time.")
    if what.startswith("s:"):
        try:
            sid = uuid.UUID(what[2:])
        except ValueError:
            return Reply("Couldn't tell which supplier.")
        more = svc.set_kind(db, t, "supplier", uid, supplier_id=sid)
        s = db.get(Supplier, sid)
        return next_line(db, done=f"✅ Paid to {s.name if s else 'supplier'}, added to supplier dues." + _more(more))
    if what == "sup":
        sups = db.scalars(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name)).all()[:9]
        return Reply(svc.check_text(t) + "\n\nWhich supplier?", [(f"bk {t.id} s:{s.id}", s.name) for s in sups] + [(f"bk {t.id} skip", "Leave it")])
    kinds = {"d": "drawing", "sal": "salary", "sales": "sales", "cap": "capital", "oin": "other_in", "oout": "other_out",
             "dep": "cash_deposit", "wd": "cash_withdrawal", "rent": "rent", "tax": "tax", "chg": "charges"}
    if what.startswith("e:"):
        more = svc.set_kind(db, t, "expense", uid, category=svc.clean_category(what[2:]))
        return next_line(db, done=f"✅ Expense: {t.category}." + _more(more))
    if what in kinds:
        more = svc.set_kind(db, t, kinds[what], uid)
        return next_line(db, done=f"✅ {svc.fmt_kind(kinds[what])}." + _more(more))
    return None


def _more(n: int) -> str:
    return f" Also sorted {n} similar line(s); I'll remember it." if n else " I'll remember it next time."


def next_line(db: Session, done: str | None = None) -> Reply:
    q = select(BankTxn).where(BankTxn.status.in_(("suggested", "unmatched"))).order_by(BankTxn.txn_date, BankTxn.created_at)
    t = db.scalars(q.limit(1)).first()
    left = db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.status.in_(("suggested", "unmatched")))) or 0
    head = (done + "\n\n") if done else ""
    if t is None:
        return Reply(head + "🎉 Nothing left to check.", [("bank", "Balances")])
    body = head + f"*To check* ({left})\n" + svc.check_text(t)
    if t.amount_paise < 0:
        opts = []
        if t.kind == "supplier" and t.supplier_id:
            s = db.get(Supplier, t.supplier_id)
            opts.append((f"bk {t.id} s:{t.supplier_id}", f"Yes, {s.name}"[:24] if s else "Yes, supplier"))
        opts += [(f"bk {t.id} sup", "Supplier"), (f"bk {t.id} e", "Expense"), (f"bk {t.id} d", "Owner's drawing"),
                 (f"bk {t.id} sal", "Salary"), (f"bk {t.id} rent", "Rent"), (f"bk {t.id} wd", "Cash withdrawn"),
                 (f"bk {t.id} tax", "Tax / GST"), (f"bk {t.id} oout", "Other"), (f"bk {t.id} skip", "Leave it")]
    else:
        opts = [(f"bk {t.id} sales", "Sales"), (f"bk {t.id} dep", "Cash deposited"), (f"bk {t.id} cap", "Owner's money"),
                (f"bk {t.id} oin", "Other"), (f"bk {t.id} skip", "Leave it")]
    return Reply(body, opts[:10])
