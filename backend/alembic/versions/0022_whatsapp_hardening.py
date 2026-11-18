"""wise-plus: WhatsApp delivery ticks for every message, restaurants' own numbers, number health; customers not on
WhatsApp are remembered.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-18
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.whatsapp.models import WaHealth, WaMessage, WaNumber
    for m in (WaMessage, WaNumber, WaHealth):
        m.__table__.create(bind=op.get_bind(), checkfirst=True)
    op.execute("alter table wa_numbers add column if not exists app_secret_enc text")
    op.execute("alter table wa_numbers add column if not exists how varchar(8) not null default 'signup'")
    op.execute("alter table wa_outbox add column if not exists wamid varchar(160)")
    op.execute("alter table wa_outbox add column if not exists delivery varchar(9)")
    op.execute("create index if not exists ix_wa_outbox_wamid on wa_outbox (wamid)")
    op.execute("alter table customers add column if not exists wa_unreachable boolean not null default false")
    # platform tables (no tenant isolation: the server's own bookkeeping; the app role may read and write them)
    op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
               "grant select, insert, update, delete on wa_messages, wa_numbers, wa_health to wise_app; end if; end $$;")


def downgrade() -> None:
    op.execute("alter table customers drop column if exists wa_unreachable")
    op.execute("drop index if exists ix_wa_outbox_wamid")
    op.execute("alter table wa_outbox drop column if exists delivery")
    op.execute("alter table wa_outbox drop column if exists wamid")
    for t in ("wa_health", "wa_numbers", "wa_messages"):
        op.execute(f"drop table if exists {t}")
