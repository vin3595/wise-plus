"""wise-plus: phone notifications (Web Push subscriptions).

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-19
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.push.models import PushSubscription
    PushSubscription.__table__.create(bind=op.get_bind(), checkfirst=True)
    t = "push_subscriptions"
    op.execute(f"alter table {t} enable row level security")
    op.execute(f"alter table {t} force row level security")
    op.execute(f"drop policy if exists tenant_isolation on {t}")
    op.execute(f"create policy tenant_isolation on {t} using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               f"grant select, insert, update, delete on {t} to wise_app; end if; end $$;")


def downgrade() -> None:
    op.execute("drop table if exists push_subscriptions")
