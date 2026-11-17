"""wise-plus: WhatsApp keyword offers. A customer sends the keyword, gets a personal QR code; the counter scans it
and the item goes on the bill at the offer price. Each code works once."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)              # "Cold Coffee at ₹29"
    keyword: Mapped[str] = mapped_column(String(30), nullable=False)           # "COFFEE29" (one WhatsApp number serves all businesses)
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("variants.id"), nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)          # what the item costs with the code
    outlet_ids: Mapped[list | None] = mapped_column(JSONB)                     # where it can be used; None = everywhere
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    per_phone: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # codes one number can ever get
    code_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)  # each code works for this many days
    daily_limit: Mapped[int | None] = mapped_column(Integer)                   # codes given out per day (None = no limit)
    message: Mapped[str | None] = mapped_column(String(300))                   # extra line in the WhatsApp reply
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("keyword", name="uq_offer_keyword"),)


class OfferCode(Base):
    __tablename__ = "offer_codes"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(12), nullable=False)              # "OF7KQ2M9XA" (in the QR and printed under it)
    phone10: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(8), default="issued", nullable=False)   # issued | used
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outlet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"))
    kot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("kots.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("code", name="uq_offer_code"), Index("ix_offer_codes_phone", "offer_id", "phone10"))
