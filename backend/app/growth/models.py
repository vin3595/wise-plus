"""wise-plus growth (selling Ease My Bill itself): leads from the website, demo bookings, the follow-up sequence on
WhatsApp, and restaurants referring restaurants. Platform tables: yours, not any one restaurant's."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk


class Lead(Base):
    __tablename__ = "growth_leads"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    phone10: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    restaurant: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(60))
    outlets: Mapped[int | None] = mapped_column(Integer)
    uses_now: Mapped[str | None] = mapped_column(String(40))       # Petpooja, paper, Excel…
    source: Mapped[str | None] = mapped_column(String(40))         # website, referral, instagram ad, walk-in…
    utm: Mapped[dict | None] = mapped_column(JSONB)
    ref_code: Mapped[str | None] = mapped_column(String(20))
    consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # agreed to WhatsApp messages
    status: Mapped[str] = mapped_column(String(9), default="new", nullable=False)    # new | talking | demo | trial | paid | lost
    demo_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)           # follow-ups already sent
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))       # when the next follow-up is due
    stopped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # asked to stop
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_growth_leads_next", "next_at"),)


class RefCode(Base):
    """A restaurant's own referral code (their link: /start?ref=CODE)."""
    __tablename__ = "growth_ref_codes"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)


class Referral(Base):
    """One restaurant bringing another. Both get a free month when the new one first pays."""
    __tablename__ = "growth_referrals"
    id: Mapped[uuid.UUID] = pk()
    referrer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    referred_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(8), default="signed", nullable=False)   # signed | rewarded
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TrialNudge(Base):
    """Trial check-ins already sent to a new restaurant (so each goes once)."""
    __tablename__ = "growth_trial_nudges"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    sent: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
