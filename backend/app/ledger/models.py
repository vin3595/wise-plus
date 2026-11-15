"""Supplier ledger (wise-plus): what is owed to each supplier.

Balance = opening balance + bills (posted purchases) - payments - debit notes - cash paid from a drawer
to that supplier. Bills come from Wise's purchases table; this table holds everything else."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk

KINDS = ("opening", "payment", "debit_note")
MODES = ("cash", "upi", "bank", "cheque", "card", "other")


class SupplierEntry(Base):
    __tablename__ = "supplier_entries"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("suppliers.id"), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)            # opening | payment | debit_note
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)    # opening: what we owed then (negative = they owed us)
    mode: Mapped[str | None] = mapped_column(String(6))                      # payments: cash | upi | bank | cheque | card | other
    reference: Mapped[str | None] = mapped_column(String(60))                # UTR, cheque no., debit note no.
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bank_accounts.id"))  # paid from
    note: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(9), default="posted", nullable=False)   # posted | cancelled
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("kind in ('opening','payment','debit_note')", name="ck_supplier_entry_kind"),
        CheckConstraint("kind = 'opening' or amount_paise > 0", name="ck_supplier_entry_amount"),
        Index("ix_supplier_entries_supplier", "supplier_id", "entry_date"),
    )
