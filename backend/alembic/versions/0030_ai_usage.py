"""wise-plus: cost of every AI read (bill scans etc.), per restaurant.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-23
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    op.execute("""create table if not exists ai_usage (
        id uuid primary key, tenant_id uuid references tenants(id) on delete set null,
        created_at timestamptz not null default now(), model varchar(40) not null, purpose varchar(20) not null,
        tokens_in integer not null default 0, tokens_out integer not null default 0, usd numeric(12, 6) not null default 0)""")
    op.execute("create index if not exists ix_ai_usage_tenant_time on ai_usage (tenant_id, created_at)")
    op.execute("alter table ai_usage enable row level security")
    op.execute("alter table ai_usage force row level security")
    op.execute("drop policy if exists tenant_isolation on ai_usage")
    op.execute(f"create policy tenant_isolation on ai_usage using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on ai_usage to wise_app; end if; end $$;")


def downgrade() -> None:
    op.execute("drop table if exists ai_usage")
