"""wise-plus: petty expenses (loose slips, kachcha chits), the petty cash wallet each outlet keeps, and regular
vendors (milkman, ice, water cans) entered in one tap. Quantity is kept with the amount so stock and recipe costs
follow what was actually bought (₹300 of milk = 6 L)."""
import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


from ..models import Base, pk, tenant_fk

PAID_FROM = ("petty", "counter", "owner", "upi")
PAID_LABEL = {"petty": "Petty cash", "counter": "Counter cash", "owner": "Owner's pocket", "upi": "UPI / bank"}


class PettyExpense(Base):
    __tablename__ = "petty_expenses"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    spent_on: Mapped[date] = mapped_column(Date, nullable=False)                    # the day on the slip
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    what: Mapped[str | None] = mapped_column(String(120))                           # "2 ice blocks", "auto to market"
    qty_m: Mapped[int | None] = mapped_column(BigInteger)                           # thousandths of `unit`
    unit: Mapped[str | None] = mapped_column(String(20))                            # L, kg, pc, pouch of 6 L…
    stock_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stock_items.id"))
    base_m: Mapped[int | None] = mapped_column(BigInteger)                          # what went into stock (base unit)
    paid_from: Mapped[str] = mapped_column(String(7), nullable=False)               # petty | counter | owner | upi
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("expense_vendors.id", ondelete="SET NULL"))
    cash_move_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("cash_movements.id"))
    file_path: Mapped[str | None] = mapped_column(String(300))                      # the slip photo
    photo_hash: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(8), default="app", nullable=False)   # app | scan | whatsapp | vendor
    status: Mapped[str] = mapped_column(String(9), default="ok", nullable=False)    # ok | cancelled
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_petty_outlet_day", "outlet_id", "spent_on"), Index("ix_petty_hash", "tenant_id", "photo_hash"))


class PettyTopup(Base):
    """Cash put into an outlet's petty cash wallet."""
    __tablename__ = "petty_topups"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    outlet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)              # negative = taken back out
    source: Mapped[str] = mapped_column(String(7), nullable=False)                  # owner | counter | bank
    note: Mapped[str | None] = mapped_column(String(120))
    cash_move_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("cash_movements.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExpenseVendor(Base):
    """A regular: the milkman, ice wala, water cans. Usual item, quantity and amount, so a day's entry is one tap."""
    __tablename__ = "expense_vendors"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    outlet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("outlets.id"))  # None = every outlet
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    what: Mapped[str | None] = mapped_column(String(120))
    qty_m: Mapped[int | None] = mapped_column(BigInteger)
    unit: Mapped[str | None] = mapped_column(String(20))
    stock_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stock_items.id"))
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_from: Mapped[str] = mapped_column(String(7), default="petty", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
