"""wise-plus: your own keys (Razorpay for Ease My Bill's billing), pasted in the app instead of the server file.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-19
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""create table if not exists platform_secrets (
        key varchar(40) primary key, value_enc text not null,
        updated_at timestamptz not null default now())""")
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on platform_secrets to wise_app; end if; end $$;")


def downgrade() -> None:
    op.execute("drop table if exists platform_secrets")
