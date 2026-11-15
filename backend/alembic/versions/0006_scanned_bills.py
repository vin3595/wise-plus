"""wise-plus: bills read by the AI and waiting to be saved (sent on WhatsApp), tenant-isolated.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    from app.purchases_ai.models import ScannedBill
    ScannedBill.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("alter table scanned_bills enable row level security")
    op.execute("alter table scanned_bills force row level security")
    op.execute("drop policy if exists tenant_isolation on scanned_bills")
    op.execute(f"create policy tenant_isolation on scanned_bills using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on scanned_bills to wise_app; end if; end $$;")


def downgrade() -> None:
    op.drop_table("scanned_bills")
