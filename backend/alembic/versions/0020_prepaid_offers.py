"""wise-plus: prepaid WhatsApp offers (Razorpay payment link first, then the QR) and what each code was worth.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-18
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.offers.models import OfferPayment
    op.execute("alter table offers add column if not exists prepaid boolean not null default false")
    op.execute("alter table offer_codes add column if not exists regular_paise integer")
    op.execute("alter table offer_codes add column if not exists prepaid_ref varchar(40)")
    op.execute("alter table offer_codes add column if not exists prepaid_paise integer")
    OfferPayment.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("alter table offer_payments enable row level security")
    op.execute("alter table offer_payments force row level security")
    op.execute("drop policy if exists tenant_isolation on offer_payments")
    op.execute(f"create policy tenant_isolation on offer_payments using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
               f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on offer_payments to wise_app; end if; end $$;")
    op.execute("set local app.bypass_rls = 'on'")
    op.execute("""update offer_codes c set regular_paise = v.price_paise from offers o join variants v on v.id = o.variant_id
                  where o.id = c.offer_id and c.regular_paise is null""")


def downgrade() -> None:
    op.execute("drop table if exists offer_payments")
    for col in ("prepaid_paise", "prepaid_ref", "regular_paise"):
        op.execute(f"alter table offer_codes drop column if exists {col}")
    op.execute("alter table offers drop column if exists prepaid")
