"""wise-plus: bill messages to customers on WhatsApp — monthly free use per outlet, bought packs, alerts.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-17
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table billing_subscriptions add column if not exists msg_month varchar(7)")
    op.execute("alter table billing_subscriptions add column if not exists msgs_used integer not null default 0")
    op.execute("alter table billing_subscriptions add column if not exists bonus_msgs integer not null default 0")
    op.execute("alter table billing_subscriptions add column if not exists msg_alert varchar(10)")


def downgrade() -> None:
    for c in ("msg_alert", "bonus_msgs", "msgs_used", "msg_month"):
        op.execute(f"alter table billing_subscriptions drop column if exists {c}")
