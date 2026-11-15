"""wise-plus: stock app extras - transfers, spot checks, storage locations, daily-order items,
WhatsApp alert subscriptions and the nightly summary.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"
GRANT = ("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
         "grant select, insert, update, delete on {t} to wise_app; end if; end $$;")


def upgrade() -> None:
    from app.stockplus.models import NightlyRun, StockTransfer, WaAlert, WaContact
    bind = op.get_bind()
    for model in (StockTransfer, WaAlert):
        t = model.__tablename__
        model.__table__.create(bind=bind, checkfirst=True)
        op.execute(f"alter table {t} enable row level security")
        op.execute(f"alter table {t} force row level security")
        op.execute(f"drop policy if exists tenant_isolation on {t}")
        op.execute(f"create policy tenant_isolation on {t} using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
                   f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
        op.execute(GRANT.format(t=t))
    for model in (WaContact, NightlyRun):   # not per restaurant
        model.__table__.create(bind=bind, checkfirst=True)
        op.execute(GRANT.format(t=model.__tablename__))
    # "if not exists": a fresh database is created from the models, which already have these columns
    op.execute("alter table stock_items add column if not exists locations jsonb")
    op.execute("alter table stock_items add column if not exists order_daily boolean not null default false")
    op.execute("alter table stock_counts add column if not exists kind varchar(5) not null default 'close'")


def downgrade() -> None:
    op.drop_column("stock_counts", "kind")
    op.drop_column("stock_items", "order_daily")
    op.drop_column("stock_items", "locations")
    for t in ("nightly_runs", "wa_contacts", "wa_alerts", "stock_transfers"):
        op.drop_table(t)
