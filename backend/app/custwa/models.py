"""wise-plus: WhatsApp for customers - the bill after paying, and how the visit was."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk


class BillFeedback(Base):
    """A customer's rating of a visit, from the buttons under the WhatsApp bill (5 loved it, 3 okay, 1 not good)."""
    __tablename__ = "bill_feedback"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    bill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bills.id"), nullable=False)
    phone10: Mapped[str] = mapped_column(String(10), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("bill_id"), Index("ix_feedback_outlet", "outlet_id", "created_at"))


class WaOutbox(Base):
    """WhatsApp messages to customers waiting to go out (sent after the bill is saved, retried if Meta is busy).
    Not per restaurant for row security: only the sender loop reads it."""
    __tablename__ = "wa_outbox"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)          # bill
    ref: Mapped[str] = mapped_column(String(40), nullable=False)           # bill id
    to_phone: Mapped[str] = mapped_column(String(15), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(8), default="queued", nullable=False)   # queued | sent | failed
    tries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(300))
    wamid: Mapped[str | None] = mapped_column(String(160))           # Meta's message id, for the delivery ticks
    delivery: Mapped[str | None] = mapped_column(String(9))          # sent | delivered | read | failed (from Meta)
    next_try_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("kind", "ref"), Index("ix_outbox_due", "status", "next_try_at"))
