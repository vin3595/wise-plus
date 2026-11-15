"""wise-plus: supplier ledger (opening balances, payments, debit notes), tenant-isolated like every table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    from app.ledger.models import SupplierEntry
    bind = op.get_bind()
    # checkfirst: 0001 builds every model it can see, and may already have made this one.
    SupplierEntry.__table__.create(bind=bind, checkfirst=True)
    op.execute("alter table supplier_entries enable row level security")
    op.execute("alter table supplier_entries force row level security")
    op.execute("drop policy if exists tenant_isolation on supplier_entries")
    op.execute(f"create policy tenant_isolation on supplier_entries using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    # Tables made by 0001 got their grants then; make sure this one has them too.
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on supplier_entries to wise_app; end if; end $$;")
    # "price vanilla" looks inside purchase lines for an item.
    op.execute("create index if not exists ix_purchases_lines on purchases using gin (lines jsonb_path_ops)")


def downgrade() -> None:
    op.execute("drop index if exists ix_purchases_lines")
    op.drop_table("supplier_entries")
