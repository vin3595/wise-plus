"""The owner's keyword assistant: short questions, instant answers from the restaurant's own data.

Same answers in the app (Home > Ask) and on WhatsApp. Plain text with WhatsApp's *bold*, kept short enough
to read on a phone. Keywords (English and common Hinglish), anything after them names a party or an item:

  dues | outstanding | balance | baki        all suppliers' outstanding, biggest first
  dues kanha                                 one supplier: balance and last few entries
  last kanha | last order kanha              last bill from that supplier
  price vanilla | rate vanilla               last purchase price of an item, and the one before
  sales | today                              today's sales so far
  help                                       this list
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.kds import business_date
from ..ledger.balances import balances, statement
from ..models import Bill, Purchase, StockItem, Supplier
from ..purchases_ai.match import name_score

HELP = ("*Ask me:*\n"
        "• *dues* – what you owe every supplier\n"
        "• *dues kanha* – one supplier's balance\n"
        "• *last kanha* – last order from a party\n"
        "• *price vanilla* – last price paid for an item\n"
        "• *sales* – today's sales so far")

DUES = {"dues", "due", "outstanding", "balance", "balances", "baki", "baaki", "udhaar", "udhar", "payable", "ledger", "ledgers"}
LAST = {"last", "lastorder", "order", "kab"}
PRICE = {"price", "rate", "cost", "bhav", "daam"}
SALES = {"sales", "sale", "today", "aaj", "bikri"}
HELPS = {"help", "hi", "hello", "menu", "?", "start"}
FILLER = {"order", "from", "of", "for", "the", "party", "ka", "ki", "ke", "se", "what", "is", "was", "when", "did", "we", "last"}


def rs(paise: int) -> str:
    """₹1,23,456 (Indian grouping), paise only when there are some."""
    neg = paise < 0
    p = abs(int(paise))
    r, ps = divmod(p, 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    out = f"₹{s}" + (f".{ps:02d}" if ps else "")
    return f"-{out}" if neg else out


def ago(d: date | None, today: date) -> str:
    if d is None:
        return "never"
    n = (today - d).days
    return "today" if n == 0 else "yesterday" if n == 1 else f"{n} days ago"


def _best(name: str, options: list[tuple[str, object]]) -> tuple[object | None, list[str]]:
    """Best match by name, and close runners-up to offer when it's unclear."""
    scored = sorted(((name_score(name, n), n, o) for n, o in options), key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.5:
        # Also allow a plain substring ("van" -> "Super Vanilla Bulk 5L").
        sub = [(n, o) for n, o in options if name.lower() in n.lower()]
        if len(sub) == 1:
            return sub[0][1], []
        return None, [n for n, _ in sub[:4]] or [n for s, n, _ in scored[:3] if s > 0.25]
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02 and scored[1][0] >= 0.5:
        return None, [n for s, n, _ in scored[:4] if s >= scored[0][0] - 0.1]
    return scored[0][2], []


def _rest(words: list[str]) -> str:
    return " ".join(w for w in words if w not in FILLER).strip()


def dues_all(db: Session, today: date) -> str:
    rows = [b for b in balances(db) if b.balance_paise]
    if not rows:
        return "Nothing outstanding. 🎉 (Add opening balances under Money › Supplier dues if older bills aren't in Wise.)"
    owe = [b for b in rows if b.balance_paise > 0]
    lines = [f"*Supplier dues: {rs(sum(b.balance_paise for b in owe))}*"]
    for b in rows[:15]:
        lines.append(f"• {b.name}: {rs(b.balance_paise)}" + (f" (last paid {ago(b.last_payment_date, today)})" if b.last_payment_date else ""))
    if len(rows) > 15:
        lines.append(f"…and {len(rows) - 15} more")
    adv = [b for b in rows if b.balance_paise < 0]
    if adv:
        lines.append(f"_Minus = you paid in advance._")
    return "\n".join(lines)


def dues_one(db: Session, sup: Supplier, today: date) -> str:
    st = statement(db, sup.id, limit=5)
    lines = [f"*{sup.name}: {rs(st['balance_paise'])} due*"]
    names = {"bill": "Bill", "payment": "Paid", "debit_note": "Debit note", "cash_drawer": "Cash", "opening": "Opening"}
    for e in reversed(st["entries"]):
        amt = e["debit_paise"] or e["credit_paise"]
        sign = "+" if e["debit_paise"] else "−"
        ref = f" {e['ref']}" if e.get("ref") else ""
        lines.append(f"• {e['date'].strftime('%d %b')} {names[e['kind']]}{ref}: {sign}{rs(amt)}")
    return "\n".join(lines)


def last_order(db: Session, sup: Supplier, today: date) -> str:
    p = db.scalar(select(Purchase).where(Purchase.supplier_id == sup.id, Purchase.status == "posted")
                  .order_by(Purchase.invoice_date.desc(), Purchase.created_at.desc()).limit(1))
    if p is None:
        return f"No bills from *{sup.name}* yet."
    lines = [f"*Last order from {sup.name}*", f"{p.invoice_date.strftime('%d %b %Y')} ({ago(p.invoice_date, today)})"
             + (f" · Bill {p.invoice_no}" if p.invoice_no else "") + f" · {rs(p.total_paise)}"]
    for ln in (p.lines or [])[:8]:
        q = ln["qty_m"] / 1000
        lines.append(f"• {ln['name']}: {q:g} {ln['unit']} · {rs(ln['amount_paise'])}")
    n = db.scalar(select(func.count()).select_from(Purchase).where(Purchase.supplier_id == sup.id, Purchase.status == "posted",
                                                                  Purchase.invoice_date >= today - timedelta(days=30)))
    lines.append(f"_{n} bill(s) from them in the last 30 days._")
    return "\n".join(lines)


def last_price(db: Session, item: StockItem, today: date) -> str:
    """Price per purchase unit as bought, before GST, from the last two bills that had this item."""
    hits = []
    for p in db.scalars(select(Purchase).where(Purchase.status == "posted", Purchase.lines.contains([{"stock_item_id": str(item.id)}]))
                        .order_by(Purchase.invoice_date.desc(), Purchase.created_at.desc()).limit(10)):
        for ln in p.lines or []:
            if ln.get("stock_item_id") == str(item.id) and ln.get("qty_m"):
                hits.append((p, ln))
                break
        if len(hits) == 2:
            break
    if not hits:
        return f"*{item.name}* hasn't been bought yet in Wise."
    sups = {s.id: s.name for s in db.scalars(select(Supplier))}

    def each(p, ln) -> tuple[int, str]:
        per = round(ln["amount_paise"] * 1000 / ln["qty_m"])
        return per, ln["unit"]
    p, ln = hits[0]
    per, unit = each(p, ln)
    gst = ln.get("gst_bp", 0) / 100
    lines = [f"*{item.name}*: {rs(per)} per {unit}" + (f" + {gst:g}% GST = {rs(round(per * (1 + gst / 100)))}" if gst else ""),
             f"{sups.get(p.supplier_id, 'No supplier')} · {p.invoice_date.strftime('%d %b %Y')} ({ago(p.invoice_date, today)})"]
    if len(hits) > 1:
        p2, ln2 = hits[1]
        per2, unit2 = each(p2, ln2)
        if unit2 != unit and ln.get("factor") and ln2.get("base_m"):
            # Bought in a different unit last time (litres, then crates): show it in this time's unit.
            per2 = round(ln2["amount_paise"] * ln["factor"] * 1000 / ln2["base_m"])
            note = f" · bought by the {unit2} then"
        else:
            note = ""
        if per2:
            ch = (per - per2) * 100 / per2
            arrow = "▲" if ch > 0.5 else "▼" if ch < -0.5 else "="
            lines.append(f"Before: {rs(per2)} per {unit} on {p2.invoice_date.strftime('%d %b')} ({arrow} {abs(ch):.1f}%){note}")
    return "\n".join(lines)


def sales_today(db: Session) -> str:
    d = business_date()
    n, total = db.execute(select(func.count(), func.coalesce(func.sum(Bill.total_paise), 0))
                          .where(Bill.business_date == d, Bill.status != "cancelled")).one()
    if not n:
        return f"No bills yet today ({d.strftime('%d %b')})."
    return f"*Sales today ({d.strftime('%d %b')})*: {rs(int(total))} from {n} bill(s), average {rs(int(total) // n)}."


def answer(db: Session, text: str, today: date | None = None) -> str:
    today = today or business_date()
    words = re.findall(r"[a-z0-9?]+", (text or "").lower())
    if not words or words[0] in HELPS:
        return HELP
    head, rest = words[0], _rest(words[1:])
    if head == "last" and len(words) > 1 and words[1] in PRICE:      # "last price vanilla"
        head, rest = words[1], _rest(words[2:])
    if head in DUES:
        if not rest:
            return dues_all(db, today)
        sup, maybe = _best(rest, [(s.name, s) for s in db.scalars(select(Supplier).where(Supplier.is_active.is_(True)))])
        return dues_one(db, sup, today) if sup else _unsure("supplier", rest, maybe)
    if head in LAST:
        if not rest:
            return "Which party? e.g. *last kanha*"
        sup, maybe = _best(rest, [(s.name, s) for s in db.scalars(select(Supplier).where(Supplier.is_active.is_(True)))])
        return last_order(db, sup, today) if sup else _unsure("supplier", rest, maybe)
    if head in PRICE:
        if not rest:
            return "Which item? e.g. *price vanilla*"
        it, maybe = _best(rest, [(i.name, i) for i in db.scalars(select(StockItem).where(StockItem.is_active.is_(True)))])
        return last_price(db, it, today) if it else _unsure("item", rest, maybe)
    if head in SALES:
        return sales_today(db)
    return "I didn't get that.\n\n" + HELP


def _unsure(what: str, name: str, maybe: list[str]) -> str:
    if maybe:
        return f"Which {what}?\n" + "\n".join(f"• {m}" for m in maybe)
    return f"No {what} called “{name}”."
