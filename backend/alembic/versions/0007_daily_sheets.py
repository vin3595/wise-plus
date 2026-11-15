"""wise-plus: daily cash sheets read from a photo (Ease My Bill daily sheet), tenant-isolated.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    from app.daily_sheet.models import DailySheet
    DailySheet.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("alter table daily_sheets enable row level security")
    op.execute("alter table daily_sheets force row level security")
    op.execute("drop policy if exists tenant_isolation on daily_sheets")
    op.execute(f"create policy tenant_isolation on daily_sheets using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on daily_sheets to wise_app; end if; end $$;")


def downgrade() -> None:
    op.drop_table("daily_sheets")
