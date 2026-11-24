"""wise-plus: sales history brought in from another POS (Petpooja, Posist ...) for the months before EMB.
Kept apart from EMB's own bills: no invoice numbers, shifts or cash drawers are touched. Reports add them in."""
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk


class PosImport(Base):
    """One uploaded file."""
    __tablename__ = "pos_imports"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)        # Petpooja, Posist, Other
    file_name: Mapped[str] = mapped_column(String(200), nullable=False)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    bills: Mapped[int] = mapped_column(Integer, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    item_wise: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ImportedBill(Base):
    __tablename__ = "imported_bills"
    __table_args__ = (UniqueConstraint("outlet_id", "business_date", "bill_no", name="uq_imported_bill"),
                      Index("ix_imported_bills_outlet_day", "outlet_id", "business_date"))
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pos_imports.id", ondelete="CASCADE"), index=True, nullable=False)
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    billed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bill_no: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)        # paid | cancelled
    order_type: Mapped[str | None] = mapped_column(String(10))
    gross_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    taxable_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    cgst_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    sgst_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    igst_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    round_off_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    payments: Mapped[dict] = mapped_column(JSONB, nullable=False)        # mode -> paise
    customer_name: Mapped[str | None] = mapped_column(String(80))
    customer_phone: Mapped[str | None] = mapped_column(String(15))
    customer_gstin: Mapped[str | None] = mapped_column(String(15))


class ImportedLine(Base):
    __tablename__ = "imported_lines"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    bill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("imported_bills.id", ondelete="CASCADE"), index=True, nullable=False)
    item_name: Mapped[str] = mapped_column(String(120), nullable=False)
    variant_name: Mapped[str] = mapped_column(String(60), nullable=False)
    category: Mapped[str | None] = mapped_column(String(60))
    hsn_sac: Mapped[str | None] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    taxable_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_bp: Mapped[int] = mapped_column(Integer, nullable=False)
