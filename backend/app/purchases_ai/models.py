"""Bills read by the AI but not saved yet: sent on WhatsApp, waiting for 'save' or a check in the app."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk


class ScannedBill(Base):
    __tablename__ = "scanned_bills"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(10), default="whatsapp", nullable=False)     # whatsapp
    sender: Mapped[str | None] = mapped_column(String(15))                                 # last 10 digits of the phone
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    file_path: Mapped[str | None] = mapped_column(String(300))
    draft: Mapped[dict] = mapped_column(JSONB, nullable=False)                             # what POST /scan returns
    status: Mapped[str] = mapped_column(String(9), default="pending", nullable=False)      # pending | saved | discarded
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("purchases.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_scanned_bills_status", "tenant_id", "status", "created_at"),)
