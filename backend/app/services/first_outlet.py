"""wise-plus: a business that skips POS still needs one place for its stock, bills and sheets."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Brand, Outlet, Tenant


def first_outlet(db: Session, tenant: Tenant, brand: Brand | None = None) -> Outlet:
    have = db.scalar(select(Outlet).where(Outlet.tenant_id == tenant.id).limit(1))
    if have:
        return have
    brand = brand or db.scalar(select(Brand).where(Brand.tenant_id == tenant.id).limit(1))
    if brand is None:
        brand = Brand(tenant_id=tenant.id, name=tenant.name)
        db.add(brand)
        db.flush()
    code = (re.sub(r"[^A-Za-z]", "", tenant.name).upper() + "MAIN")[:2]
    o = Outlet(tenant_id=tenant.id, brand_id=brand.id, name=tenant.name, code=code)
    db.add(o)
    db.flush()
    return o
