"""Security M1: row-level security on the last tables without it.

Restaurant tables: each business sees only its own rows (as every other table already does).
Platform tables (codes, throttles, server settings, WhatsApp bookkeeping): only server code that switches the
check off on purpose (system sessions) can touch them; a restaurant's session sees nothing.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-24
"""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"

TENANT_TABLES = ("billing_subscriptions", "wa_messages", "wa_numbers", "wa_outbox", "growth_ref_codes",
                 "growth_trial_nudges", "growth_leads")
PLATFORM_TABLES = ("auth_throttle", "billing_rz_plans", "growth_referrals", "nightly_runs", "otp_codes",
                   "platform_secrets", "wa_contacts", "wa_health", "wa_state")


def upgrade() -> None:
    for t in TENANT_TABLES + PLATFORM_TABLES:
        using = f"(tenant_id = {CURRENT_TENANT} or {BYPASS})" if t in TENANT_TABLES else f"({BYPASS})"
        op.execute(f"alter table {t} enable row level security")
        op.execute(f"alter table {t} force row level security")
        op.execute(f"drop policy if exists tenant_isolation on {t}")
        op.execute(f"create policy tenant_isolation on {t} using {using} with check {using}")


def downgrade() -> None:
    for t in TENANT_TABLES + PLATFORM_TABLES:
        op.execute(f"drop policy if exists tenant_isolation on {t}")
        op.execute(f"alter table {t} no force row level security")
        op.execute(f"alter table {t} disable row level security")
