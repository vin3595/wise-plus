"""wise-plus prices (per business, per month). Yearly: pay 10 months for 12; the bundle of all three, 9 for 12."""
from __future__ import annotations

PRICES = {"pos": 59900, "stock": 29900, "bills": 29900}
BUNDLE = 99900                          # POS + Inventory + Bill scan
PAID = list(PRICES)                     # services anyone can buy
SCANS_PER_MONTH = 150                   # with Bill scan
FOUNDING_PAISE = 499900                 # founding members: everything, first year, flat
FOUNDING_SLOTS = 50
PACKS = {"25": {"scans": 25, "paise": 10000}, "100": {"scans": 100, "paise": 30000}}


def price(modules: list[str], period: str) -> dict:
    if period == "founding":   # the full package only
        full = BUNDLE * 12
        return {"modules": list(PAID), "period": "founding", "bundle": True, "monthly_paise": BUNDLE, "amount_paise": FOUNDING_PAISE,
                "months_paid": 12, "saving_paise": full - FOUNDING_PAISE, "list_paise": sum(PRICES.values())}
    mods = [m for m in PAID if m in set(modules)]
    if not mods:
        raise ValueError("Pick at least one service")
    bundle = set(mods) == set(PAID)
    monthly = BUNDLE if bundle else sum(PRICES[m] for m in mods)
    months_paid = (9 if bundle else 10) if period == "yearly" else 1
    amount = monthly * months_paid
    full = monthly * (12 if period == "yearly" else 1)
    return {"modules": mods, "period": period, "bundle": bundle, "monthly_paise": monthly, "amount_paise": amount,
            "months_paid": months_paid, "saving_paise": full - amount if period == "yearly" else 0,
            "list_paise": sum(PRICES[m] for m in mods)}
