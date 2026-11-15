"""Read a bank statement: Excel / CSV exactly (column detection), PDF and photos with the AI reader, page by page.

Returns {"bank", "last4", "period_from", "period_to", "opening_paise", "closing_paise",
         "rows": [{"date", "narration", "ref", "amount_paise" (+in / -out), "balance_paise"}]}."""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import re
import uuid
from datetime import date, datetime


class BankParseError(Exception):
    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def to_date(x) -> date | None:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    s = str(x or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})[/\-. ](\d{1,2}|[A-Za-z]{3,9})[/\-. ,]*(\d{2,4})\b", s)
        if not m:
            return None
        d, mo, y = m.group(1), m.group(2), m.group(3)
        d = int(d)
        mo = int(mo) if mo.isdigit() else MONTHS.get(mo[:3].lower(), 0)
        y = int(y)
        y = y + 2000 if y < 100 else y
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def to_paise(x) -> int | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return int(round(float(x) * 100))
    s = str(x).strip().replace(",", "").replace("₹", "").replace("INR", "").strip()
    if not s or s in ("-", "--"):
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    m = re.search(r"\d+(\.\d+)?", s)
    if not m:
        return None
    v = int(round(float(m.group(0)) * 100))
    if re.search(r"\bdr\b", s.lower()):
        neg = True
    return -v if neg else v


# ------------------------------------------------------------------ spreadsheets

HEAD = {
    "date": re.compile(r"^(txn |transaction |value |tran |post(ing)? )?date|^date$|^dt$"),
    "narration": re.compile(r"narration|description|particulars|remarks|details|transaction remarks"),
    "ref": re.compile(r"ref|chq|cheque|utr"),
    "debit": re.compile(r"debit|withdrawal|dr\b|paid out|money out"),
    "credit": re.compile(r"credit|deposit|cr\b|paid in|money in"),
    "amount": re.compile(r"^amount|txn amount|transaction amount"),
    "drcr": re.compile(r"^(dr ?/ ?cr|cr ?/ ?dr|type|debit/credit)$"),
    "balance": re.compile(r"balance"),
}


def _header(row: list) -> dict | None:
    cells = [str(c or "").strip().lower() for c in row]
    found: dict = {}
    for i, c in enumerate(cells):
        if not c:
            continue
        for k in ("balance", "date", "narration", "drcr", "amount", "debit", "credit", "ref"):
            if k not in found and HEAD[k].search(c) and not (k == "date" and "value" in c and "date" in found):
                if k in ("debit", "credit") and "balance" in c:
                    continue
                found[k] = i
                break
    ok = "date" in found and "narration" in found and (("debit" in found and "credit" in found) or "amount" in found)
    return found if ok else None


def rows_from_table(table: list[list]) -> list[dict]:
    head, start = None, 0
    for i, r in enumerate(table[:60]):
        head = _header(r)
        if head:
            start = i + 1
            break
    if not head:
        return []
    out = []
    for r in table[start:]:
        r = list(r) + [None] * 12
        d = to_date(r[head["date"]])
        if d is None:
            continue
        narr = re.sub(r"\s+", " ", str(r[head["narration"]] or "")).strip()
        if "debit" in head:
            dr, cr = to_paise(r[head["debit"]]), to_paise(r[head["credit"]])
            amt = (abs(cr) if cr else 0) - (abs(dr) if dr else 0)
        else:
            a = to_paise(r[head["amount"]]) or 0
            t = str(r[head["drcr"]] or "").lower() if "drcr" in head else ""
            amt = -abs(a) if t.startswith("d") else abs(a) if t.startswith("c") else a
        if not amt:
            continue
        bal = to_paise(r[head["balance"]]) if "balance" in head else None
        ref = str(r[head["ref"]]).strip() if "ref" in head and r[head["ref"]] not in (None, "") else None
        out.append({"date": d, "narration": narr[:300] or "—", "ref": (ref or None) and ref[:60], "amount_paise": amt, "balance_paise": bal})
    return out


def read_table_file(data: bytes, name: str) -> list[list] | None:
    n = name.lower()
    try:
        if n.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            return [list(r) for r in ws.iter_rows(values_only=True)]
        if n.endswith(".xls") or data[:4] == b"\xd0\xcf\x11\xe0":
            try:
                import xlrd
            except ImportError as e:
                raise BankParseError("Save the statement as .xlsx or CSV and send it again") from e
            wb = xlrd.open_workbook(file_contents=data)
            sh = wb.sheet_by_index(0)
            rows = []
            for i in range(sh.nrows):
                row = []
                for j in range(sh.ncols):
                    c = sh.cell(i, j)
                    row.append(xlrd.xldate_as_datetime(c.value, wb.datemode) if c.ctype == xlrd.XL_CELL_DATE else c.value)
                rows.append(row)
            return rows
        if n.endswith((".csv", ".txt")) or b"," in data[:2000] and data[:4] != b"%PDF":
            text = data.decode("utf-8-sig", errors="replace")
            delim = max(",;\t|", key=lambda c: text[:5000].count(c))
            return [r for r in csv.reader(io.StringIO(text), delimiter=delim)]
    except BankParseError:
        raise
    except Exception:
        return None
    return None


def _meta_from_text(text: str) -> dict:
    t = text[:4000]
    bank = None
    for b in ("HDFC", "ICICI", "Axis", "State Bank of India", "SBI", "Kotak", "Bank of Baroda", "AU Small Finance", "Yes Bank",
              "IndusInd", "Punjab National", "Canara", "Union Bank", "IDFC", "Federal", "RBL"):
        if re.search(re.escape(b), t, re.I):
            bank = {"SBI": "State Bank of India", "AU Small Finance": "AU Bank"}.get(b, b + ("" if "Bank" in b else " Bank"))
            break
    m = re.search(r"(?:a/?c|account)\s*(?:no\.?|number|#)?\s*[:\-]?\s*[xX*\d ]*?(\d{4})\b", t, re.I)
    return {"bank": bank, "last4": m.group(1) if m else None}


# ------------------------------------------------------------------ PDF and photos (AI)

PROMPT = """You are reading a BANK ACCOUNT STATEMENT (Indian bank). Return ONLY JSON:
{"bank": "bank name or null", "last4": "last 4 digits of the account number or null",
 "opening_balance": number or null, "closing_balance": number or null,
 "rows": [{"date": "YYYY-MM-DD", "narration": "full description text", "ref": "cheque/ref no or null",
           "withdrawal": number or null, "deposit": number or null, "balance": number or null}]}
Rules: one row per transaction, in order, never invented; skip opening/closing/total lines and page headers.
withdrawal = money out (debit), deposit = money in (credit). Plain numbers, no commas. Dates like 09/10/26 are day/month/year.
If this text is only part of the statement, return just the rows in it."""


def _pdf_pages_text(data: bytes) -> list[str]:
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        return [pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), 40))]
    except Exception:
        return []


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no json")
    return json.loads(m.group(0))


async def _ask(text: str | None, png: bytes | None) -> dict:
    from ..purchases_ai.gemini import GEMINI_API_KEY, ImageContent, LlmChat, UserMessage
    if not GEMINI_API_KEY:
        raise BankParseError("The statement reader isn't set up on this server", 503)
    files = [ImageContent(image_base64=base64.b64encode(png).decode())] if png else []
    msg = UserMessage(text=("Statement text:\n\n" + text) if text else "Here is a page of the statement.", file_contents=files)
    for _ in range(2):
        try:
            chat = LlmChat(api_key=GEMINI_API_KEY, session_id=f"bank-{uuid.uuid4()}", system_message=PROMPT)
            return _parse_json(await chat.send_message(msg))
        except (ValueError, json.JSONDecodeError):
            continue
    raise BankParseError("Couldn't read part of that statement. Try the Excel or CSV download from net banking.")


def _ai_rows(raw: dict) -> list[dict]:
    out = []
    for r in raw.get("rows") or []:
        d = to_date(r.get("date"))
        wd, dp = to_paise(r.get("withdrawal")), to_paise(r.get("deposit"))
        amt = (abs(dp) if dp else 0) - (abs(wd) if wd else 0)
        if d is None or not amt:
            continue
        out.append({"date": d, "narration": re.sub(r"\s+", " ", str(r.get("narration") or "")).strip()[:300] or "—",
                    "ref": (str(r.get("ref")).strip()[:60] or None) if r.get("ref") not in (None, "", "null") else None,
                    "amount_paise": amt, "balance_paise": to_paise(r.get("balance"))})
    return out


async def read_statement(data: bytes, content_type: str, name: str) -> dict:
    meta: dict = {"bank": None, "last4": None, "opening_paise": None, "closing_paise": None}
    rows: list[dict] = []
    table = None if data[:4] == b"%PDF" or content_type.startswith("image/") else read_table_file(data, name)
    if table:
        rows = rows_from_table(table)
        flat = "\n".join(" ".join(str(c) for c in r if c not in (None, "")) for r in table[:25])
        meta.update(_meta_from_text(flat))
        if not rows:   # an odd layout: let the AI read it as text
            text = "\n".join(" | ".join("" if c is None else str(c) for c in r) for r in table)
            chunks = [text[i:i + 14000] for i in range(0, min(len(text), 200000), 14000)]
            parts = await asyncio.gather(*[_ask(c, None) for c in chunks])
            rows = [x for p in parts for x in _ai_rows(p)]
    elif data[:4] == b"%PDF" or content_type == "application/pdf" or name.lower().endswith(".pdf"):
        pages = _pdf_pages_text(data)
        if sum(len(p.strip()) for p in pages) > 200:
            meta.update(_meta_from_text(pages[0] if pages else ""))
            # two pages per request, all at once: a 20-page statement takes about as long as a 2-page one
            chunks = ["\n\n".join(pages[i:i + 2]) for i in range(0, len(pages), 2)]
            parts = await asyncio.gather(*[_ask(c, None) for c in chunks])
        else:
            from ..purchases_ai.extraction import pdf_to_page_pngs
            pngs = pdf_to_page_pngs(data, max_pages=12)
            parts = await asyncio.gather(*[_ask(None, p) for p in pngs])
        rows = [x for p in parts for x in _ai_rows(p)]
        first, last = (parts[0] if parts else {}), (parts[-1] if parts else {})
        meta["bank"] = meta["bank"] or first.get("bank")
        meta["last4"] = meta["last4"] or (re.sub(r"\D", "", str(first.get("last4") or ""))[-4:] or None)
        meta["opening_paise"], meta["closing_paise"] = to_paise(first.get("opening_balance")), to_paise(last.get("closing_balance"))
    elif not (content_type.startswith("image/") or name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic"))):
        raise BankParseError("Couldn't read that file. Send the PDF, Excel or CSV statement from net banking.")
    else:
        from ..purchases_ai.extraction import image_bytes_to_normalized_png
        raw = await _ask(None, image_bytes_to_normalized_png(data, max_side=2200))
        rows = _ai_rows(raw)
        meta.update(bank=raw.get("bank"), last4=(re.sub(r"\D", "", str(raw.get("last4") or ""))[-4:] or None),
                    opening_paise=to_paise(raw.get("opening_balance")), closing_paise=to_paise(raw.get("closing_balance")))
    if not rows:
        raise BankParseError("No transactions found in that file. Try the Excel or CSV download from net banking.")
    if not meta.get("last4"):
        m = re.search(r"(?:x{2,}|\*{2,}|a/?c|acct?)[ _-]*(\d{4})(?!\d)", name, re.I)
        meta["last4"] = m.group(1) if m else None
    if meta["closing_paise"] is None and rows[-1].get("balance_paise") is not None:
        meta["closing_paise"] = rows[-1]["balance_paise"]
    # Some banks list newest first: keep oldest first.
    if len(rows) > 1 and rows[0]["date"] > rows[-1]["date"]:
        rows.reverse()
        if rows[-1].get("balance_paise") is not None:
            meta["closing_paise"] = rows[-1]["balance_paise"]
    meta.update(rows=rows, period_from=min(r["date"] for r in rows), period_to=max(r["date"] for r in rows))
    return meta


def looks_like_bank_statement(data: bytes, content_type: str, name: str, caption: str = "") -> bool:
    if re.search(r"\b(bank statement|passbook|bank stmt|account statement of|acct statement)\b", f"{caption} {name}", re.I):
        return True
    t = ""
    if data[:4] == b"%PDF":
        t = " ".join(_pdf_pages_text(data)[:1])[:5000].lower()
    elif name.lower().endswith((".xlsx", ".xls", ".csv")):
        tab = read_table_file(data, name) or []
        return bool(rows_from_table(tab))
    return bool(re.search(r"\b(ifsc|withdrawal|deposits?|closing balance|statement of account)\b", t)
                and re.search(r"\b(bank|ifsc)\b", t) and not re.search(r"ledger account|vch type|voucher", t))
