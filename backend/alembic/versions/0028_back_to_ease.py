"""Back to the original Ease My Bill look as the default; the night look stays as a choice in Branding.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-20
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table tenants alter column ui_theme set default 'ease'")
    op.execute("select set_config('app.bypass_rls', 'on', true)")
    op.execute("update tenants set ui_theme = 'ease' where ui_theme = 'velvet'")


def downgrade() -> None:
    op.execute("alter table tenants alter column ui_theme set default 'velvet'")
