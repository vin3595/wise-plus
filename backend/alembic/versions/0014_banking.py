"""wise-plus: bank accounts, bank statement lines, learnt categories; supplier payments say which bank.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-16
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"
TABLES = ("bank_accounts", "bank_statements", "bank_txns", "bank_rules")


def secure(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(f"drop policy if exists tenant_isolation on {table}")
    op.execute(f"create policy tenant_isolation on {table} using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               f"grant select, insert, update, delete on {table} to wise_app; end if; end $$;")


def upgrade() -> None:
    from app.banking.models import BankAccount, BankRule, BankStatement, BankTxn
    bind = op.get_bind()
    for m in (BankAccount, BankStatement, BankRule):
        m.__table__.create(bind=bind, checkfirst=True)
    op.execute("alter table supplier_entries add column if not exists bank_account_id uuid references bank_accounts(id)")
    BankTxn.__table__.create(bind=bind, checkfirst=True)
    for t in TABLES:
        secure(t)


def downgrade() -> None:
    op.execute("alter table supplier_entries drop column if exists bank_account_id")
    for t in reversed(TABLES):
        op.execute(f"drop table if exists {t}")
