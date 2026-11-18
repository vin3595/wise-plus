"""wise-plus: petty expenses on WhatsApp.

Type it:  "kharcha milk 300 6L", "kharcha ice 120", "kharcha auto 50 counter", "kharcha milkman" (a regular vendor)
Snap it:  a photo of loose slips (caption "kharcha" if it's not obvious). Saved straight away, with an Undo button."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..assistant.answers import Reply
from ..core.permissions import Perm, Role
from ..expenses import service as svc
from ..expenses.models import PAID_LABEL, ExpenseVendor, PettyExpense
from ..models import Outlet
from ..services.role_perms import effective

HEADS = ("kharcha", "kharch", "kharche", "expense", "exp", "petty")
UNITS = {"l": "L", "ltr": "L", "litre": "L", "liter": "L", "lt": "L", "ml": "ml", "kg": "kg", "kgs": "kg", "g": "g", "gm": "g",
         "pc": "pc", "pcs": "pc", "dozen": "dozen", "dz": "dozen", "packet": "packet", "pkt": "packet", "block": "block",
         "silli": "block", "can": "can", "jar": "can", "crate": "crate"}
FROM_WORDS = {"counter": "counter", "galla": "counter", "upi": "upi", "bank": "upi", "gpay": "upi", "paytm": "upi", "phonepe": "upi",
              "owner": "owner", "pocket": "owner", "jeb": "owner", "petty": "petty"}
SYN = {"doodh": "Milk", "dudh": "Milk", "barf": "Ice", "baraf": "Ice", "sabzi": "Vegetables", "sabji": "Vegetables", "phal": "Fruits",
       "auto": "Transport", "rickshaw": "Transport", "kiraya": "Transport", "cylinder": "Gas", "paani": "Water", "pani": "Water",
       "safai": "Cleaning"}


def is_petty(word: str) -> bool:
    return word.split(" ", 1)[0] in HEADS and len(word.split()) > 1


def caption_says_petty(caption: str | None) -> bool:
    return bool(re.search(r"\b(kharcha|kharch|kharche|petty|expenses?|slips?|parchi)\b", caption or "", re.I))


def _can(chosen) -> bool:
    return Perm.billing in effective(uuid.UUID(chosen[0]), Role(chosen[2]))


def outlet_for(db: Session, words: set[str]) -> Outlet | None:
    outs = db.scalars(select(Outlet).where(Outlet.is_active.is_(True)).order_by(Outlet.created_at)).all()
    for o in outs:
        if set(re.findall(r"[a-z0-9]+", o.name.lower())) & words:
            return o
    return outs[0] if outs else None


def parse(text: str) -> dict:
    """'kharcha milk 300 6L counter' → amount, category, qty, unit, paid_from, other words."""
    raw = text.lower().replace("₹", " ").split()[1:]
    toks = []
    for i, t in enumerate(raw):          # "6 ltr" → "6ltr" when the next word is a unit
        if toks and re.fullmatch(r"\d+(?:\.\d+)?", toks[-1]) and t in UNITS and not re.fullmatch(r"\d+(?:\.\d+)?[a-z]+", toks[-1]):
            toks[-1] += t
        else:
            toks.append(t)
    amount, qty, unit, frm, words = None, None, None, None, []
    for t in toks:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", t)
        if m and m.group(2) in UNITS:
            qty, unit = float(m.group(1)), UNITS[m.group(2)]
        elif m and m.group(2) in ("rs", "rupees", "rupaye"):
            amount = float(m.group(1))
        elif re.fullmatch(r"\d+(?:\.\d+)?", t):
            if amount is None:
                amount = float(t)
            elif qty is None:
                qty = float(t)
        elif t in UNITS and qty is not None and unit is None:
            unit = UNITS[t]
        elif t in FROM_WORDS:
            frm = FROM_WORDS[t]
        elif t not in ("rs", "₹", "-", "of", "ka", "ki", "ke", "for"):
            words.append(t.strip("₹.,"))
    words = [w for w in words if w]
    cat = SYN.get(words[0], words[0].title()) if words else None
    return {"amount": amount, "qty": qty, "unit": unit, "from": frm, "category": cat, "words": words}


def _undo_btn(ids: list[uuid.UUID]) -> tuple[str, str]:
    return (f"pu {','.join(i.hex[:12] for i in ids)}"[:200], "Undo")


def _line(x: PettyExpense) -> str:
    q = svc.qty_text(x.qty_m, x.unit)
    return f"₹{x.amount_paise / 100:,.0f} · {x.category}{f' · {q}' if q else ''}{f' ({x.what})' if x.what and x.what.lower() != x.category.lower() else ''}"


def _wallet_line(db: Session, outlet: Outlet, used_petty: bool) -> str:
    if not used_petty:
        return ""
    w = svc.wallet(db, outlet.id)
    return f"\nPetty cash left at {outlet.name}: ₹{w['in_hand_paise'] / 100:,.0f}"


def handle(db: Session, chosen, word: str, text: str) -> Reply | None:
    """Typed kharcha, the Undo button. None if it isn't ours."""
    from fastapi import HTTPException
    try:
        return _handle(db, chosen, word, text)
    except HTTPException as e:
        return Reply(f"⚠️ {e.detail}")


def _handle(db: Session, chosen, word: str, text: str) -> Reply | None:
    if word.startswith("pu "):
        return undo(db, chosen, word[3:])
    if not is_petty(word):
        return None
    if not _can(chosen):
        return Reply("Your account can't add expenses. Ask the owner to allow it under Staff › Who can do what.")
    p = parse(text)
    ws = set(p["words"])
    outlet = outlet_for(db, ws)
    if outlet is None:
        return Reply("Add an outlet in the app first (Setup › Outlets).")
    ws -= set(re.findall(r"[a-z0-9]+", outlet.name.lower()))
    words = [w for w in p["words"] if w in ws]          # without the outlet's name
    p["category"] = (SYN.get(words[0], words[0].title()) if words else None)
    # a regular vendor by name: "kharcha milkman" (or with a different amount)
    vend = next((v for v in db.scalars(select(ExpenseVendor).where(ExpenseVendor.is_active.is_(True)))
                 if v.outlet_id in (None, outlet.id) and set(re.findall(r"[a-z0-9]+", v.name.lower())) & ws), None)
    if vend is not None:
        amt = int(round(p["amount"] * 100)) if p["amount"] else vend.amount_paise
        qty = int(round(p["qty"] * 1000)) if p["qty"] else (round(vend.qty_m * amt / vend.amount_paise) if vend.qty_m else None)
        x = svc.add(db, uuid.UUID(chosen[0]), uuid.UUID(chosen[3]), svc.New(
            outlet_id=outlet.id, amount_paise=amt, category=vend.category, what=vend.what or vend.name, qty_m=qty, unit=vend.unit,
            stock_item_id=vend.stock_item_id, paid_from=p["from"] or vend.paid_from, vendor_id=vend.id, source="whatsapp"))
        return Reply(f"✅ {vend.name}: {_line(x)} · {PAID_LABEL[x.paid_from]}{_wallet_line(db, outlet, x.paid_from == 'petty')}", [_undo_btn([x.id])])
    if not p["amount"] and not p["qty"]:
        return None                                   # "kharcha last month": the expenses report
    if not p["amount"] or not p["category"]:
        return Reply("Write it like: *kharcha milk 300 6L*\n(amount, what, and the quantity if you know it). "
                     "Add *counter*, *upi* or *owner* if it wasn't from petty cash.")
    amt = int(round(p["amount"] * 100))
    qty = int(round(p["qty"] * 1000)) if p["qty"] else None
    unit = p["unit"]
    guessed = False
    if qty is None:
        qty, unit = svc.guess_qty(db, p["category"], amt, outlet.id)
        guessed = bool(qty)
    item = svc.item_for(db, p["category"])
    if item is not None and qty and svc.factor(item, unit) is None:
        item = None                                   # a unit the stock item isn't counted in: keep the expense, skip stock
    what = " ".join(words[1:]) or None
    x = svc.add(db, uuid.UUID(chosen[0]), uuid.UUID(chosen[3]), svc.New(
        outlet_id=outlet.id, amount_paise=amt, category=p["category"], what=what, qty_m=qty, unit=unit,
        stock_item_id=item.id if item else None, paid_from=p["from"] or "petty", source="whatsapp"))
    tail = " (same rate as last time)" if guessed else ""
    stock = f"\n📦 {item.name} stock +{svc.qty_text(qty, unit)}" if item and qty else ""
    return Reply(f"✅ {_line(x)}{tail} · {PAID_LABEL[x.paid_from]}{stock}{_wallet_line(db, outlet, x.paid_from == 'petty')}",
                 [_undo_btn([x.id])])


def undo(db: Session, chosen, ids: str) -> Reply:
    hexes = [h for h in ids.split(",") if re.fullmatch(r"[0-9a-f]{12}", h)]
    since = datetime.now(timezone.utc) - timedelta(hours=6)
    rows = [x for x in db.scalars(select(PettyExpense).where(PettyExpense.created_by == uuid.UUID(chosen[3]), PettyExpense.created_at >= since,
                                                             PettyExpense.status == "ok"))
            if x.id.hex[:12] in hexes]
    if not rows:
        return Reply("Nothing to undo (only your own entries from the last few hours).")
    for x in rows:
        svc.cancel(db, x, uuid.UUID(chosen[3]), "Undone on WhatsApp")
    return Reply(f"↩️ Undone: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}, ₹{sum(x.amount_paise for x in rows) / 100:,.0f}.")


def save_slips(db: Session, chosen, outlet: Outlet, drafts: list[dict], path: str, phash: str, already: PettyExpense | None) -> Reply:
    """A photo of slips, read: saved from petty cash straight away, with Undo."""
    if already is not None:
        return Reply(f"This slip photo was already saved on {already.spent_on:%d %b} (₹{already.amount_paise / 100:,.0f} · {already.category}).")
    made = []
    for d in drafts:
        item = svc.item_for(db, d["category"])
        if item is not None and d.get("qty_m") and svc.factor(item, d.get("unit")) is None:
            item = None
        made.append(svc.add(db, uuid.UUID(chosen[0]), uuid.UUID(chosen[3]), svc.New(
            outlet_id=outlet.id, amount_paise=d["amount_paise"], category=d["category"], what=d.get("what"), qty_m=d.get("qty_m"),
            unit=d.get("unit"), stock_item_id=item.id if item else None, spent_on=__import__("datetime").date.fromisoformat(d["spent_on"]),
            paid_from="petty", file_path=path, photo_hash=phash, source="whatsapp")))
    total = sum(x.amount_paise for x in made)
    lines = "\n".join("• " + _line(x) for x in made)
    return Reply(f"🧾 Saved {len(made)} slip{'s' if len(made) != 1 else ''} · *₹{total / 100:,.0f}* from petty cash ({outlet.name})\n{lines}"
                 f"{_wallet_line(db, outlet, True)}\n\nWrong? Undo, or fix it in the app under Money › Petty expenses.",
                 [_undo_btn([x.id for x in made])])
