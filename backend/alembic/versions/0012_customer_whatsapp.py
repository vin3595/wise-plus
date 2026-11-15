"""wise-plus: bills on WhatsApp for customers, with feedback buttons.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-15
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"
GRANT = ("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
         "grant select, insert, update, delete on {t} to wise_app; end if; end $$;")


def upgrade() -> None:
    from app.custwa.models import BillFeedback, WaOutbox
    bind = op.get_bind()
    BillFeedback.__table__.create(bind=bind, checkfirst=True)
    op.execute("alter table bill_feedback enable row level security")
    op.execute("alter table bill_feedback force row level security")
    op.execute("drop policy if exists tenant_isolation on bill_feedback")
    op.execute(f"create policy tenant_isolation on bill_feedback using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute(GRANT.format(t="bill_feedback"))
    WaOutbox.__table__.create(bind=bind, checkfirst=True)
    op.execute(GRANT.format(t="wa_outbox"))
    op.execute("alter table outlets add column if not exists wa_bills boolean not null default false")
    op.execute("alter table outlets add column if not exists google_review_url varchar(300)")
    op.execute("alter table wa_state add column if not exists feedback jsonb")


def downgrade() -> None:
    op.execute("alter table wa_state drop column if exists feedback")
    op.execute("alter table outlets drop column if exists google_review_url")
    op.execute("alter table outlets drop column if exists wa_bills")
    op.drop_table("wa_outbox")
    op.drop_table("bill_feedback")
