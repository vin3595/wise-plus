"""Reading sales exported from another POS (Petpooja, Posist, DotPe, Rista, Excel ...) — pure, unit-tested.

Two shapes are accepted, both as .xlsx or .csv:
  - item-wise: one row per item sold, with the bill number on every row (gives item-wise reports too)
  - bill-wise: one row per bill
The header row is found automatically (exports often start with a title and the restaurant's name), and
columns are matched by their usual names; anything not matched can be picked by hand (`mapping`).
"""
from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from openpyxl import load_workbook

MAX_ROWS = 200_000

# Our field -> header names other POS exports use (compared lower-case, spaces and punctuation removed).
FIELDS: "OrderedDict[str, tuple[str, list[str]]]" = OrderedDict([
    ("bill_no", ("Bill / invoice number", ["invoiceno", "invoicenumber", "billno", "billnumber", "invoice", "bill",
                                           "orderno", "ordernumber", "orderid", "receiptno", "receiptnumber", "kotno",
                                           "billid", "invoiceid", "transactionid", "docno"])),
    ("date", ("Date (or date and time)", ["date", "invoicedate", "billdate", "orderdate", "createdon", "createdat",
                                          "datetime", "billdatetime", "ordertime", "businessdate", "created", "timestamp"])),
    ("time", ("Time", ["time", "billtime", "invoicetime"])),
    ("item", ("Item name", ["item", "itemname", "product", "productname", "dish", "dishname", "menuitem",
                            "itemdescription", "particulars"])),
    ("variant", ("Size / variant", ["variation", "variant", "variantname", "size", "portion"])),
    ("category", ("Category", ["category", "categoryname", "itemcategory", "group", "menugroup"])),
    ("qty", ("Quantity", ["qty", "quantity", "itemqty", "itemquantity", "noofitems", "count"])),
    ("item_amount", ("Item amount", ["itemtotal", "itemamount", "itemvalue", "amount", "linetotal", "value",
                                     "totalprice", "itemprice", "netamount", "price"])),
    ("hsn", ("HSN / SAC", ["hsn", "hsncode", "sac", "hsnsac", "saccode"])),
    ("rate", ("GST rate %", ["taxrate", "gstrate", "taxpercent", "gstpercent", "rate"])),
    ("gross", ("Sub total (before discount)", ["subtotal", "myamount", "mytotal", "grossamount", "gross", "billamount", "itemsubtotal"])),
    ("discount", ("Discount", ["discount", "discountamount", "totaldiscount", "disc"])),
    ("taxable", ("Taxable value", ["taxable", "taxableamount", "taxablevalue", "netsales", "aftertaxable"])),
    ("tax", ("Total tax", ["tax", "gst", "totaltax", "taxamount", "gstamount", "totalgst"])),
    ("cgst", ("CGST", ["cgst", "cgstamount", "cgst25", "centralgst"])),
    ("sgst", ("SGST", ["sgst", "sgstamount", "sgst25", "stategst", "utgst"])),
    ("igst", ("IGST", ["igst", "igstamount"])),
    ("round_off", ("Round off", ["roundoff", "rounding", "roundoffamount"])),
    ("total", ("Bill total", ["grandtotal", "total", "billtotal", "nettotal", "totalamount", "invoicetotal",
                              "finaltotal", "payable", "amountpayable", "invoiceamount", "net"])),
    ("payment", ("Payment type", ["paymenttype", "paymentmode", "payment", "paymentmethod", "paidby", "settlement",
                                  "modeofpayment", "tender", "paymode"])),
    ("payment_desc", ("Payment details (split)", ["paymentdescription", "paymentdetails", "paymentbreakup", "splitdetails"])),
    ("order_type", ("Order type", ["ordertype", "type", "servicetype", "ordersource", "channel", "area"])),
    ("status", ("Status", ["status", "orderstatus", "billstatus", "invoicestatus"])),
    ("customer_name", ("Customer name", ["customername", "customer", "name", "guestname", "custname"])),
    ("customer_phone", ("Customer phone", ["customerphone", "phone", "mobile", "mobileno", "customermobile",
                                           "phoneno", "contactno", "custphone"])),
    ("customer_gstin", ("Customer GSTIN", ["gstin", "customergstin", "gstno", "customergst"])),
])
REQUIRED = ("bill_no", "date")


def norm(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


@dataclass
class Issue:
    row: int
    message: str


@dataclass
class Line:
    item: str
    variant: str
    category: str | None
    qty: int
    amount: int | None            # paise, as the POS showed it (may be before or after discount)
    hsn: str | None
    rate_bp: int | None


@dataclass
class PBill:
    bill_no: str
    day: date
    at: datetime | None
    row: int
    values: dict = field(default_factory=dict)   # bill-level fields, first value seen
    pays: dict = field(default_factory=dict)     # mode -> paise (when the file splits payments)
    lines: list[Line] = field(default_factory=list)


@dataclass
class Parsed:
    headers: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)   # field -> header
    bills: list[PBill] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    item_wise: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.bills)


# ----------------------------------------------------------------- reading the file

def _rows(data: bytes, filename: str) -> list[list]:
    if filename.lower().endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|") if text.strip() else csv.excel
        return [r for r in csv.reader(io.StringIO(text), dialect)]
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    best: list[list] = []
    for ws in wb.worksheets:                     # the sheet with the most rows is the data
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if len(rows) > len(best):
            best = rows
    return best


def _guess(headers: list[str]) -> dict[str, str]:
    """Match columns to our fields: exact names first, then names that start with or contain one."""
    out: dict[str, str] = {}
    used: set[str] = set()
    normed = [(h, norm(h)) for h in headers if norm(h)]
    for strict in (True, False):
        for f, (_, names) in FIELDS.items():
            if f in out:
                continue
            for n in names:
                hit = next((h for h, k in normed if h not in used and (k == n if strict else (
                    len(n) > 4 and n in k and not k.startswith(("non", "not", "without"))))), None)
                if hit:
                    out[f] = hit
                    used.add(hit)
                    break
    return out


def find_header(rows: list[list]) -> int:
    """Exports start with titles and blank rows: the header is the early row matching the most field names."""
    best, best_i = 0, 0
    for i, r in enumerate(rows[:30]):
        cells = [norm(c) for c in r if c not in (None, "")]
        score = sum(1 for c in cells if any(c == n for _, names in FIELDS.values() for n in names))
        if score > best:
            best, best_i = score, i
    return best_i


# ----------------------------------------------------------------- values

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_when(v) -> tuple[date, datetime | None] | None:
    """Excel dates, '24/11/2026', '24-11-2026 13:05', '2026-11-24 13:05:22', '24-Nov-2026 1:05 PM'. Day first."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date(), (v if (v.hour, v.minute, v.second) != (0, 0, 0) else None)
    if isinstance(v, date):
        return v, None
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:      # an Excel serial number
        d = datetime(1899, 12, 30) + timedelta(days=float(v))
        return d.date(), (d if d.time() != time(0) else None)
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](.*))?$", s)
    if m:
        y, mo, d, rest = int(m[1]), int(m[2]), int(m[3]), m[4]
    else:
        m = re.match(r"^(\d{1,2})[/\-. ]([A-Za-z]{3,9}|\d{1,2})[/\-., ]+(\d{2,4})(?:[ ,T]+(.*))?$", s)
        if not m:
            return None
        d, mo_s, y, rest = int(m[1]), m[2], int(m[3]), m[4]
        mo = _MONTHS.get(mo_s[:3].lower()) if mo_s.isalpha() else int(mo_s)
        if not mo:
            return None
        if y < 100:
            y += 2000
    try:
        day = date(y, mo, d)
    except ValueError:
        return None
    t = parse_time(rest) if rest else None
    return day, (datetime.combine(day, t) if t else None)


def parse_time(v) -> time | None:
    if v is None or v == "":
        return None
    if isinstance(v, time):
        return v
    if isinstance(v, datetime):
        return v.time()
    if isinstance(v, float) and 0 <= v < 1:                           # Excel time as a fraction of a day
        secs = round(v * 86400)
        return time(secs // 3600 % 24, secs // 60 % 60, secs % 60)
    m = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AaPp][Mm])?", str(v))
    if not m:
        return None
    h, mi, se = int(m[1]), int(m[2]), int(m[3] or 0)
    if m[4]:
        h = h % 12 + (12 if m[4].lower() == "pm" else 0)
    return time(h % 24, mi, se) if mi < 60 and se < 60 else None


def money(v) -> int | None:
    """Rupees to paise. '1,234.50', '₹ 99', '(12.00)' and '-12' all work; blanks are None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v) * 100))
    s = str(v).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("INR", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s in ("", "-", "--", "NA", "N/A", "na"):
        return None
    try:
        p = int(round(float(s) * 100))
    except ValueError:
        return None
    return -p if neg else p


def number(v) -> float | None:
    p = money(v)
    return None if p is None else p / 100


PAY_WORDS = [("complimentary", "complimentary"), ("comp", "complimentary"), ("free", "complimentary"),
             ("due", "due"), ("credit", "due"), ("unpaid", "due"), ("pending", "due"),
             ("upi", "upi"), ("gpay", "upi"), ("googlepay", "upi"), ("phonepe", "upi"), ("paytm", "upi"),
             ("bhim", "upi"), ("qr", "upi"), ("wallet", "upi"),
             ("card", "card"), ("debit", "card"), ("visa", "card"), ("master", "card"), ("rupay", "card"), ("edc", "card"),
             ("pinelabs", "card"), ("swipe", "card"),
             ("zomato", "online"), ("swiggy", "online"), ("magicpin", "online"), ("eatsure", "online"),
             ("online", "online"), ("ondc", "online"), ("aggregator", "online"),
             ("cash", "cash")]


def pay_mode(v) -> str | None:
    """'Cash', 'UPI', 'Card', 'Paytm', 'Zomato Pay'... -> cash | upi | card | online | due | complimentary.
    'Part Payment' / 'Split' -> None (the caller keeps the split columns, or calls it 'other')."""
    s = norm(v)
    if not s:
        return None
    if s in ("part", "partpayment", "split", "multiple", "mixed"):
        return None
    for w, m in PAY_WORDS:
        if w in s:
            return m
    return "other"


def split_pays(desc, total: int) -> dict | None:
    """'Cash - 200.00, Card - 100.00' / 'Cash:200 UPI:150' -> {cash: 20000, card: 10000}, if it adds up to the total."""
    if not desc:
        return None
    out: dict[str, int] = {}
    for word, amt in re.findall(r"([A-Za-z][A-Za-z ._/]*?)\s*[:\-=]?\s*(?:Rs\.?|₹)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", str(desc)):
        m = pay_mode(word)
        a = money(amt)
        if m and a:
            out[m] = out.get(m, 0) + a
    return out if out and sum(out.values()) == total else None


def order_type(v) -> str | None:
    s = norm(v)
    if not s:
        return None
    if "deliver" in s or "zomato" in s or "swiggy" in s or "online" in s:
        return "delivery"
    if "dine" in s or "table" in s:
        return "dine_in"
    if "pick" in s or "take" in s or "parcel" in s or "counter" in s:
        return "takeaway"
    return None


def cancelled(v) -> bool:
    s = norm(v)
    return any(w in s for w in ("cancel", "void", "deleted", "rejected", "refund"))


def rate_bp(v) -> int | None:
    n = number(v)
    if n is None:
        return None
    if 0 < n < 1:                     # 0.05 meaning 5 %
        n *= 100
    return int(round(n * 100)) if 0 <= n <= 40 else None


STANDARD_RATES = (0, 500, 1200, 1800, 2800)


def nearest_rate(tax: int, taxable: int) -> int:
    if taxable <= 0:
        return 500
    r = tax * 10000 / taxable
    return min(STANDARD_RATES, key=lambda s: abs(s - r))


# ----------------------------------------------------------------- the whole file

def parse(data: bytes, filename: str, mapping: dict[str, str] | None = None) -> Parsed:
    p = Parsed()
    try:
        rows = _rows(data, filename)
    except Exception:  # noqa: BLE001 - openpyxl/csv raise many kinds
        p.errors.append(Issue(0, "Couldn't read the file. Upload the Excel (.xlsx) or .csv your POS exported."))
        return p
    if not rows:
        p.errors.append(Issue(0, "The file is empty"))
        return p
    if len(rows) > MAX_ROWS:
        p.errors.append(Issue(0, f"Too many rows ({len(rows):,}). Export a few months at a time (max {MAX_ROWS:,} rows)."))
        return p

    hi = find_header(rows)
    headers = [str(c).strip() if c not in (None, "") else "" for c in rows[hi]]
    p.headers = [h for h in headers if h]
    guess = _guess(p.headers)
    for f, h in (mapping or {}).items():          # the person's own choices win; "" = don't use
        if f in FIELDS:
            if h:
                guess[f] = h
            else:
                guess.pop(f, None)
    p.mapping = {f: h for f, h in guess.items() if h in p.headers}
    missing = [FIELDS[f][0] for f in REQUIRED if f not in p.mapping]
    if "total" not in p.mapping and "item_amount" not in p.mapping:
        missing.append("Bill total (or item amount)")
    if missing:
        p.errors.append(Issue(hi + 1, "Pick the column for: " + ", ".join(missing)))
        return p
    p.item_wise = "item" in p.mapping
    if not p.item_wise and "total" not in p.mapping and "item_amount" in p.mapping:
        p.mapping["total"] = p.mapping.pop("item_amount")      # one row per bill: its amount is the bill total
    col = {f: headers.index(h) for f, h in p.mapping.items()}

    def cell(r, f):
        i = col.get(f)
        return r[i] if i is not None and i < len(r) else None

    bills: "OrderedDict[tuple[str, date], PBill]" = OrderedDict()
    last: PBill | None = None
    for n, r in enumerate(rows[hi + 1:], start=hi + 2):
        if not any(c not in (None, "") for c in r):
            continue
        raw_no = cell(r, "bill_no")
        no = str(int(raw_no) if isinstance(raw_no, float) and raw_no.is_integer() else raw_no or "").strip()
        if not no and last is not None and p.item_wise and cell(r, "item"):
            b = last                                   # item rows under the bill's first row
        else:
            if not no:
                continue                               # totals / footer rows
            if norm(no) in ("total", "grandtotal", "subtotal", "min", "max", "avg", "average", "count", "sum"):
                continue
            if cell(r, "date") in (None, ""):
                continue                               # summary rows (Total / Min. / Max. / Avg.) have no date
            when = parse_when(cell(r, "date"))
            if when is None:
                p.errors.append(Issue(n, f"Bill {no}: date '{cell(r, 'date')}' isn't a date"))
                if len(p.errors) > 50:
                    break
                continue
            day, at = when
            if at is None and "time" in col:
                t = parse_time(cell(r, "time"))
                at = datetime.combine(day, t) if t else None
            key = (no, day)
            b = bills.get(key)
            if b is None:
                b = bills[key] = PBill(no, day, at, n)
        last = b
        for f in ("gross", "discount", "taxable", "tax", "cgst", "sgst", "igst", "round_off", "total"):
            v = money(cell(r, f))
            if v is not None and f not in b.values:
                b.values[f] = v
        for f in ("order_type", "status", "customer_name", "customer_phone", "customer_gstin", "payment", "payment_desc"):
            v = cell(r, f)
            if v not in (None, "") and f not in b.values:
                b.values[f] = str(v).strip()
        if p.item_wise and cell(r, "item") not in (None, ""):
            q = number(cell(r, "qty"))
            qty = int(round(q)) if q else 1
            b.lines.append(Line(str(cell(r, "item")).strip()[:120], str(cell(r, "variant") or "").strip()[:60] or "Regular",
                                (str(cell(r, "category")).strip()[:60] or None) if cell(r, "category") else None,
                                qty, money(cell(r, "item_amount")), (str(cell(r, "hsn")).strip()[:8] or None) if cell(r, "hsn") else None,
                                rate_bp(cell(r, "rate"))))
    p.bills = list(bills.values())
    if not p.bills and not p.errors:
        p.errors.append(Issue(hi + 1, "No bills found under the header row"))
    return p


# ----------------------------------------------------------------- one bill's figures

@dataclass
class Figures:
    status: str
    gross: int
    discount: int
    taxable: int
    cgst: int
    sgst: int
    igst: int
    round_off: int
    total: int
    pays: dict
    order_type: str | None
    lines: list[dict]


def figures(b: PBill) -> Figures:
    """Works out every amount we keep from whatever the POS gave, so the bill adds up:
    gross - discount = taxable; taxable + GST + round off = total."""
    v = b.values
    line_sum = sum(l.amount for l in b.lines if l.amount is not None) if b.lines else None
    total = v.get("total")
    if total is None:
        total = line_sum or 0
    tax_split = [v.get("cgst"), v.get("sgst"), v.get("igst")]
    tax = v.get("tax")
    if tax is None and any(x is not None for x in tax_split):
        tax = sum(x or 0 for x in tax_split)
    ro = v.get("round_off") or 0
    discount = abs(v.get("discount") or 0)
    taxable = v.get("taxable")
    if taxable is None:
        if tax is not None:
            taxable = total - tax - ro
        elif v.get("gross") is not None:
            taxable = v["gross"] - discount
        else:
            taxable = total - ro
    if tax is None:
        tax = max(0, total - taxable - ro)
    ro = total - taxable - tax                    # whatever is left is round off
    gross = v.get("gross")
    if gross is None or gross - discount != taxable:
        gross = taxable + discount
    if any(x is not None for x in tax_split):
        cgst, sgst, igst = (x or 0 for x in tax_split)
        if cgst + sgst + igst != tax:
            igst = 0 if not v.get("igst") else igst
            cgst = tax // 2 if not igst else 0
            sgst = tax - cgst - igst
    else:
        cgst, sgst, igst = tax // 2, tax - tax // 2, 0

    status = "cancelled" if cancelled(v.get("status")) else "paid"
    mode = pay_mode(v.get("payment"))
    split = split_pays(v.get("payment_desc"), total) if mode is None or mode == "other" else None
    pays = dict(b.pays) if b.pays else split or ({mode or "other": total} if total else {})
    if status == "paid" and mode == "complimentary":
        pays = {"complimentary": total}

    # Lines: spread the bill's taxable value and GST over the items by their amounts, so items and GST add up.
    lines: list[dict] = []
    if b.lines:
        weights = [max(0, l.amount) if l.amount is not None else 0 for l in b.lines]
        if not sum(weights):
            weights = [l.qty for l in b.lines]
        whole = sum(weights) or 1
        rate = nearest_rate(tax, taxable)
        left_tx, left_tax, left_g = taxable, tax, gross
        for i, (l, w) in enumerate(zip(b.lines, weights)):
            last = i == len(b.lines) - 1
            tx = left_tx if last else taxable * w // whole
            tt = left_tax if last else tax * w // whole
            g = left_g if last else gross * w // whole
            left_tx, left_tax, left_g = left_tx - tx, left_tax - tt, left_g - g
            lines.append({"item": l.item, "variant": l.variant, "category": l.category, "qty": l.qty,
                          "gross": g, "discount": g - tx, "taxable": tx, "tax": tt, "hsn": l.hsn,
                          "rate_bp": l.rate_bp if l.rate_bp is not None else rate})
    return Figures(status, gross, discount, taxable, cgst, sgst, igst, ro, total, pays, order_type(v.get("order_type")), lines)
