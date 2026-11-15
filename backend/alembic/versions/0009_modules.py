"""wise-plus: services a business has switched on (POS, Inventory, Bill scan, Daily sheet).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-15
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table tenants add column if not exists modules jsonb")   # null = everything


def downgrade() -> None:
    op.execute("alter table tenants drop column if exists modules")
