"""wise-plus: sales history imported from another POS.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-24
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

CURRENT_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('app.bypass_rls', true), 'off') = 'on'"
TABLES = ("pos_imports", "imported_bills", "imported_lines")


def upgrade() -> None:
    op.execute("""create table if not exists pos_imports (
        id uuid primary key, tenant_id uuid not null references tenants(id) on delete cascade,
        outlet_id uuid not null references outlets(id), source varchar(40) not null, file_name varchar(200) not null,
        date_from date not null, date_to date not null, bills integer not null, total_paise integer not null,
        item_wise boolean not null default false, created_by uuid not null references users(id),
        created_at timestamptz not null default now())""")
    op.execute("create index if not exists ix_pos_imports_tenant_id on pos_imports (tenant_id)")
    op.execute("""create table if not exists imported_bills (
        id uuid primary key, tenant_id uuid not null references tenants(id) on delete cascade,
        import_id uuid not null references pos_imports(id) on delete cascade,
        outlet_id uuid not null references outlets(id), business_date date not null, billed_at timestamptz,
        bill_no varchar(40) not null, status varchar(10) not null, order_type varchar(10),
        gross_paise integer not null, discount_paise integer not null, taxable_paise integer not null,
        cgst_paise integer not null, sgst_paise integer not null, igst_paise integer not null,
        round_off_paise integer not null, total_paise integer not null, payments jsonb not null,
        customer_name varchar(80), customer_phone varchar(15), customer_gstin varchar(15),
        constraint uq_imported_bill unique (outlet_id, business_date, bill_no))""")
    op.execute("create index if not exists ix_imported_bills_tenant_id on imported_bills (tenant_id)")
    op.execute("create index if not exists ix_imported_bills_import_id on imported_bills (import_id)")
    op.execute("create index if not exists ix_imported_bills_outlet_day on imported_bills (outlet_id, business_date)")
    op.execute("""create table if not exists imported_lines (
        id uuid primary key, tenant_id uuid not null references tenants(id) on delete cascade,
        bill_id uuid not null references imported_bills(id) on delete cascade,
        item_name varchar(120) not null, variant_name varchar(60) not null, category varchar(60), hsn_sac varchar(8),
        qty integer not null, gross_paise integer not null, discount_paise integer not null,
        taxable_paise integer not null, tax_paise integer not null, rate_bp integer not null)""")
    op.execute("create index if not exists ix_imported_lines_tenant_id on imported_lines (tenant_id)")
    op.execute("create index if not exists ix_imported_lines_bill_id on imported_lines (bill_id)")
    for t in TABLES:
        op.execute(f"alter table {t} enable row level security")
        op.execute(f"alter table {t} force row level security")
        op.execute(f"drop policy if exists tenant_isolation on {t}")
        op.execute(f"create policy tenant_isolation on {t} using (tenant_id = {CURRENT_TENANT} or {BYPASS}) "
                   f"with check (tenant_id = {CURRENT_TENANT} or {BYPASS})")
        op.execute("do $$ begin if exists (select 1 from pg_roles where rolname = 'wise_app') then "
                   f"grant select, insert, update, delete on {t} to wise_app; end if; end $$;")


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"drop table if exists {t}")
