"""wise-plus: a UPI QR printed on every bill that turns the bill to UPI when paid.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-25
"""
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table payment_settings add column if not exists qr_on_bill boolean not null default false")


def downgrade() -> None:
    op.execute("alter table payment_settings drop column if exists qr_on_bill")
