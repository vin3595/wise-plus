"""Reading another POS's sales export (pure)."""
import io
from datetime import date, datetime

from openpyxl import Workbook

from app.posimport.parse import figures, money, parse, parse_when, pay_mode


def xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_values():
    assert money("1,234.50") == 123450 and money("₹ 99") == 9900 and money("(12)") == -1200 and money("") is None
    assert parse_when("24/11/2026 1:05 PM") == (date(2026, 11, 24), datetime(2026, 11, 24, 13, 5))
    assert parse_when("2026-04-01")[0] == date(2026, 4, 1)
    assert parse_when("05-Apr-26")[0] == date(2026, 4, 5)
    assert parse_when(datetime(2026, 5, 2, 21, 30))[1] == datetime(2026, 5, 2, 21, 30)
    assert [pay_mode(x) for x in ("Cash", "UPI", "Paytm", "Card", "Zomato Pay", "Due", "Part Payment", "Coupon")] == \
        ["cash", "upi", "upi", "card", "online", "due", None, "other"]


def test_item_wise_export_with_title_rows():
    rows = [["Shakewallah (Jodhpur)"], ["Item Wise Report: 01/04/2026 to 30/04/2026"], [],
            ["Invoice No.", "Date", "Item Name", "Variation", "Category", "Qty", "Item Total", "Discount", "Tax",
             "Round Off", "Grand Total", "Payment Type", "Order Type", "Status", "Customer Phone"],
            ["101", "01/04/2026 13:05", "Oreogasm", "300 ML", "Chocolate", 2, 360, 0, 18, 0, 378, "Cash", "Pick Up", "Success", "9829012345"],
            ["101", "01/04/2026 13:05", "KitKat", "300 ML", "Chocolate", 1, 180, 0, 9, 0, 189, "Cash", "Pick Up", "Success", ""],
            ["102", "01/04/2026 14:00", "Mango", "1000 ML", "Fruit", 1, 500, 50, 22.5, 0.5, 473, "UPI", "Delivery", "Success", ""],
            ["103", "02/04/2026 11:00", "Mango", "300 ML", "Fruit", 1, 170, 0, 8.5, 0.5, 179, "Cash", "Dine In", "Cancelled", ""],
            ["Total", "", "", "", "", 5, 1210]]
    p = parse(xlsx(rows), "items.xlsx")
    assert p.ok and p.item_wise and len(p.bills) == 3
    assert p.mapping["bill_no"] == "Invoice No." and p.mapping["total"] == "Grand Total"
    b101 = figures(p.bills[0])
    # Grand Total repeats on every row of the bill: the first one is the bill's.
    assert b101.total == 37800 and b101.taxable + b101.cgst + b101.sgst + b101.round_off == b101.total
    assert [ln["qty"] for ln in b101.lines] == [2, 1] and sum(ln["taxable"] for ln in b101.lines) == b101.taxable
    assert b101.pays == {"cash": 37800} and b101.order_type == "takeaway"
    b102 = figures(p.bills[1])
    assert b102.discount == 5000 and b102.total == 47300 and b102.pays == {"upi": 47300} and b102.round_off == 50
    assert figures(p.bills[2]).status == "cancelled"


def test_bill_wise_csv_and_hand_picked_columns():
    csv = ("Bill#,When,Amount,GST,Mode\n"
           "A1,05-04-2026,105,5,GPay\n"
           "A2,05-04-2026,210,10,cash\n").encode()
    p = parse(csv, "bills.csv")
    assert not p.ok and "Date" in p.errors[0].message and p.mapping["bill_no"] == "Bill#" and p.mapping["tax"] == "GST"
    p = parse(csv, "bills.csv", {"date": "When"})
    assert p.ok and p.mapping["total"] == "Amount" and figures(p.bills[1]).total == 21000
    p = parse(csv, "bills.csv", {"bill_no": "Bill#", "date": "When", "total": "Amount", "tax": "GST", "payment": "Mode"})
    assert p.ok and not p.item_wise and [figures(b).pays for b in p.bills] == [{"upi": 10500}, {"cash": 21000}]
    f = figures(p.bills[0])
    assert (f.taxable, f.cgst, f.sgst, f.total) == (10000, 250, 250, 10500)
