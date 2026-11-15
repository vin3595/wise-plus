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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.kds import business_date
from ..ledger.balances import balances, statement
from ..models import Bill, Purchase, StockItem, Supplier
from ..purchases_ai.match import name_score

@dataclass
class Reply:
    """An answer and the taps that follow from it: (command, label). Buttons on WhatsApp, chips in the app."""
    text: str
    options: list[tuple[str, str]] = field(default_factory=list)


HOME = [("dues", "Dues"), ("sales", "Sales today"), ("sheet", "Last daily sheet")]

HELP = ("*Ask me:*\n"
        "• *dues* – what you owe every supplier\n"
        "• *dues kanha* – one supplier's balance\n"
        "• *last kanha* – last order from a party\n"
        "• *price vanilla* – last price paid for an item\n"
        "• *sales* – today's sales so far\n"
        "• *sheet* / *sheet 13 nov* – a daily cash sheet\n"
        "• *expenses* – this month's expenses by head\n"
        "• *summary* – closing summary of all outlets (*nightly on* to get it every night)\n"
        "• Send a photo of a bill or the daily sheet to save it")

DUES = {"dues", "due", "outstanding", "balance", "balances", "baki", "baaki", "udhaar", "udhar", "payable", "ledger", "ledgers"}
LAST = {"last", "lastorder", "order", "kab"}
PRICE = {"price", "rate", "cost", "bhav", "daam"}
SALES = {"sales", "sale", "today", "aaj", "bikri"}
HELPS = {"help", "hi", "hello", "menu", "?", "start"}
SHEET = {"sheet", "sheets", "hisab", "hisaab", "daily"}
EXPENSES = {"expenses", "expense", "kharcha", "kharch", "kharche"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
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


def dues_all(db: Session, today: date) -> Reply:
    rows = [b for b in balances(db) if b.balance_paise]
    if not rows:
        return Reply("Nothing outstanding. 🎉 (Add opening balances under Money › Supplier dues if older bills aren't in Wise.)", HOME[1:])
    owe = [b for b in rows if b.balance_paise > 0]
    lines = [f"*Supplier dues: {rs(sum(b.balance_paise for b in owe))}*"]
    for b in rows[:15]:
        lines.append(f"• {b.name}: {rs(b.balance_paise)}" + (f" (last paid {ago(b.last_payment_date, today)})" if b.last_payment_date else ""))
    if len(rows) > 15:
        lines.append(f"…and {len(rows) - 15} more")
    adv = [b for b in rows if b.balance_paise < 0]
    if adv:
        lines.append(f"_Minus = you paid in advance._")
    return Reply("\n".join(lines), [(f"dues {b.name}", b.name) for b in rows[:10]])


def dues_one(db: Session, sup: Supplier, today: date) -> Reply:
    st = statement(db, sup.id, limit=5)
    lines = [f"*{sup.name}: {rs(st['balance_paise'])} due*"]
    names = {"bill": "Bill", "payment": "Paid", "debit_note": "Debit note", "credit_note": "Credit note", "cash_drawer": "Cash", "opening": "Opening"}
    for e in reversed(st["entries"]):
        amt = e["debit_paise"] or e["credit_paise"]
        sign = "+" if e["debit_paise"] else "−"
        ref = f" {e['ref']}" if e.get("ref") else ""
        lines.append(f"• {e['date'].strftime('%d %b')} {names[e['kind']]}{ref}: {sign}{rs(amt)}")
    return Reply("\n".join(lines), [(f"last {sup.name}", "Last order"), ("dues", "All dues")])


def last_order(db: Session, sup: Supplier, today: date) -> Reply:
    p = db.scalar(select(Purchase).where(Purchase.supplier_id == sup.id, Purchase.status == "posted")
                  .order_by(Purchase.invoice_date.desc(), Purchase.created_at.desc()).limit(1))
    if p is None:
        return Reply(f"No bills from *{sup.name}* yet.", [(f"dues {sup.name}", "Their dues")])
    lines = [f"*Last order from {sup.name}*", f"{p.invoice_date.strftime('%d %b %Y')} ({ago(p.invoice_date, today)})"
             + (f" · Bill {p.invoice_no}" if p.invoice_no else "") + f" · {rs(p.total_paise)}"]
    for ln in (p.lines or [])[:8]:
        q = ln["qty_m"] / 1000
        lines.append(f"• {ln['name']}: {q:g} {ln['unit']} · {rs(ln['amount_paise'])}")
    n = db.scalar(select(func.count()).select_from(Purchase).where(Purchase.supplier_id == sup.id, Purchase.status == "posted",
                                                                  Purchase.invoice_date >= today - timedelta(days=30)))
    lines.append(f"_{n} bill(s) from them in the last 30 days._")
    opts = [(f"dues {sup.name}", "Their dues")] + [(f"price {ln['name']}", f"Price: {ln['name']}") for ln in (p.lines or [])[:9]]
    return Reply("\n".join(lines), opts)


def last_price(db: Session, item: StockItem, today: date) -> Reply:
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
        return Reply(f"*{item.name}* hasn't been bought yet in Wise.", HOME)
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
    sup = sups.get(p.supplier_id)
    return Reply("\n".join(lines), [(f"last {sup}", "Last order"), (f"dues {sup}", "Their dues")] if sup else HOME)


def _when(words: list[str], today: date) -> date | None:
    """'13 nov', '13/11', '13-11-2026', 'yesterday', 'today' -> a date."""
    t = " ".join(words)
    if "yesterday" in t or "kal" in words:
        return today - timedelta(days=1)
    if "today" in t or "aaj" in words:
        return today
    m = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", t)
    if m:
        y = int(m.group(3)) if m.group(3) else today.year
        try:
            return date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*([a-z]{3})", t)
    if m and m.group(2) in MONTHS:
        try:
            return date(today.year, MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    return None


def daily_sheet(db: Session, words: list[str], today: date) -> Reply:
    from ..daily_sheet.models import DailySheet
    from ..daily_sheet.service import summary as sheet_summary
    want = _when(words, today)
    q = select(DailySheet).where(DailySheet.status == "saved")
    if want:
        q = q.where(DailySheet.sheet_date == want)
    s = db.scalar(q.order_by(DailySheet.sheet_date.desc(), DailySheet.updated_at.desc()).limit(1))
    if s is None:
        return Reply((f"No daily sheet saved for {want.strftime('%d %b')}." if want else "No daily sheet saved yet.")
                     + " Send a photo of the sheet to save it.", [("sheet", "Last sheet")] if want else [])
    prev = s.sheet_date - timedelta(days=1)
    return Reply(sheet_summary(s.data, s.sheet_date), [(f"sheet {prev.strftime('%d %b')}", f"{prev.strftime('%d %b')} sheet"), ("expenses", "Month's expenses")])


def expenses(db: Session, words: list[str], today: date) -> Reply:
    """Expenses written on the daily sheets, by head, for a month (default this one)."""
    from ..daily_sheet.models import DailySheet
    from ..daily_sheet.service import PAYOUTS, rows
    t = " ".join(words)
    mon = next((MONTHS[w[:3]] for w in words if w[:3] in MONTHS), None)
    y = today.year
    if "last" in t or "pichla" in t:
        mon = today.month - 1 or 12
        y = today.year - (today.month == 1)
    mon = mon or today.month
    if mon > today.month and y == today.year:
        y -= 1
    start = date(y, mon, 1)
    end = date(y + (mon == 12), mon % 12 + 1, 1)
    heads: dict[str, float] = {}
    family: dict[str, float] = {}
    days = 0
    for s in db.scalars(select(DailySheet).where(DailySheet.status == "saved", DailySheet.sheet_date >= start, DailySheet.sheet_date < end)):
        days += 1
        for n, a in rows((s.data or {}).get("expenses")) + rows((s.data or {}).get("expense_payouts")):
            bucket = family if n.upper() in PAYOUTS else heads
            bucket[n.title()] = bucket.get(n.title(), 0) + a
    label = start.strftime("%B %Y")
    if not days:
        return Reply(f"No daily sheets saved for {label} yet.", [("sheet", "Last sheet")])
    total = sum(heads.values())
    lines = [f"*Expenses, {label}: {rs(round(total * 100))}* ({days} sheet(s))"]
    for n, a in sorted(heads.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"• {n}: {rs(round(a * 100))}")
    if family:
        lines.append("To family: " + ", ".join(f"{n} {rs(round(a * 100))}" for n, a in family.items()))
    other = "expenses last month" if mon == today.month else "expenses"
    return Reply("\n".join(lines), [(other, "Last month" if mon == today.month else "This month"), ("sheet", "Last sheet")])


def sales_today(db: Session) -> Reply:
    d = business_date()
    n, total = db.execute(select(func.count(), func.coalesce(func.sum(Bill.total_paise), 0))
                          .where(Bill.business_date == d, Bill.status != "cancelled")).one()
    if not n:
        return Reply(f"No bills yet today ({d.strftime('%d %b')}).", HOME[:1])
    return Reply(f"*Sales today ({d.strftime('%d %b')})*: {rs(int(total))} from {n} bill(s), average {rs(int(total) // n)}.", HOME[:1])


def answer(db: Session, text: str, today: date | None = None) -> str:
    return answer_rich(db, text, today).text


def answer_rich(db: Session, text: str, today: date | None = None) -> Reply:
    today = today or business_date()
    words = re.findall(r"[a-z0-9?]+", (text or "").lower())
    if not words or words[0] in HELPS:
        return Reply(HELP, HOME)
    head, rest = words[0], _rest(words[1:])
    if head == "last" and len(words) > 1 and words[1] in PRICE:      # "last price vanilla"
        head, rest = words[1], _rest(words[2:])
    if head in DUES:
        if not rest:
            return dues_all(db, today)
        sup, maybe = _best(rest, [(s.name, s) for s in db.scalars(select(Supplier).where(Supplier.is_active.is_(True)))])
        return dues_one(db, sup, today) if sup else _unsure("supplier", rest, maybe, "dues")
    if head in LAST:
        sups = db.scalars(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name)).all()
        if not rest:
            return Reply("Which party? e.g. *last kanha*", [(f"last {x.name}", x.name) for x in sups[:10]])
        sup, maybe = _best(rest, [(x.name, x) for x in sups])
        return last_order(db, sup, today) if sup else _unsure("supplier", rest, maybe, "last")
    if head in PRICE:
        if not rest:
            return Reply("Which item? e.g. *price vanilla*")
        it, maybe = _best(rest, [(i.name, i) for i in db.scalars(select(StockItem).where(StockItem.is_active.is_(True)))])
        return last_price(db, it, today) if it else _unsure("item", rest, maybe, "price")
    if head in SALES:
        return sales_today(db)
    if head in SHEET:
        return daily_sheet(db, words[1:], today)
    if head in EXPENSES:
        return expenses(db, words[1:], today)
    from ..stockplus.nightly import SUMMARY, summary_text   # wise-plus: closing summary (also the nightly message)
    if head in SUMMARY and (len(words) == 1 or words[1] not in {"on", "off"}):
        day = (_when(words[1:], today) if len(words) > 1 else None) or today
        if head == "show" and datetime.now(ZoneInfo("Asia/Kolkata")).hour < 12:   # last night's message, tapped this morning
            day = today - timedelta(days=1)
        return summary_text(db, day)
    return Reply("I didn't get that.\n\n" + HELP, HOME)


def _unsure(what: str, name: str, maybe: list[str], cmd: str) -> Reply:
    if maybe:
        return Reply(f"Which {what}?\n" + "\n".join(f"• {m}" for m in maybe), [(f"{cmd} {m}", m) for m in maybe])
    return Reply(f"No {what} called “{name}”.", HOME)
