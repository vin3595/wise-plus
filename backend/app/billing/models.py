"""wise-plus billing: what each business pays for, through Razorpay."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base


class Subscription(Base):
    """One row per business. Not per-restaurant data (no row-level policy): only billing code touches it."""
    __tablename__ = "billing_subscriptions"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True)
    modules: Mapped[list | None] = mapped_column(JSONB)          # services paid for
    period: Mapped[str | None] = mapped_column(String(8))        # monthly | yearly | founding (first 50: ₹4,999 first year)
    founding: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rz_subscription_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    # checkout started but not finished: {subscription_id, modules, period, amount_paise, start_at}
    pending: Mapped[dict | None] = mapped_column(JSONB)
    # none | created (checkout not finished) | authenticated | active | halted | cancelled | completed
    status: Mapped[str] = mapped_column(String(13), default="none", nullable=False)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scan_month: Mapped[str | None] = mapped_column(String(7))      # YYYY-MM the count below is for
    scans_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bonus_scans: Mapped[int] = mapped_column(Integer, default=0, nullable=False)   # bought packs; never expire
    packs: Mapped[list | None] = mapped_column(JSONB)              # [{order_id, payment_id, scans | messages, at}]
    # bill messages to customers on WhatsApp: this month's free use, bought ones left, and the alert already sent
    msg_month: Mapped[str | None] = mapped_column(String(7))
    msgs_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    bonus_msgs: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    msg_alert: Mapped[str | None] = mapped_column(String(10))      # "YYYY-MM:80" | "YYYY-MM:0" (used up)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class RzPlan(Base):
    """Razorpay plans already created, so each price/period is created once."""
    __tablename__ = "billing_rz_plans"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)     # "<amount_paise>:<period>"
    plan_id: Mapped[str] = mapped_column(String(40), nullable=False)
