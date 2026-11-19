"""Ease My Bill price: one plan, everything included, ₹999 per outlet per month (3 outlets pay 3×).
The only extras are WhatsApp message packs and bill-scan packs. (Old per-service, yearly and founding plans are gone.)"""
from __future__ import annotations

PRICES = {"pos": 59900, "stock": 29900, "bills": 29900}
BUNDLE = 99900                          # POS + Inventory + Bill scan
PAID = list(PRICES)                     # services anyone can buy
SCANS_PER_MONTH = 150                   # with Bill scan
FOUNDING_PAISE = 499900                 # founding members: everything, first year, flat
FOUNDING_SLOTS = 50
PACKS = {"25": {"scans": 25, "paise": 10000}, "100": {"scans": 100, "paise": 30000}}
# Bills sent to customers on WhatsApp: a free allowance per outlet each month, then prepaid packs that never expire.
MSGS_PER_OUTLET = 500
MSG_PACKS = {"m1000": {"messages": 1000, "paise": 19900}, "m5000": {"messages": 5000, "paise": 79900}}
ALL_PACKS = PACKS | MSG_PACKS


def price(modules: list[str] | None = None, period: str = "monthly", outlets: int = 1) -> dict:
    """modules is ignored: the plan always has everything."""
    if period != "monthly":
        raise ValueError("There's one plan: ₹999 per outlet a month, everything included")
    n = max(int(outlets or 1), 1)
    return {"modules": list(PAID), "period": "monthly", "bundle": True, "monthly_paise": BUNDLE * n, "amount_paise": BUNDLE * n,
            "months_paid": 1, "saving_paise": 0, "list_paise": BUNDLE * n, "outlets": n, "per_outlet_paise": BUNDLE}
