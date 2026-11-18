"""wise-plus growth: website leads, demo bookings, follow-ups, referrals, trial check-ins.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-18
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None
TABLES = ("growth_leads", "growth_ref_codes", "growth_referrals", "growth_trial_nudges")


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.growth.models import Lead, RefCode, Referral, TrialNudge
    for m in (Lead, RefCode, Referral, TrialNudge):
        m.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               f"grant select, insert, update, delete on {', '.join(TABLES)} to wise_app; end if; end $$;")


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"drop table if exists {t}")
