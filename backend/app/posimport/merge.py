"""Imported POS sales, summed the same way reports sum EMB's own bills, so reports can add them in."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .models import ImportedBill, ImportedLine

TAX = ImportedBill.cgst_paise + ImportedBill.sgst_paise + ImportedBill.igst_paise


def paid(outlets: list[uuid.UUID], d1: date, d2: date):
    return and_(ImportedBill.outlet_id.in_(outlets), ImportedBill.business_date >= d1,
                ImportedBill.business_date <= d2, ImportedBill.status == "paid")


def count(db: Session, outlets, d1, d2) -> int:
    return db.scalar(select(func.count()).select_from(ImportedBill).where(paid(outlets, d1, d2))) or 0


def totals(db: Session, outlets, d1, d2) -> tuple[int, ...]:
    """bills, gross, discount, taxable, cgst, sgst, igst, round off, total."""
    r = db.execute(select(
        func.count(), *(func.coalesce(func.sum(c), 0) for c in (
            ImportedBill.gross_paise, ImportedBill.discount_paise, ImportedBill.taxable_paise, ImportedBill.cgst_paise,
            ImportedBill.sgst_paise, ImportedBill.igst_paise, ImportedBill.round_off_paise, ImportedBill.total_paise)))
        .where(paid(outlets, d1, d2))).one()
    return tuple(int(x) for x in r)


def by_day(db: Session, outlets, d1, d2):
    """date -> (bills, gross, discount, taxable, tax, round off, total)."""
    return {d: tuple(int(x) for x in rest) for d, *rest in db.execute(
        select(ImportedBill.business_date, func.count(), func.sum(ImportedBill.gross_paise),
               func.sum(ImportedBill.discount_paise), func.sum(ImportedBill.taxable_paise), func.sum(TAX),
               func.sum(ImportedBill.round_off_paise), func.sum(ImportedBill.total_paise))
        .where(paid(outlets, d1, d2)).group_by(ImportedBill.business_date))}


def by_outlet(db: Session, outlets, d1, d2):
    """outlet id -> (bills, discount, tax, total)."""
    return {o: tuple(int(x) for x in rest) for o, *rest in db.execute(
        select(ImportedBill.outlet_id, func.count(), func.sum(ImportedBill.discount_paise), func.sum(TAX),
               func.sum(ImportedBill.total_paise)).where(paid(outlets, d1, d2)).group_by(ImportedBill.outlet_id))}


def by_mode(db: Session, outlets, d1, d2, per_day: bool = False):
    """mode -> (bills, paise), or (date, mode) -> paise. Payments are kept as {mode: paise} on each bill."""
    out: dict = {}
    for d, pays in db.execute(select(ImportedBill.business_date, ImportedBill.payments).where(paid(outlets, d1, d2))):
        for m, a in (pays or {}).items():
            if per_day:
                out[(d, m)] = out.get((d, m), 0) + int(a)
            else:
                n, t = out.get(m, (0, 0))
                out[m] = (n + 1, t + int(a))
    return out


def lines(db: Session, outlets, d1, d2, *group):
    """Item lines grouped by the given columns, with qty, gross, discount, taxable, tax summed."""
    return db.execute(select(*group, func.sum(ImportedLine.qty), func.sum(ImportedLine.gross_paise),
                             func.sum(ImportedLine.discount_paise), func.sum(ImportedLine.taxable_paise),
                             func.sum(ImportedLine.tax_paise))
                      .join(ImportedBill, ImportedBill.id == ImportedLine.bill_id).where(paid(outlets, d1, d2))
                      .group_by(*group)).all()


def by_rate(db: Session, outlets, d1, d2):
    """GST by rate. Bills imported without items count at the rate their tax works out to."""
    from .parse import nearest_rate
    out: dict[int, list[int]] = {}
    for r, tx, tax in db.execute(select(ImportedLine.rate_bp, func.sum(ImportedLine.taxable_paise), func.sum(ImportedLine.tax_paise))
                                 .join(ImportedBill, ImportedBill.id == ImportedLine.bill_id)
                                 .where(paid(outlets, d1, d2)).group_by(ImportedLine.rate_bp)):
        a = out.setdefault(int(r), [0, 0])
        a[0] += int(tx)
        a[1] += int(tax)
    has_lines = select(ImportedLine.id).where(ImportedLine.bill_id == ImportedBill.id).exists()
    for tx, tax in db.execute(select(ImportedBill.taxable_paise, TAX).where(paid(outlets, d1, d2), ~has_lines)):
        a = out.setdefault(nearest_rate(int(tax), int(tx)), [0, 0])
        a[0] += int(tx)
        a[1] += int(tax)
    return out   # rate -> [taxable, tax]; CGST/SGST are half each (imported sales are within the state)
