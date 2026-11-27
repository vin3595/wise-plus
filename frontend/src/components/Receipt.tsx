import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { canPrintRaw, desktop, paintDelay, printNow, printRaw, withPrintLock } from "../desktop";
import { billBytes, logoBytes } from "../escpos";
import { formatPaise } from "../money";
import { QrImage } from "../qr";
import type { Bill, BillLine, TaxRow } from "../types";

const MODE = { cash: "Cash", upi: "UPI", card: "Card", due: "Credit (due)", complimentary: "Complimentary" } as const;
const pct = (bp: number) => `${bp / 100}%`;

/** 80 mm thermal bill, printed through the browser at the counter. */
export function Receipt({ bill, payQr }: { bill: Bill; payQr?: string | null }) {
  const o = bill.outlet;
  return (
    <div className="receipt">
      <div className="r-center">
        {o.logo_url && <img src={o.logo_url} alt="" className="r-logo" />}
        <strong className="r-brand">{o.brand}</strong>
        <div>{o.legal_name !== o.brand ? o.legal_name : ""}</div>
        <div>{o.name}</div>
        {o.address && <div>{o.address}</div>}
        {o.phone && <div>Ph: {o.phone}</div>}
        {o.gstin && <div>GSTIN: {o.gstin}</div>}
        {o.fssai && <div>FSSAI: {o.fssai}</div>}
        <div className="r-title">{bill.training ? "TRAINING - NOT A BILL" : bill.status === "cancelled" ? "CANCELLED TAX INVOICE" : "TAX INVOICE"}</div>
        {bill.training && <div>Practice only. No payment is due.</div>}
        {(bill.revision || 1) > 1 && <div>Revised bill (version {bill.revision})</div>}
      </div>
      <div>Bill no: <strong>{bill.invoice_no}</strong></div>
      <div>Date: {new Date(bill.created_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}</div>
      <div className="r-row">
        <span>Token #{bill.token_no}</span>
        <span>{bill.order_type === "dine_in" ? `Table ${bill.table_label}` : bill.car_number ? `Car ${bill.car_number}` : bill.order_type === "takeaway" ? "Takeaway" : "Delivery"}</span>
      </div>
      {(bill.customer_name || bill.customer_phone) && <div>Customer: {[bill.customer_name, bill.customer_phone].filter(Boolean).join(" · ")}</div>}
      {bill.customer_gstin && <div>Customer GSTIN: {bill.customer_gstin}</div>}
      <hr />
      <table className="r-items">
        <thead><tr><th>Item</th><th>Qty</th><th>Amt</th></tr></thead>
        <tbody>
          {bill.lines.map((l, i) => (
            <tr key={i}>
              <td>{l.item_name}{l.variant_name !== "Regular" ? ` (${l.variant_name})` : ""}{l.addons_text ? <div className="r-sub">+ {l.addons_text}</div> : null}
                <div className="r-sub">@ {formatPaise(l.unit_paise)}{l.hsn_sac ? ` · SAC ${l.hsn_sac}` : ""}</div></td>
              <td>{l.qty}</td>
              <td>{formatPaise(l.gross_paise)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <hr />
      <div className="r-row"><span>Subtotal</span><span>{formatPaise(bill.gross_paise)}</span></div>
      {bill.discount_paise > 0 && <div className="r-row"><span>Discount{bill.discount_reason ? ` (${bill.discount_reason})` : ""}</span><span>−{formatPaise(bill.discount_paise)}</span></div>}
      <div className="r-row"><span>Taxable value</span><span>{formatPaise(bill.taxable_paise)}</span></div>
      {bill.tax_summary.filter((t) => t.rate_bp > 0).map((t) => (
        <div key={t.rate_bp}>
          {t.igst_paise > 0 ? <div className="r-row"><span>IGST {pct(t.rate_bp)}</span><span>{formatPaise(t.igst_paise)}</span></div> : <>
            <div className="r-row"><span>CGST {pct(t.rate_bp / 2)}</span><span>{formatPaise(t.cgst_paise)}</span></div>
            <div className="r-row"><span>SGST {pct(t.rate_bp / 2)}</span><span>{formatPaise(t.sgst_paise)}</span></div>
          </>}
        </div>
      ))}
      {bill.round_off_paise !== 0 && <div className="r-row"><span>Round off</span><span>{bill.round_off_paise > 0 ? "" : "−"}{formatPaise(Math.abs(bill.round_off_paise))}</span></div>}
      <div className="r-row r-total"><span>TOTAL</span><span>{formatPaise(bill.total_paise)}</span></div>
      <div className="r-row"><span>Paid by</span><span>{bill.payments.map((p) => `${MODE[p.mode]} ${formatPaise(p.amount_paise)}`).join(", ")}</span></div>
      {bill.status === "cancelled" && <div className="r-center"><strong>Cancelled: {bill.cancel_reason}</strong></div>}
      {payQr && bill.status !== "cancelled" && (
        <div className="r-center r-payqr">
          <hr />
          <div><strong>Pay {formatPaise(bill.total_paise)} by UPI</strong></div>
          <QrImage text={payQr} size={170} label={`UPI QR to pay ${formatPaise(bill.total_paise)}`} />
          <div className="r-sub">Scan with GPay, PhonePe, Paytm or any UPI app</div>
        </div>
      )}
      <hr />
      <div className="r-center r-sub">{o.footer || "Thank you! Visit again."}</div>
      {bill.cashier_name && <div className="r-center r-sub">Billed by {bill.cashier_name}</div>}
    </div>
  );
}

const cashOnly = (b: Bill) => b.status !== "cancelled" && !b.training && b.total_paise > 0 && b.payments.length === 1 && b.payments[0].mode === "cash";

/** Prints one bill and calls onDone afterwards. A cash bill gets its own UPI QR when the outlet has
 * "QR on every bill" on: if the customer pays it, the bill turns to UPI by itself. */
export function PrintBill({ bill, onDone }: { bill: Bill; onDone: () => void }) {
  const [qr, setQr] = useState<string | null | undefined>(cashOnly(bill) && navigator.onLine ? undefined : null);
  useEffect(() => {
    if (qr !== undefined) return;
    let live = true;
    const give = (v: string | null) => { if (live) setQr(v); };
    const stop = setTimeout(() => give(null), 6000);   // never hold the bill up for long
    api<{ upi: string | null }>(`/bills/${bill.id}/pay-qr`, { method: "POST" })
      .then((r) => give(r.upi || null)).catch(() => give(null)).finally(() => clearTimeout(stop));
    return () => { live = false; clearTimeout(stop); };
  }, [bill.id, qr]);
  const [show, setShow] = useState(false);
  const done = useRef(onDone);
  done.current = onDone;
  const started = useRef(false);
  useEffect(() => {
    if (qr === undefined || started.current) return;
    started.current = true;             // print once, however often the screen redraws
    void withPrintLock(async () => {
      if (canPrintRaw()) {              // Windows app: printer text, sharp, and cut right after the last line
        try {
          const paper = Number((await desktop()!.settings().catch(() => ({ paperMm: 80 }))).paperMm) === 58 ? 58 : 80;
          const err = await printRaw("bill", billBytes(bill, qr || null, paper, await logoBytes(bill.outlet.logo_url, paper)));
          if (!err) return;
        } catch { /* fall back to the page print below */ }
      }
      setShow(true);
      await paintDelay(350);            // let the logo/QR images load
      await printNow("bill");
    }).finally(() => done.current());
  }, [qr]);
  return show ? createPortal(<div id="print-root"><Receipt bill={bill} payQr={qr || null} /></div>, document.body) : null;
}

/** What a UPI QR slip shows: the bill as it will be made, before it is paid. */
export interface QrSlipData {
  lines: BillLine[]; gross_paise: number; discount_paise: number; taxable_paise: number; round_off_paise: number; total_paise: number;
  tax_summary: TaxRow[]; token_no: number | null; order_type: string; table_label: string | null; car_number: string | null;
  outlet: Bill["outlet"];
}

/** The bill with its own UPI QR printed on it. The invoice number comes when the money lands. */
export function QrSlip({ slip, upi, expiresAt }: { slip: QrSlipData; upi: string; expiresAt: string }) {
  const o = slip.outlet;
  return (
    <div className="receipt">
      <div className="r-center">
        <strong className="r-brand">{o.brand}</strong>
        <div>{o.name}</div>
        {o.address && <div>{o.address}</div>}
        {o.gstin && <div>GSTIN: {o.gstin}</div>}
        <div className="r-title">BILL: SCAN TO PAY</div>
      </div>
      <div className="r-row">
        <span>Token #{slip.token_no}</span>
        <span>{slip.order_type === "dine_in" ? `Table ${slip.table_label}` : slip.car_number ? `Car ${slip.car_number}` : slip.order_type === "takeaway" ? "Takeaway" : "Delivery"}</span>
      </div>
      <div>{new Date().toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}</div>
      <hr />
      <table className="r-items">
        <thead><tr><th>Item</th><th>Qty</th><th>Amt</th></tr></thead>
        <tbody>
          {slip.lines.map((l, i) => (
            <tr key={i}><td>{l.item_name}{l.variant_name !== "Regular" ? ` (${l.variant_name})` : ""}{l.addons_text ? <div className="r-sub">+ {l.addons_text}</div> : null}</td><td>{l.qty}</td><td>{formatPaise(l.gross_paise)}</td></tr>
          ))}
        </tbody>
      </table>
      <hr />
      <div className="r-row"><span>Subtotal</span><span>{formatPaise(slip.gross_paise)}</span></div>
      {slip.discount_paise > 0 && <div className="r-row"><span>Discount</span><span>−{formatPaise(slip.discount_paise)}</span></div>}
      {slip.tax_summary.filter((t) => t.rate_bp > 0).map((t) => (
        <div key={t.rate_bp} className="r-row"><span>GST {pct(t.rate_bp)}</span><span>{formatPaise(t.cgst_paise + t.sgst_paise + t.igst_paise)}</span></div>
      ))}
      {slip.round_off_paise !== 0 && <div className="r-row"><span>Round off</span><span>{slip.round_off_paise > 0 ? "" : "−"}{formatPaise(Math.abs(slip.round_off_paise))}</span></div>}
      <div className="r-row r-total"><span>TO PAY</span><span>{formatPaise(slip.total_paise)}</span></div>
      <div className="r-center r-qr">
        <QrImage text={upi} size={200} label="UPI QR for this bill" />
        <div><strong>Scan with any UPI app</strong></div>
        <div className="r-sub">Amount is filled in. Valid till {new Date(expiresAt).toLocaleTimeString("en-IN", { timeStyle: "short" })}.</div>
        <div className="r-sub">Your tax invoice prints when the payment comes in.</div>
      </div>
    </div>
  );
}

/** Prints one QR slip and calls onDone afterwards. */
export function PrintQrSlip({ slip, upi, expiresAt, onDone }: { slip: QrSlipData; upi: string; expiresAt: string; onDone: () => void }) {
  const [show, setShow] = useState(false);
  const done = useRef(onDone);
  done.current = onDone;
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void withPrintLock(async () => {
      setShow(true);
      await paintDelay(450);            // let the QR image draw
      await printNow("bill");
    }).finally(() => done.current());
  }, []);
  return show ? createPortal(<div id="print-root"><QrSlip slip={slip} upi={upi} expiresAt={expiresAt} /></div>, document.body) : null;
}
