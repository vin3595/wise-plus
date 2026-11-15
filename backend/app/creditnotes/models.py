"""Credit notes to customers (wise-plus): money given back on a bill, GST-correct, never by editing the bill.

Numbered per outlet and financial year: CN/<CODE>/<yyYY><seq> (16 characters at most, GST rule 46)."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk

REFUND_MODES = ("cash", "upi", "card", "bank", "none")


class CreditNote(Base):
    __tablename__ = "credit_notes"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    bill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False)
    fy_start: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[str] = mapped_column(String(16), nullable=False)
    note_date: Mapped[date] = mapped_column(Date, nullable=False)                # business date it was given
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    lines: Mapped[list] = mapped_column(JSONB, nullable=False)  # [{bill_line_id, item_name, qty, rate_bp, hsn_sac, taxable, cgst, sgst, igst}]
    taxable_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cgst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sgst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    igst_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    round_off_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refund_mode: Mapped[str] = mapped_column(String(5), nullable=False)
    cash_movement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("cash_movements.id"))
    status: Mapped[str] = mapped_column(String(9), default="issued", nullable=False)     # issued | cancelled
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("outlet_id", "fy_start", "seq"), UniqueConstraint("outlet_id", "number"),
                      Index("ix_credit_notes_outlet_date", "outlet_id", "note_date"))
