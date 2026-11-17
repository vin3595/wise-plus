"""Files on WhatsApp: *tally* (last month's CA pack), *tally oct* / *tally this month*, *backup* (this business's data).
Sent as a document from a private link that expires in a few hours."""
from __future__ import annotations

import re
import uuid
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..assistant.answers import Reply
from ..core.kds import business_date
from ..core.permissions import Perm, Role
from ..deps import Auth

TALLY = {"tally", "ca", "accounts", "ca pack", "books", "hisab kitab"}
BACKUP = {"backup", "back up", "my data", "export data", "data"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def period(words: list[str], today: date) -> tuple[date, date, str]:
    """'' = last month; 'this' / 'this month'; a month name ('oct', 'october 2026')."""
    first = today.replace(day=1)
    if "this" in words:
        return first, today, f"{today:%B %Y} so far"
    for w in words:
        m = MONTHS.get(w[:3])
        if m:
            y = next((int(x) for x in words if re.fullmatch(r"20\d\d", x)), today.year if m <= today.month else today.year - 1)
            a = date(y, m, 1)
            b = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
            return a, min(b, today), f"{a:%B %Y}"
    b = first - timedelta(days=1)
    return b.replace(day=1), b, f"{b:%B %Y}"


MSGS = {"messages", "buy messages", "message pack", "msgs", "whatsapp bills"}


def messages(db: Session, tenant_id) -> Reply:
    from ..billing.service import msgs
    from ..models import Tenant
    from ..services.sms import app_base_url
    t = db.get(Tenant, uuid.UUID(tenant_id))
    m = msgs(db, t)
    if m["unlimited"]:
        return Reply(f"📨 Bills on WhatsApp this month: {m['used']}. No limit on this account.")
    return Reply(f"📨 Bills on WhatsApp this month: {m['used']} of {m['allowance']} free ({m['per_outlet']} per outlet)"
                 + (f" + {m['bonus']} bought" if m["bonus"] else "") + f".\nLeft: *{m['left']}*.\n\n"
                 "More: 1,000 for ₹199 · 5,000 for ₹799 (never expire).\n"
                 f"Buy: {app_base_url(t)}/billing")


def handle(db: Session, chosen: tuple, word: str) -> Reply | None:
    if word in MSGS:
        return messages(db, chosen[0])
    words = word.split()
    if not words or (words[0] not in TALLY and word not in TALLY and word not in BACKUP):
        return None
    tenant_id, login_id, role, user_id = chosen
    auth = Auth(user_id=uuid.UUID(user_id), tenant_id=uuid.UUID(tenant_id), role=Role(role), outlet_id=None, via="otp")
    from ..exports import make_link
    if word in BACKUP:
        if role != Role.owner.value:
            return Reply("Only the owner can download the business's data.")
        from ..tenant_backup import export_zip
        data, name = export_zip(db, auth.tenant_id)
        return Reply("📦 Your data: every bill, menu, customer, stock, purchase, supplier, bank line and bill photo, "
                     "in Excel and as a full restore file.\nThe link works for 6 hours.", document=(make_link(data, name), name))
    if not auth.can(Perm.reports_view):
        return Reply("Your account can't download the accounts. Ask the owner.")
    a, b, label = period(words[1:], business_date())
    from ..routers.accounts import ca_pack
    resp = ca_pack(a, b, None, None, auth, db)
    name = f"accounts-{a:%Y-%m}.zip"
    return Reply(f"📒 Accounts for {label}: Tally ledgers + vouchers, Excel with the GST summary, and the purchase bills.\n"
                 "Forward it to your CA. The link works for 6 hours.",
                 [("tally this month", "This month so far")], document=(make_link(resp.body, name), name))
