"""wise-plus: barcodes on item sizes and stock items; WhatsApp keyword offers and their one-time codes.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-17
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.offers.models import Offer, OfferCode
    op.execute("alter table variants add column if not exists barcode varchar(32)")
    op.execute("alter table stock_items add column if not exists barcode varchar(32)")
    op.execute("do $$ begin if not exists (select 1 from pg_constraint where conname = 'uq_variant_barcode') then "
               "alter table variants add constraint uq_variant_barcode unique (tenant_id, barcode); end if; end $$;")
    op.execute("do $$ begin if not exists (select 1 from pg_constraint where conname = 'uq_stock_item_barcode') then "
               "alter table stock_items add constraint uq_stock_item_barcode unique (tenant_id, barcode); end if; end $$;")
    bind = op.get_bind()
    for m in (Offer, OfferCode):
        m.__table__.create(bind=bind, checkfirst=True)
        t = m.__tablename__
        op.execute(f"alter table {t} enable row level security")
        op.execute(f"alter table {t} force row level security")
        op.execute(f"drop policy if exists tenant_isolation on {t}")
        op.execute(f"create policy tenant_isolation on {t} using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
                   f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
        op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
                   f"grant select, insert, update, delete on {t} to wise_app; end if; end $$;")


def downgrade() -> None:
    op.execute("drop table if exists offer_codes")
    op.execute("drop table if exists offers")
    op.execute("alter table variants drop constraint if exists uq_variant_barcode")
    op.execute("alter table stock_items drop constraint if exists uq_stock_item_barcode")
    op.execute("alter table variants drop column if exists barcode")
    op.execute("alter table stock_items drop column if exists barcode")
