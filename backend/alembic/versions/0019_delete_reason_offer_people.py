"""wise-plus: purchase bills deleted with a manager PIN and a reason; offer codes keep the customer's name, number,
the item and its offer price.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-18
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table purchases add column if not exists cancel_reason varchar(200)")
    op.execute("alter table purchases add column if not exists cancelled_by uuid references users(id)")
    op.execute("alter table purchases add column if not exists cancel_approved_by uuid references users(id)")
    op.execute("alter table purchases add column if not exists cancelled_at timestamptz")
    op.execute("alter table offer_codes add column if not exists cust_name varchar(60)")
    op.execute("alter table offer_codes add column if not exists item_name varchar(120)")
    op.execute("alter table offer_codes add column if not exists price_paise integer")
    # codes already given out: fill the item and price from their offer
    op.execute("set local app.bypass_rls = 'on'")
    op.execute("""update offer_codes c set price_paise = o.price_paise,
                    item_name = left(i.name || case when v.name in ('Regular', '') then '' else ' (' || v.name || ')' end, 120)
                  from offers o join variants v on v.id = o.variant_id join items i on i.id = v.item_id
                  where o.id = c.offer_id and c.item_name is null""")
    op.execute("""update offer_codes c set cust_name = cu.name from customers cu
                  where cu.tenant_id = c.tenant_id and cu.phone = c.phone10 and c.cust_name is null and cu.name is not null""")


def downgrade() -> None:
    for col in ("price_paise", "item_name", "cust_name"):
        op.execute(f"alter table offer_codes drop column if exists {col}")
    for col in ("cancelled_at", "cancel_approved_by", "cancelled_by", "cancel_reason"):
        op.execute(f"alter table purchases drop column if exists {col}")
