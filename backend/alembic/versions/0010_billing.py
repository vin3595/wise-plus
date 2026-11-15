"""wise-plus billing: plans paid through Razorpay, bill-scan allowance.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-15
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

GRANT = ("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
         "grant select, insert, update, delete on {t} to wise_app; end if; end $$;")


def upgrade() -> None:
    from app.billing.models import RzPlan, Subscription
    bind = op.get_bind()
    for model in (Subscription, RzPlan):
        model.__table__.create(bind=bind, checkfirst=True)
        op.execute(GRANT.format(t=model.__tablename__))


def downgrade() -> None:
    op.drop_table("billing_rz_plans")
    op.drop_table("billing_subscriptions")
