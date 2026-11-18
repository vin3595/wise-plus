"""wise-plus: petty expenses with quantity, petty cash wallets, regular vendors.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-18
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"
TABLES = ("expense_vendors", "petty_expenses", "petty_topups")


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.expenses.models import ExpenseVendor, PettyExpense, PettyTopup
    for m in (ExpenseVendor, PettyExpense, PettyTopup):
        m.__table__.create(bind=op.get_bind(), checkfirst=True)
    for t in TABLES:
        op.execute(f"alter table {t} enable row level security")
        op.execute(f"alter table {t} force row level security")
        op.execute(f"drop policy if exists tenant_isolation on {t}")
        op.execute(f"create policy tenant_isolation on {t} using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
                   f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
        op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
                   f"grant select, insert, update, delete on {t} to wise_app; end if; end $$;")


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"drop table if exists {t}")
