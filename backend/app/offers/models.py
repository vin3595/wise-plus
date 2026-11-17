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
    require_otp: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)  # cashier checks the customer
    prepaid: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)  # pay online first, then the QR
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
    cust_name: Mapped[str | None] = mapped_column(String(60))                  # their WhatsApp name (or the name on their bills)
    item_name: Mapped[str | None] = mapped_column(String(120))                 # what the code was for, as it was then
    price_paise: Mapped[int | None] = mapped_column(Integer)                   # the offer price it was given at
    regular_paise: Mapped[int | None] = mapped_column(Integer)                 # the item's usual price then (for the offer's ROI)
    prepaid_ref: Mapped[str | None] = mapped_column(String(40))                # Razorpay payment id when it was paid online first
    prepaid_paise: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default="issued", nullable=False)   # issued | used
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # the customer gave the OTP at the counter
    outlet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"))
    kot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("kots.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("code", name="uq_offer_code"), Index("ix_offer_codes_phone", "offer_id", "phone10"))


class Broadcast(Base):
    """A WhatsApp message to a chosen group of customers (marketing template, with a 'Get my offer' button)."""
    __tablename__ = "broadcasts"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    filters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    text: Mapped[str] = mapped_column(String(700), nullable=False)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("offers.id", ondelete="SET NULL"))
    recipients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)          # message credits it used
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OfferPayment(Base):
    """A prepaid offer: the payment link sent on WhatsApp. Once paid, the customer gets their QR (code_id)."""
    __tablename__ = "offer_payments"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"), nullable=False)
    phone10: Mapped[str] = mapped_column(String(10), nullable=False)
    cust_name: Mapped[str | None] = mapped_column(String(60))
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(10), nullable=False)             # razorpay | test
    link_id: Mapped[str | None] = mapped_column(String(40))
    short_url: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(8), default="created", nullable=False)  # created | paid | expired | problem
    payment_id: Mapped[str | None] = mapped_column(String(40))
    fee_paise: Mapped[int | None] = mapped_column(Integer)                        # Razorpay's fee + GST on it
    problem: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    code_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("offer_codes.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_offer_payments_link", "link_id"), Index("ix_offer_payments_phone", "offer_id", "phone10"))
