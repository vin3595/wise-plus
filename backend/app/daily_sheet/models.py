"""Daily cash sheets (the printed Shakewallah sheet), read from a photo. One saved sheet per outlet per date;
saving another for the same date replaces it (the old one is kept as 'replaced')."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk


class DailySheet(Base):
    __tablename__ = "daily_sheets"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    sheet_date: Mapped[date | None] = mapped_column(Date)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)             # EMB's 10-field sheet JSON
    status: Mapped[str] = mapped_column(String(9), default="pending", nullable=False)   # pending | saved | discarded | replaced
    source: Mapped[str] = mapped_column(String(10), default="whatsapp", nullable=False)  # whatsapp | app
    sender: Mapped[str | None] = mapped_column(String(15))
    photo_path: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (Index("ix_daily_sheets_date", "tenant_id", "outlet_id", "sheet_date", "status"),)
