"""wise-plus: water / waiter / bill requests from the table QR.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-15
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    from app.tableqr.models import TableRequest
    TableRequest.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("alter table table_requests enable row level security")
    op.execute("alter table table_requests force row level security")
    op.execute("drop policy if exists tenant_isolation on table_requests")
    op.execute(f"create policy tenant_isolation on table_requests using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on table_requests to wise_app; end if; end $$;")
    op.execute("alter table outlets add column if not exists table_service boolean not null default false")


def downgrade() -> None:
    op.execute("alter table outlets drop column if exists table_service")
    op.drop_table("table_requests")
