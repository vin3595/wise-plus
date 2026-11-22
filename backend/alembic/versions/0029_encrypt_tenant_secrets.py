"""Encrypt each restaurant's payment and SMS keys in the database (Razorpay secret + webhook secret,
Razorpay POS app key, Paytm merchant key, MSG91 auth key).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-22
"""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

COLS = {"payment_settings": ["key_secret", "webhook_secret", "pos_app_key", "paytm_key"]}


def _sms_table(conn) -> str | None:
    r = conn.execute(sa.text("select table_name from information_schema.columns where column_name = 'msg91_auth_key' "
                             "and table_schema = current_schema()")).first()
    return r[0] if r else None


def upgrade() -> None:
    from app.core.secretbox import encrypt
    conn = op.get_bind()
    cols = dict(COLS)
    sms = _sms_table(conn)
    if sms:
        cols[sms] = ["msg91_auth_key"]
    conn.execute(sa.text("select set_config('app.bypass_rls', 'on', true)"))
    for table, names in cols.items():
        for c in names:
            op.alter_column(table, c, type_=sa.Text(), existing_nullable=True)
        pk = "tenant_id" if table == "payment_settings" else None
        if pk is None:
            pk = conn.execute(sa.text(
                "select a.attname from pg_index i join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey) "
                "where i.indrelid = cast(:t as regclass) and i.indisprimary limit 1"), {"t": table}).scalar_one()
        rows = conn.execute(sa.text(f'select {pk}, {", ".join(names)} from "{table}"')).all()
        for row in rows:
            vals = {c: encrypt(getattr(row, c)) for c in names}
            if any(vals[c] != getattr(row, c) for c in names):
                sets = ", ".join(f"{c} = :{c}" for c in names)
                conn.execute(sa.text(f'update "{table}" set {sets} where {pk} = :pk'), {**vals, "pk": getattr(row, pk)})


def downgrade() -> None:
    pass   # keys stay encrypted; the app reads both forms
