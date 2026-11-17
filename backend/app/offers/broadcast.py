"""Customers › Broadcast: a WhatsApp message to a chosen group of customers.

Who: built from paid bills (number on the bill) — visits, total and average spend, first and last visit, outlets —
plus offers they've used. Never to anyone who tapped "Stop offers".
How: Meta's rules allow a message to someone who hasn't written in 24 hours only as an approved MARKETING template
(about ₹0.86 each), so broadcasts use bought message credits (6 per message); the free monthly bill messages don't
cover them. Messages queue in the outbox and go out a few a second."""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.kds import business_date

CREDITS_EACH = 6
STATS = """
with b as (
  select right(regexp_replace(customer_phone, '\\D', '', 'g'), 10) as p, count(*) as visits, sum(total_paise) as spend,
         max(business_date) as last_visit, min(business_date) as first_visit, array_agg(distinct outlet_id::text) as outlets
  from bills where status = 'paid' and customer_phone is not null and length(regexp_replace(customer_phone, '\\D', '', 'g')) >= 10
  group by 1),
o as (select phone10 as p, count(*) as used from offer_codes where status = 'used' group by 1),
c as (select right(regexp_replace(phone, '\\D', '', 'g'), 10) as p, max(name) as name, bool_or(wa_optout) as optout from customers group by 1)
select b.p, c.name, b.visits, b.spend, b.spend / b.visits as avg, b.last_visit, b.first_visit, b.outlets,
       coalesce(o.used, 0) as offers_used, coalesce(c.optout, false) as optout
from b left join o on o.p = b.p left join c on c.p = b.p
"""


def people(db: Session, f: dict) -> list[dict]:
    """Customers matching every filter given (a filter left out doesn't narrow anything)."""
    today = business_date()
    rows = [dict(r) for r in db.execute(text(STATS)).mappings()]
    out = []
    for r in rows:
        if r["optout"]:
            continue
        if f.get("active_days") and r["last_visit"] < today - timedelta(days=int(f["active_days"])):
            continue
        if f.get("away_days") and r["last_visit"] > today - timedelta(days=int(f["away_days"])):
            continue
        if f.get("new_days") and r["first_visit"] < today - timedelta(days=int(f["new_days"])):
            continue
        if f.get("min_visits") and r["visits"] < int(f["min_visits"]):
            continue
        if f.get("max_visits") and r["visits"] > int(f["max_visits"]):
            continue
        if f.get("min_spend") and r["spend"] < int(f["min_spend"]):
            continue
        if f.get("max_spend") and r["spend"] > int(f["max_spend"]):
            continue
        if f.get("min_avg") and r["avg"] < int(f["min_avg"]):
            continue
        if f.get("max_avg") and r["avg"] > int(f["max_avg"]):
            continue
        if f.get("offers") == "users" and r["offers_used"] == 0:
            continue
        if f.get("offers") == "never" and r["offers_used"] > 0:
            continue
        if f.get("outlet_ids") and not set(f["outlet_ids"]) & set(r["outlets"] or []):
            continue
        out.append(r)
    top = f.get("top_pct")
    if top:                                      # the biggest spenders: top N% of everyone (before other filters)
        everyone = sorted((r["spend"] for r in rows if not r["optout"]), reverse=True)
        if everyone:
            cut = everyone[max(0, min(len(everyone) - 1, int(len(everyone) * int(top) / 100) - 1))]
            out = [r for r in out if r["spend"] >= cut]
    bottom = f.get("bottom_pct")
    if bottom:
        everyone = sorted(r["spend"] for r in rows if not r["optout"])
        if everyone:
            cut = everyone[max(0, min(len(everyone) - 1, int(len(everyone) * int(bottom) / 100) - 1))]
            out = [r for r in out if r["spend"] <= cut]
    return out


def mask(p: str) -> str:
    return "…" + p[-4:]


def clean_text(t: str) -> str:
    """WhatsApp template values can't hold new lines or tabs: lines become ' · '."""
    t = re.sub(r"[\t ]{2,}", " ", (t or "").strip())
    return re.sub(r"\s*\n+\s*", " · ", t)[:700]


def queue(db: Session, tenant, bid: uuid.UUID, rows: list[dict], text_: str, keyword: str | None) -> int:
    brand = tenant.app_name or tenant.name
    n = 0
    for r in rows:
        payload = {"name": (r.get("name") or "there").split(" ")[0][:30], "text": text_, "brand": brand, "keyword": keyword, "tenant_id": str(tenant.id)}
        res = db.execute(text("insert into wa_outbox (id, tenant_id, kind, ref, to_phone, payload, status, tries, next_try_at, created_at) "
                              "values (:id, :t, 'promo', :ref, :to, cast(:p as jsonb), 'queued', 0, now(), now()) on conflict (kind, ref) do nothing"),
                         {"id": uuid.uuid4(), "t": tenant.id, "ref": bid.hex[:20] + r["p"], "to": r["p"], "p": json.dumps(payload)})
        n += res.rowcount or 0
    return n


def send_promo(to: str, p: dict) -> None:
    """The approved marketing template: 'Hi {{1}}, here's an update from {{2}}: {{3}} …' with 'Get my offer' (when there's
    an offer) and 'Stop offers'."""
    from ..config import get_settings
    from ..whatsapp import client
    s = get_settings()
    name = s.wa_promo_offer_template if p.get("keyword") else s.wa_promo_template
    comps = [{"type": "body", "parameters": [{"type": "text", "text": str(p[k])} for k in ("name", "brand", "text")]}]
    btns = ([f"kw:{p['keyword']}"] if p.get("keyword") else []) + [f"stop:{p['tenant_id']}"]
    comps += [{"type": "button", "sub_type": "quick_reply", "index": str(i), "parameters": [{"type": "payload", "payload": b}]} for i, b in enumerate(btns)]
    client._post({"to": client.to_wa(to), "type": "template", "template": {"name": name, "language": {"code": "en"}, "components": comps}})


def opt_out(phone: str, tenant_id: str) -> str | None:
    """'Stop offers' tapped: no more broadcasts from that business to this number."""
    from ..db import system_session
    from ..models import Customer, Tenant
    from sqlalchemy import select
    p10 = re.sub(r"\D", "", phone)[-10:]
    with system_session() as db:
        t = db.get(Tenant, uuid.UUID(tenant_id))
        if t is None:
            return None
        rows = [c for c in db.scalars(select(Customer).where(Customer.tenant_id == t.id)) if re.sub(r"\D", "", c.phone)[-10:] == p10]
        if not rows:
            db.add(Customer(tenant_id=t.id, phone=p10, wa_optout=True))
        for c in rows:
            c.wa_optout = True
        return f"Done. You won't get offers from {t.app_name or t.name} on WhatsApp any more. Your bills still come as usual."
