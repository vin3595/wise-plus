"""wise-plus: what a WhatsApp offer cost and what it brought in.

Spent: the discount given (usual price - offer price on every code used), Razorpay's fee on pay-first offers,
and WhatsApp broadcasts that carried the offer (message credits at the pack rate).
Brought in: the offer items, everything else on those same bills, and later visits by the same customers."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..billing.plans import MSG_PACKS
from ..models import Bill, StockItem, Variant
from .models import Broadcast, Offer, OfferCode, OfferPayment

PAISE_PER_CREDIT = MSG_PACKS["m1000"]["paise"] / MSG_PACKS["m1000"]["messages"]
RAZORPAY_FEE_BP = 236          # 2% + 18% GST, when Razorpay didn't say


def _unit_cost(db: Session, variant_id) -> int | None:
    """Recipe cost of one at today's average costs (None without a recipe)."""
    try:
        from ..core.stock import recipe_cost, rolled_cost_k
        from ..services import stock as st
        rec = st.recipes(db, "variant", [variant_id]).get(variant_id)
        if not rec:
            return None
        costs = {str(i.id): i.last_cost_k for i in db.scalars(select(StockItem))}
        book, yields, on_sale = st.prep_book(db)
        for sid in book:
            if sid in on_sale or not costs.get(sid):
                costs[sid] = rolled_cost_k(sid, book, yields, {k: v for k, v in costs.items() if k not in on_sale})
        return recipe_cost(rec, costs)
    except Exception:  # noqa: BLE001 - costing is a bonus; the ROI works without it
        return None


def report(db: Session, offer: Offer, d1: date | None = None, d2: date | None = None) -> dict:
    q = select(OfferCode).where(OfferCode.offer_id == offer.id)
    if d1:
        q = q.where(OfferCode.issued_on >= d1)
    if d2:
        q = q.where(OfferCode.issued_on <= d2)
    codes = db.scalars(q).all()
    v = db.get(Variant, offer.variant_id)
    orders = {c.order_id for c in codes if c.status == "used" and c.order_id}
    bills = {b.order_id: b for b in db.scalars(select(Bill).where(Bill.order_id.in_(orders or [None]), Bill.status == "paid"))}
    billed = [c for c in codes if c.status == "used" and c.order_id in bills]

    offer_sales = sum((c.price_paise if c.price_paise is not None else offer.price_paise) for c in billed)
    discount = sum(max(0, (c.regular_paise or (v.price_paise if v else 0)) - (c.price_paise if c.price_paise is not None else offer.price_paise))
                   for c in billed)
    per_bill: dict = defaultdict(int)
    for c in billed:
        per_bill[c.order_id] += c.price_paise if c.price_paise is not None else offer.price_paise
    extra = sum(max(0, bills[o].total_paise - amt) for o, amt in per_bill.items())

    # the same customers coming back later (bills without this offer on them)
    first: dict[str, date] = {}
    for c in billed:
        d = bills[c.order_id].business_date
        if c.phone10 not in first or d < first[c.phone10]:
            first[c.phone10] = d
    later, back, visits, before = 0, set(), 0, set()
    if first:
        for b in db.scalars(select(Bill).where(Bill.customer_phone.in_(list(first)), Bill.status == "paid")):
            if b.order_id in per_bill:
                continue
            if b.business_date < first[b.customer_phone]:
                before.add(b.customer_phone)                 # was a customer before the offer
            elif b.business_date > first[b.customer_phone]:
                later += b.total_paise
                visits += 1
                back.add(b.customer_phone)
    new_people = len(set(first) - before)

    # pay-first offers
    pq = select(OfferPayment).where(OfferPayment.offer_id == offer.id)
    pays = [p for p in db.scalars(pq) if (not d1 or p.created_at.date() >= d1) and (not d2 or p.created_at.date() <= d2)]
    paid = [p for p in pays if p.status == "paid"]
    fees = sum(p.fee_paise if p.fee_paise is not None else (0 if p.provider == "test" else (p.amount_paise * RAZORPAY_FEE_BP + 5000) // 10000)
               for p in paid)
    collected = sum(p.amount_paise for p in paid)
    waiting = [p for p in paid if p.code_id and p.code_id in {c.id for c in codes if c.status == "issued"}]

    bq = select(Broadcast).where(Broadcast.offer_id == offer.id)
    casts = [b for b in db.scalars(bq) if (not d1 or b.created_at.date() >= d1) and (not d2 or b.created_at.date() <= d2)]
    messages = round(sum(b.credits for b in casts) * PAISE_PER_CREDIT)

    unit = _unit_cost(db, offer.variant_id)
    food = unit * len(billed) if unit is not None else None
    spent = discount + fees + messages
    brought = offer_sales + extra + later
    return {
        "offer": offer.name, "keyword": offer.keyword, "prepaid": offer.prepaid, "d1": d1, "d2": d2,
        "codes": {"sent": len(codes), "used": len(billed), "rate_bp": (len(billed) * 10000 // len(codes)) if codes else 0},
        "customers": {"redeemed": len(first), "new": new_people, "came_back": len(back), "later_visits": visits},
        "links": {"sent": len(pays), "paid": len(paid), "collected_paise": collected, "paid_not_collected": len(waiting)} if offer.prepaid else None,
        "spent": {"discount_paise": discount, "razorpay_fees_paise": fees, "messages_paise": messages, "broadcasts": len(casts),
                  "total_paise": spent},
        "sales": {"offer_items_paise": offer_sales, "same_bill_paise": extra, "later_visits_paise": later, "total_paise": brought},
        "food_cost_paise": food, "food_cost_each_paise": unit,
        "return_per_rupee": round(brought / spent, 2) if spent else None,
    }
