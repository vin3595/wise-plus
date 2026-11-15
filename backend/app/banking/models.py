"""Bank accounts, imported bank statement lines, and the categories the app has learnt (wise-plus).

A statement upload adds its lines (overlapping uploads are skipped line by line), then each line is matched:
UPI / card sales of a day, Zomato / Swiggy payouts, supplier payments, cash deposits, charges, salaries…
What can't be matched waits under "To check"; the person's choice is remembered for similar lines."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, pk, tenant_fk

KINDS = ("sales", "aggregator", "supplier", "cash_deposit", "cash_withdrawal", "transfer", "expense", "drawing",
         "salary", "rent", "tax", "loan", "charges", "refund", "capital", "other_in", "other_out")
KIND_LABEL = {
    "sales": "UPI / card sales", "aggregator": "Zomato / Swiggy payout", "supplier": "Supplier payment",
    "cash_deposit": "Cash deposited", "cash_withdrawal": "Cash withdrawn", "transfer": "Between own accounts",
    "expense": "Expense", "drawing": "Owner's drawing", "salary": "Salary", "rent": "Rent", "tax": "Tax / GST",
    "loan": "Loan / EMI", "charges": "Bank charges", "refund": "Refund received", "capital": "Owner put money in",
    "other_in": "Other money in", "other_out": "Other money out",
}
STATUSES = ("matched", "suggested", "unmatched", "ignored")


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(60), nullable=False)               # "HDFC Current"
    bank: Mapped[str | None] = mapped_column(String(40))                         # "HDFC Bank"
    last4: Mapped[str | None] = mapped_column(String(4))                         # only the last 4 digits are kept
    outlet_ids: Mapped[list | None] = mapped_column(JSONB)                       # where UPI / card money lands; None = all
    opening_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    opening_date: Mapped[date | None] = mapped_column(Date)
    tally_ledger: Mapped[str | None] = mapped_column(String(80))                 # its name in Tally, if different
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)


class BankStatement(Base):
    __tablename__ = "bank_statements"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    bank_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(120))
    file_path: Mapped[str | None] = mapped_column(String(300))
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)
    closing_paise: Mapped[int | None] = mapped_column(BigInteger)
    lines_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lines_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BankTxn(Base):
    __tablename__ = "bank_txns"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    bank_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False)
    statement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bank_statements.id"))
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    narration: Mapped[str] = mapped_column(String(300), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(60))
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)        # + money in, - money out
    balance_paise: Mapped[int | None] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)          # same line in two uploads = one line
    status: Mapped[str] = mapped_column(String(9), default="unmatched", nullable=False)
    kind: Mapped[str | None] = mapped_column(String(15))
    category: Mapped[str | None] = mapped_column(String(40))                      # expense category, e.g. Electricity
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("suppliers.id"))
    supplier_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("supplier_entries.id"))
    other_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bank_accounts.id"))
    note: Mapped[str | None] = mapped_column(String(160))                         # "UPI sales of 14 Nov", what it matched
    matched_by: Mapped[str | None] = mapped_column(String(6))                     # auto | rule | person
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("bank_account_id", "fingerprint"),
                      Index("ix_bank_txns_account_date", "bank_account_id", "txn_date"),
                      Index("ix_bank_txns_status", "tenant_id", "status"))


class BankRule(Base):
    """Learnt from the person: lines whose description has these words are this kind of money."""
    __tablename__ = "bank_rules"
    id: Mapped[uuid.UUID] = pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    pattern: Mapped[str] = mapped_column(String(80), nullable=False)              # lower case words, e.g. "jvvnl"
    direction: Mapped[str] = mapped_column(String(3), nullable=False)             # in | out
    kind: Mapped[str] = mapped_column(String(15), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40))
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("suppliers.id"))
    uses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "pattern", "direction"),)
