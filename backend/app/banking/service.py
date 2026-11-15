"""Bank statement import and the answers shared by the app and WhatsApp."""
from __future__ import annotations

import re
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .match import fingerprint, match_account, missing_money, words
from .models import KIND_LABEL, BankAccount, BankRule, BankStatement, BankTxn


def find_account(db: Session, tenant_id, bank: str | None, last4: str | None, account_id=None) -> tuple[BankAccount, bool]:
    """The account this statement belongs to: the one chosen, else by last 4 digits, else the only one; otherwise a new one."""
    if account_id:
        a = db.get(BankAccount, account_id)
        if a is not None:
            return a, False
    accts = db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))).all()
    if last4:
        a = next((a for a in accts if a.last4 == last4), None)
        if a:
            return a, False
    if len(accts) == 1 and (not last4 or not accts[0].last4):
        a = accts[0]
        if last4 and not a.last4:
            a.last4 = last4
        return a, False
    short = (bank or "Bank").replace(" Bank", "").strip() or "Bank"
    name = f"{short} ···{last4}" if last4 else f"{short} account"
    n, base = 1, name
    while any(x.name == name for x in accts):
        n += 1
        name = f"{base} {n}"
    a = BankAccount(tenant_id=tenant_id, name=name[:60], bank=bank, last4=last4)
    db.add(a)
    db.flush()
    return a, True


def import_statement(db: Session, tenant_id, user_id, st: dict, account_id=None, file_name: str | None = None,
                     file_path: str | None = None) -> dict:
    acct, created = find_account(db, tenant_id, st.get("bank"), st.get("last4"), account_id)
    stmt = BankStatement(tenant_id=tenant_id, bank_account_id=acct.id, file_name=(file_name or "")[:120] or None, file_path=file_path,
                         period_from=st.get("period_from"), period_to=st.get("period_to"), closing_paise=st.get("closing_paise"),
                         lines_read=len(st["rows"]), uploaded_by=user_id)
    db.add(stmt)
    db.flush()
    have = set(db.scalars(select(BankTxn.fingerprint).where(BankTxn.bank_account_id == acct.id)).all())
    seen: dict = {}
    new = []
    for r in st["rows"]:
        fp = fingerprint(acct.id, r, seen)
        if fp in have:
            continue
        have.add(fp)
        t = BankTxn(tenant_id=tenant_id, bank_account_id=acct.id, statement_id=stmt.id, txn_date=r["date"], narration=r["narration"],
                    ref=r.get("ref"), amount_paise=r["amount_paise"], balance_paise=r.get("balance_paise"), fingerprint=fp)
        db.add(t)
        new.append(t)
    db.flush()
    stmt.lines_new = len(new)
    if acct.opening_date is None and st.get("rows"):
        first = st["rows"][0]
        if first.get("balance_paise") is not None:
            acct.opening_paise = first["balance_paise"] - first["amount_paise"]
            acct.opening_date = first["date"]
    match_account(db, acct, new, user_id)
    db.flush()
    return summary(db, acct, new, st, created)


def balance(db: Session, acct: BankAccount) -> int | None:
    """From the last line that carries the bank's own balance; else opening + lines."""
    last = db.scalar(select(BankTxn).where(BankTxn.bank_account_id == acct.id, BankTxn.balance_paise.is_not(None))
                     .order_by(BankTxn.txn_date.desc(), BankTxn.created_at.desc()).limit(1))
    if last is not None:
        later = db.scalar(select(func.sum(BankTxn.amount_paise)).where(
            BankTxn.bank_account_id == acct.id, BankTxn.txn_date > last.txn_date)) or 0
        return last.balance_paise + int(later)
    total = db.scalar(select(func.sum(BankTxn.amount_paise)).where(BankTxn.bank_account_id == acct.id))
    return None if total is None and not acct.opening_date else acct.opening_paise + int(total or 0)


def summary(db: Session, acct: BankAccount, new: list[BankTxn], st: dict, created: bool) -> dict:
    by: dict = {}
    for t in new:
        k = t.kind or ("other_in" if t.amount_paise > 0 else "other_out")
        if t.status in ("matched",):
            x = by.setdefault(k, {"kind": k, "label": KIND_LABEL.get(k, k), "count": 0, "in_paise": 0, "out_paise": 0})
            x["count"] += 1
            x["in_paise" if t.amount_paise > 0 else "out_paise"] += abs(t.amount_paise)
    check = [t for t in new if t.status in ("suggested", "unmatched")]
    lo, hi = st.get("period_from"), st.get("period_to")
    closing_ok = None
    if st.get("closing_paise") is not None:
        b = balance(db, acct)
        closing_ok = b is not None and abs(b - st["closing_paise"]) <= 100
    return {
        "account": account_out(db, acct), "account_added": created,
        "period_from": lo, "period_to": hi, "lines_read": len(st["rows"]), "lines_new": len(new),
        "money_in_paise": sum(t.amount_paise for t in new if t.amount_paise > 0),
        "money_out_paise": -sum(t.amount_paise for t in new if t.amount_paise < 0),
        "matched": sum(1 for t in new if t.status == "matched"), "to_check": len(check),
        "groups": sorted(by.values(), key=lambda x: -(x["in_paise"] + x["out_paise"])),
        "check": [txn_out(t) for t in check[:50]],
        "missing": [m | {"date": m["date"]} for m in (missing_money(db, acct, lo, hi) if lo and hi else [])],
        "closing_paise": st.get("closing_paise"), "closing_matches": closing_ok,
    }


def account_out(db: Session, a: BankAccount) -> dict:
    open_ = db.scalar(select(func.count()).select_from(BankTxn).where(BankTxn.bank_account_id == a.id, BankTxn.status.in_(("suggested", "unmatched")))) or 0
    last = db.scalar(select(func.max(BankTxn.txn_date)).where(BankTxn.bank_account_id == a.id))
    return {"id": str(a.id), "name": a.name, "bank": a.bank, "last4": a.last4, "outlet_ids": a.outlet_ids, "opening_paise": a.opening_paise,
            "opening_date": a.opening_date, "tally_ledger": a.tally_ledger, "balance_paise": balance(db, a), "to_check": int(open_),
            "last_line": last}


def txn_out(t: BankTxn) -> dict:
    return {"id": str(t.id), "date": t.txn_date, "narration": t.narration, "ref": t.ref, "amount_paise": t.amount_paise,
            "balance_paise": t.balance_paise, "status": t.status, "kind": t.kind, "kind_label": KIND_LABEL.get(t.kind or "", None),
            "category": t.category, "supplier_id": str(t.supplier_id) if t.supplier_id else None, "note": t.note,
            "matched_by": t.matched_by, "account_id": str(t.bank_account_id)}


def set_kind(db: Session, t: BankTxn, kind: str, user_id, category: str | None = None, supplier_id=None,
             other_account_id=None, remember: bool = True) -> int:
    """The person says what a line is. Remembered for similar lines (and applied to the open ones now). Returns how many
    other open lines this also sorted."""
    from .match import link_supplier
    if kind == "supplier":
        if not supplier_id:
            raise ValueError("Choose the supplier")
        link_supplier(db, t, supplier_id, user_id)
    else:
        t.kind, t.status, t.matched_by = kind, "matched", "person"
        t.category = (category or "").strip()[:40] or None
        t.other_account_id = other_account_id
        t.supplier_id = t.supplier_entry_id = None
        t.note = None
    t.matched_by = "person"
    if t.status != "matched":
        t.status = "matched"
    more = 0
    pat = words(t.narration)
    if remember and pat and kind not in ("sales", "transfer"):
        direction = "in" if t.amount_paise > 0 else "out"
        r = db.scalar(select(BankRule).where(BankRule.pattern == pat, BankRule.direction == direction))
        if r is None:
            r = BankRule(tenant_id=t.tenant_id, pattern=pat[:80], direction=direction, kind=kind)
            db.add(r)
        r.kind, r.category, r.supplier_id = kind, t.category, supplier_id if kind == "supplier" else None
        db.flush()
        for o in db.scalars(select(BankTxn).where(BankTxn.status.in_(("suggested", "unmatched")), BankTxn.id != t.id)):
            if (o.amount_paise > 0) == (direction == "in") and words(o.narration) == pat:
                if kind == "supplier":
                    link_supplier(db, o, supplier_id, user_id, by="rule")
                else:
                    o.kind, o.status, o.matched_by, o.category = kind, "matched", "rule", t.category
                more += 1
    db.flush()
    return more


# ------------------------------------------------------------------ WhatsApp

def _r(p: int | None) -> str:
    if p is None:
        return "—"
    v = abs(p) / 100
    return ("-" if p < 0 else "") + "₹" + (f"{v:,.0f}" if v == int(v) else f"{v:,.2f}")


def _dd(d) -> str:
    d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    return f"{d.day} {d.strftime('%b')}"


def summary_text(s: dict) -> str:
    a = s["account"]
    per = f"{_dd(s['period_from'])} – {_dd(s['period_to'])}" if s.get("period_from") else ""
    out = [f"🏦 *{a['name']}* {per}".strip()]
    if s["account_added"]:
        out.append("(New account, added)")
    if s["lines_new"] == 0:
        out.append("Already had every line of this statement.")
        return "\n".join(out)
    out.append(f"In {_r(s['money_in_paise'])} · Out {_r(s['money_out_paise'])}")
    out.append(f"✅ Sorted {s['matched']} of {s['lines_new']}" + (f" · {s['to_check']} to check" if s["to_check"] else ""))
    for g in s["groups"][:5]:
        out.append(f"• {g['label']}: {_r(g['in_paise'] or g['out_paise'])}")
    for m in s["missing"][:3]:
        out.append(f"⚠️ {m['mode'].upper()} sales of {_dd(m['date'])} {_r(m['sales_paise'])}: {_r(m['short_paise'])} not in the bank")
    if a["balance_paise"] is not None:
        out.append(f"Balance {_r(a['balance_paise'])}" + ("" if s.get("closing_matches") in (None, True) else " (doesn't match the statement)"))
    return "\n".join(out)


def check_text(t: BankTxn) -> str:
    sign = "in" if t.amount_paise > 0 else "out"
    hint = f"\n_{t.note}_" if t.note else ""
    return f"{_dd(t.txn_date)} · {_r(abs(t.amount_paise))} {sign}\n{t.narration[:90]}{hint}"


def balances_text(db: Session) -> str:
    accts = db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.name)).all()
    if not accts:
        return "No bank accounts yet. Send a bank statement (PDF or Excel) to add one."
    lines = ["🏦 *Banks*"]
    total = 0
    for a in accts:
        o = account_out(db, a)
        b = o["balance_paise"]
        total += b or 0
        tail = f" · {o['to_check']} to check" if o["to_check"] else ""
        lines.append(f"• {a.name}: {_r(b)}{tail}" + (f" (till {_dd(o['last_line'])})" if o["last_line"] else ""))
    if len(accts) > 1:
        lines.append(f"Total {_r(total)}")
    return "\n".join(lines)


QUICK_OUT = [("supplier", "Supplier"), ("expense", "Expense"), ("drawing", "Drawing")]
QUICK_IN = [("sales", "Sales"), ("capital", "Owner's money"), ("other_in", "Other")]


def is_kind(k: str) -> bool:
    return k in KIND_LABEL


def fmt_kind(k: str) -> str:
    return KIND_LABEL.get(k, k)


def clean_category(s: str | None) -> str | None:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:40].title() if s else None
