"""wise-plus stock extras (from the Shakewallah stock app): transfers between outlets, WhatsApp alert
subscriptions and the nightly summary run log."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk


class StockTransfer(Base):
    """Stock sent from one outlet (the central kitchen) to another. It leaves the sender when sent and
    reaches the other outlet when they receive it; anything short on arrival is kept on the transfer."""
    __tablename__ = "stock_transfers"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    from_outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    to_outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    # [{stock_item_id, name, unit, sent_m, cost_k, received_m?}]
    lines: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(9), default="sent", nullable=False)   # sent | received | cancelled
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    received_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_transfers_to", "to_outlet_id", "status"), Index("ix_transfers_from", "from_outlet_id", "business_date"))


class WaAlert(Base):
    """A team member who wants an update on WhatsApp (nightly summary, ...)."""
    __tablename__ = "wa_alerts"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    topic: Mapped[str] = mapped_column(String(12), nullable=False)   # nightly | wa_off (this account ignored on WhatsApp)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "topic"),)


class WaContact(Base):
    """When a number last wrote to us: free messages only reach it within 24 hours of that."""
    __tablename__ = "wa_contacts"
    phone10: Mapped[str] = mapped_column(String(10), primary_key=True)
    last_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NightlyRun(Base):
    """One row per night, so only one API worker sends the nightly summaries."""
    __tablename__ = "nightly_runs"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WaState(Base):
    """Per WhatsApp number: the restaurant they picked, and photos waiting for that choice.
    In the database (not memory) because the API runs as more than one process."""
    __tablename__ = "wa_state"
    phone10: Mapped[str] = mapped_column(String(10), primary_key=True)
    choice: Mapped[str | None] = mapped_column(String(30))
    held: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))   # [[time, media_id, mime, filename, caption]]
    # a customer who just said the visit wasn't good: their next message is what went wrong
    feedback: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))   # {feedback_id, tenant_id, at}
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
