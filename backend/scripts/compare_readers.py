"""Read the same bills with Gemini, Claude Haiku and Claude Sonnet and compare.

    python -m scripts.compare_readers BILL.jpg [MORE.jpg ...] [--sheet SHEET.jpg] [--models a,b,c]

For each bill and model: supplier, invoice, date, total, number of lines, whether the lines add up to
the bill total, time taken and what the call cost (₹, from the tokens each API reports).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import time

from app.daily_sheet import extract as dsx
from app.purchases_ai import extraction as ex
from app.purchases_ai import gemini as g

INR = 88.0
MODELS = ["gemini-3.5-flash-lite", "claude-haiku-5-5", "claude-sonnet-5-5"]


def _cost(since: int) -> tuple[float, int, int]:
    rows = g.USAGE[since:]
    return sum(r["usd"] for r in rows) * INR, sum(r["in"] for r in rows), sum(r["out"] for r in rows)


async def bill(path: str, model: str) -> dict:
    data = open(path, "rb").read()
    png = ex.get_first_page_png(data, mimetypes.guess_type(path)[0] or "image/jpeg", path)
    n, t = len(g.USAGE), time.monotonic()
    try:
        b = await ex.extract_bill_from_png_bytes(png, model)
    except Exception as e:  # noqa: BLE001
        return {"model": model, "error": str(e)[:160]}
    rs, tin, tout = _cost(n)
    items = b.get("items") or []
    s = round(sum(float(i.get("total") or 0) for i in items), 2)
    tot = b.get("total") or 0
    return {"model": model, "secs": round(time.monotonic() - t, 1), "rs": round(rs, 3), "tokens": f"{tin}/{tout}",
            "vendor": b.get("vendor"), "invoice": b.get("invoice_number"), "date": b.get("bill_date"), "total": tot,
            "lines": len(items), "lines_sum": s, "adds_up": bool(tot) and abs(s - tot) <= max(2, 0.02 * tot),
            "items": [(i.get("name"), i.get("quantity"), i.get("unit_price"), i.get("total")) for i in items]}


async def sheet(path: str, model: str) -> dict:
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    real = dsx.LlmChat.with_model
    dsx.LlmChat.with_model = lambda self, provider, m: real(self, provider, model)   # force the model
    n, t = len(g.USAGE), time.monotonic()
    try:
        d = await dsx.extract_daily_sheet(b64)
    except Exception as e:  # noqa: BLE001
        return {"model": model, "error": str(e)[:160]}
    finally:
        dsx.LlmChat.with_model = real
    rs, tin, tout = _cost(n)
    return {"model": model, "secs": round(time.monotonic() - t, 1), "rs": round(rs, 3), "tokens": f"{tin}/{tout}",
            "date": d.get("date"), "cash_in": (d.get("cash_in") or {}), "cash_out": (d.get("cash_out") or {}),
            "expenses": d.get("expenses"), "absent": d.get("absent"), "advances": d.get("advances")}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bills", nargs="*")
    ap.add_argument("--sheet", action="append", default=[])
    ap.add_argument("--models", default=",".join(MODELS))
    a = ap.parse_args()
    models = a.models.split(",")
    out = {"bills": {}, "sheets": {}}
    for p in a.bills:
        out["bills"][p] = [await bill(p, m) for m in models]
    for p in a.sheet:
        out["sheets"][p] = [await sheet(p, m) for m in models]
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
