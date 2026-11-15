"""wise-plus: WhatsApp restaurant choice and photos waiting for it, shared by all API processes.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-15
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.stockplus.models import WaState
    WaState.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on wa_state to wise_app; end if; end $$;")


def downgrade() -> None:
    op.drop_table("wa_state")
