"""wise-plus: every WhatsApp message we send (with Meta's delivery ticks), restaurants' own WhatsApp numbers, and the
health of each number (quality, limit, token)."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk


class WaMessage(Base):
    """One message out. status: accepted (Meta took it) → sent → delivered → read, or failed (with the reason)."""
    __tablename__ = "wa_messages"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"))
    wamid: Mapped[str | None] = mapped_column(String(160), unique=True)
    from_id: Mapped[str | None] = mapped_column(String(40))            # the sending number's phone_number_id
    to_phone: Mapped[str] = mapped_column(String(15), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)      # text | template | image | document | interactive
    purpose: Mapped[str | None] = mapped_column(String(16))            # bill | promo | otp | offer | owner | alert | login …
    template: Mapped[str | None] = mapped_column(String(60))
    ref: Mapped[str | None] = mapped_column(String(60))                # bill id, offer code…
    status: Mapped[str] = mapped_column(String(9), default="accepted", nullable=False)
    error_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))
    tries: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # sent as a template because the 24 h window had closed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_wa_messages_tenant_time", "tenant_id", "created_at"), Index("ix_wa_messages_to", "to_phone", "created_at"))


class WaNumber(Base):
    """A restaurant's own WhatsApp number (Connect WhatsApp). Without one, they send from the shared number."""
    __tablename__ = "wa_numbers"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    phone_number_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    waba_id: Mapped[str | None] = mapped_column(String(40))
    display_number: Mapped[str | None] = mapped_column(String(20))
    token_enc: Mapped[str | None] = mapped_column(Text)               # their business token, encrypted
    app_secret_enc: Mapped[str | None] = mapped_column(Text)          # their own Meta app's secret (set up by hand), for webhooks
    how: Mapped[str] = mapped_column(String(8), default="signup", nullable=False)       # signup (Meta popup) | manual
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)   # active | paused | removed
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WaHealth(Base):
    """Small key → value notes: each number's last health check, alerts already sent (so they aren't repeated)."""
    __tablename__ = "wa_health"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
