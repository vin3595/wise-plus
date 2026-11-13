"""wise-plus: the Ease look (from Ease My Bill) is the default for new restaurants.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table tenants alter column ui_theme set default 'ease'")


def downgrade() -> None:
    op.execute("update tenants set ui_theme = 'steel' where ui_theme = 'ease'")
    op.execute("alter table tenants alter column ui_theme set default 'steel'")
