"""wise-plus: OTP check on offers, WhatsApp broadcasts to chosen customers, opt-out.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-18
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.offers.models import Broadcast
    op.execute("alter table offers add column if not exists require_otp boolean not null default false")
    op.execute("alter table offer_codes add column if not exists verified_at timestamptz")
    op.execute("alter table customers add column if not exists wa_optout boolean not null default false")
    Broadcast.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("alter table broadcasts enable row level security")
    op.execute("alter table broadcasts force row level security")
    op.execute("drop policy if exists tenant_isolation on broadcasts")
    op.execute(f"create policy tenant_isolation on broadcasts using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on broadcasts to wise_app; end if; end $$;")


def downgrade() -> None:
    op.execute("drop table if exists broadcasts")
    op.execute("alter table customers drop column if exists wa_optout")
    op.execute("alter table offer_codes drop column if exists verified_at")
    op.execute("alter table offers drop column if exists require_otp")
