"""wise-plus: credit notes to customers; supplier credit notes in the dues ledger.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-16
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.creditnotes.models import CreditNote
    CreditNote.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("alter table credit_notes enable row level security")
    op.execute("alter table credit_notes force row level security")
    op.execute("drop policy if exists tenant_isolation on credit_notes")
    op.execute(f"create policy tenant_isolation on credit_notes using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on credit_notes to wise_app; end if; end $$;")
    op.execute("alter table supplier_entries alter column kind type varchar(11)")
    op.execute("alter table supplier_entries drop constraint if exists ck_supplier_entry_kind")
    op.execute("alter table supplier_entries add constraint ck_supplier_entry_kind check (kind in ('opening','payment','debit_note','credit_note'))")


def downgrade() -> None:
    op.execute("alter table supplier_entries drop constraint if exists ck_supplier_entry_kind")
    op.execute("set local app.bypass_rls = 'on'")
    op.execute("update supplier_entries set kind = 'debit_note' where kind = 'credit_note'")   # same effect on the balance
    op.execute("alter table supplier_entries add constraint ck_supplier_entry_kind check (kind in ('opening','payment','debit_note'))")
    op.drop_table("credit_notes")
