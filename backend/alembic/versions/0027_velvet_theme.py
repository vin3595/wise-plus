"""Ease My Bill's night look (charcoal, dusty purple, gold) becomes the default theme; "ease" stays as the classic choice.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-20
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table tenants alter column ui_theme set default 'velvet'")
    op.execute("select set_config('app.bypass_rls', 'on', true)")      # every business, not just one
    op.execute("update tenants set ui_theme = 'velvet' where ui_theme = 'ease'")


def downgrade() -> None:
    op.execute("alter table tenants alter column ui_theme set default 'ease'")
    op.execute("update tenants set ui_theme = 'ease' where ui_theme = 'velvet'")
