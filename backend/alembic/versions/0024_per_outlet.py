"""wise-plus: plans are priced per outlet; a subscription remembers how many outlets it pays for.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-18
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table billing_subscriptions add column if not exists outlets integer not null default 1")


def downgrade() -> None:
    op.execute("alter table billing_subscriptions drop column if exists outlets")
