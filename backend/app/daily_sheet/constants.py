# Ported from Ease My Bill (receipt-analyzer/backend/sheet_constants.py).
"""
Fixed item labels that appear on the Shakewallah daily sheet.

These are hard-coded because the sheet is a printed template with the same
row labels every day. Extra items handwritten by the user in the margins
are appended by the extractor at runtime (see daily_sheet._normalize_named).

DO NOT reorder — the display order in the frontend mirrors this ordering
so the UI matches the physical sheet.
"""

# Drinks/raw-materials that come IN from Vijay Ras Bhandar (owner's father's
# store). Filled with quantities on the sheet.
VIJAY_RAS_BHANDAR_ITEMS: list[str] = [
    "STRAWBERRY", "SITAFAL", "MANGO", "CHIKOO", "GAS",
    "RED BULL", "BISLERI", "DRY FRUIT", "PINEAPPLE",
    "WATERMELON", "KAJU", "PISTA", "BADAAM",
]

# Daily expense rows on the sheet (main table + right-side extras column).
# MUMMY, BHAVESH, VINEET are printed rows that act as staff-payout cells.
EXPENSE_ITEMS: list[str] = [
    "MILK", "BROWNIE", "SNACK", "TEA", "15 AD",
    "F&G", "LOCAL", "PETROL", "VEGETABLE", "PANEER",
    "BLINKIT", "TAXI", "RAPIDO", "GAS",
    "MUMMY", "BHAVESH", "VINEET",
]

# Items sent OUT to Vijay Ras (i.e., we sell to father's store).
VIJAY_RAS_ITEMS: list[str] = ["KITKAT", "OREO", "MILK", "VANILLA"]

# Rupee denominations tracked in the Cash In/Out grid.
DENOMS: list[int] = [500, 200, 100, 50, 20, 10]


# ---------------------------------------------------------------------------
# Expense category auto-mapping
# ---------------------------------------------------------------------------
# When we route extracted expense rows into EMB's Expense collection, each
# row needs a category (category_key + category_label). These defaults are
# applied automatically at preview time; the user can override any of them
# before hitting "Save Everything".
#
# The keys here match EMB's normalized category slugs (see seed_preset_
# categories in server.py). If EMB doesn't have a matching key yet we
# create a "misc" entry — the user can rename it later.
# ---------------------------------------------------------------------------
EXPENSE_LABEL_TO_CATEGORY: dict[str, tuple[str, str]] = {
    # label (uppercase) -> (category_key, category_label)
    "MILK":     ("inventory", "Inventory"),
    "BROWNIE":  ("inventory", "Inventory"),
    "SNACK":    ("inventory", "Inventory"),
    "PANEER":   ("inventory", "Inventory"),
    "DRY FRUIT": ("inventory", "Inventory"),
    "VEGETABLE": ("inventory", "Inventory"),

    "TEA":       ("beverages", "Beverages"),
    "F&G":       ("groceries", "Groceries"),
    "BLINKIT":   ("groceries", "Groceries"),
    "LOCAL":     ("local_purchases", "Local Purchases"),
    "15 AD":     ("marketing", "Marketing"),

    "PETROL":  ("transport", "Transport"),
    "TAXI":    ("transport", "Transport"),
    "RAPIDO":  ("transport", "Transport"),

    "GAS":     ("utilities", "Utilities"),

    # MUMMY / BHAVESH / VINEET rows on the expenses table represent staff
    # payouts. We map them to a Salary category by default. The Save step
    # ALSO creates a StaffTransaction(type='salary_payment') if a matching
    # staff record is found (see save_daily_sheet).
    "MUMMY":   ("salary", "Salary"),
    "BHAVESH": ("salary", "Salary"),
    "VINEET":  ("salary", "Salary"),
}


def category_for_label(label: str) -> tuple[str, str]:
    """Return (category_key, category_label) for an expense row label.

    Unknown labels fall back to ("misc", "Miscellaneous"). We uppercase +
    trim before lookup so subtle sheet variations still match.
    """
    key = (label or "").strip().upper()
    if key in EXPENSE_LABEL_TO_CATEGORY:
        return EXPENSE_LABEL_TO_CATEGORY[key]
    return ("misc", "Miscellaneous")
