"""wise-plus: Customers > Feedback - how customers rated their visits from the WhatsApp bill."""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.kds import business_date
from ..core.permissions import Perm
from ..deps import Auth, get_db, require
from ..models import Bill, Outlet
from .models import BillFeedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.get("")
def feedback(outlet_id: uuid.UUID | None = None, days: int = 30, rating: int | None = None,
             auth: Auth = Depends(require(Perm.reports_view)), db: Session = Depends(get_db)) -> dict:
    days = max(1, min(days, 365))
    since = business_date() - timedelta(days=days - 1)
    q = select(BillFeedback, Bill.invoice_no, Bill.total_paise, Bill.business_date, Outlet.name).join(Bill, Bill.id == BillFeedback.bill_id) \
        .join(Outlet, Outlet.id == BillFeedback.outlet_id).where(Bill.business_date >= since)
    if outlet_id:
        q = q.where(BillFeedback.outlet_id == outlet_id)
    rows = db.execute(q.order_by(BillFeedback.created_at.desc())).all()
    counts = {5: 0, 3: 0, 1: 0}
    for fb, *_ in rows:
        counts[fb.rating] = counts.get(fb.rating, 0) + 1
    items = [{"id": str(fb.id), "rating": fb.rating, "comment": fb.comment, "phone": "…" + fb.phone10[-4:], "at": fb.created_at.isoformat(),
              "invoice_no": inv, "total_paise": tot, "date": str(d), "outlet": oname}
             for fb, inv, tot, d, oname in rows if rating is None or fb.rating == rating]
    n = sum(counts.values())
    return {"days": days, "counts": {"loved": counts[5], "okay": counts[3], "bad": counts[1]}, "total": n,
            "happy_pct": round(counts[5] * 100 / n) if n else None, "items": items[:300]}


def outlet_line(db: Session, outlet_id, day) -> str | None:
    """For the nightly summary: the day's ratings at one outlet."""
    rows = db.scalars(select(BillFeedback.rating).join(Bill, Bill.id == BillFeedback.bill_id)
                      .where(BillFeedback.outlet_id == outlet_id, Bill.business_date == day)).all()
    if not rows:
        return None
    return f"   Feedback: 😍 {rows.count(5)} · 🙂 {rows.count(3)} · 😞 {rows.count(1)}"
