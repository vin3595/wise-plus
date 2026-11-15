"""wise-plus: tomorrow's order for things bought fresh every day (bread, buns, brownie, pizza base).

Instead of a minimum level, the order is what the outlet usually uses on that weekday (the same weekday
over the last four weeks when there is enough history, otherwise the last 7 days), plus 10%, less what
is already in stock."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.kds import business_date
from ..models import StockItem, StockMove

USE = ("sale", "prep_out", "waste", "xfer_out")


def daily_use(db: Session, outlet_id: uuid.UUID, item_id: uuid.UUID, today: date, days: int = 28) -> dict[date, int]:
    rows = db.execute(select(StockMove.business_date, func.sum(StockMove.qty_m)).where(
        StockMove.outlet_id == outlet_id, StockMove.stock_item_id == item_id, StockMove.kind.in_(USE),
        StockMove.business_date >= today - timedelta(days=days), StockMove.business_date < today)
        .group_by(StockMove.business_date)).all()
    return defaultdict(int, {d: -int(q) for d, q in rows if q and q < 0})


def tomorrow_need(db: Session, outlet_id: uuid.UUID, item: StockItem, on_hand_m: int, today: date | None = None) -> dict:
    today = today or business_date()
    for_day = today + timedelta(days=1)
    use = daily_use(db, outlet_id, item.id, today + timedelta(days=1))   # includes today's use so far
    same = [use[d] for d in (for_day - timedelta(weeks=w) for w in range(1, 5)) if use.get(d)]
    if len(same) >= 2:
        avg, basis = sum(same) // len(same), f"usual {for_day.strftime('%A')}"
    else:
        week = [use.get(today - timedelta(days=k), 0) for k in range(0, 7)]
        avg, basis = sum(week) // 7, "last 7 days"
    want = avg + avg // 10
    return {"for": str(for_day), "usual_m": avg, "basis": basis, "order_m": max(want - max(on_hand_m, 0), 0)}
