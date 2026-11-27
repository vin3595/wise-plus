/** KOTs as plain printer text (ESC/POS), the same slip as the print bridge: works on "Generic / Text Only"
 *  drivers and cuts right after the last line, so no paper is wasted. */
import type { Bill, PrintJob } from "./types";
import { ORDER_TYPE_LABEL } from "./hooks";

const ESC = 0x1b, GS = 0x1d;
const SUBS: Record<string, string> = { "₹": "Rs", "–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "×": "x", "…": "...", "·": "-" };
const clean = (t: string | null | undefined) => Array.from(t || "").map((c) => SUBS[c] ?? (c.charCodeAt(0) < 128 ? c : "?")).join("");

function wrap(text: string, width: number, indent = 0): string[] {
  const out: string[] = [];
  let cur = "";
  for (let w of clean(text).split(/\s+/).filter(Boolean)) {
    while (w.length > width - indent) {
      if (cur) { out.push(cur); cur = ""; }
      out.push(w.slice(0, width - indent));
      w = w.slice(width - indent);
    }
    if (!cur) cur = w;
    else if (cur.length + 1 + w.length <= width - indent) cur += " " + w;
    else { out.push(cur); cur = w; }
  }
  if (cur) out.push(cur);
  return (out.length ? out : [""]).map((l, i) => (i ? " ".repeat(indent) : "") + l);
}

type Size = 1 | 2 | 3 | "tall";
const SIZE: Record<string, number> = { 1: 0x00, 2: 0x11, 3: 0x22, tall: 0x01 };

export function kotBytes(job: PrintJob, paperMm = 80, copyNo = 1, copies = 1): Uint8Array {
  const k = job.kot, w = paperMm === 58 ? 32 : 48;
  const out: number[] = [ESC, 0x40];
  const line = (text = "", size: Size = 1, bold = false, center = false) => {
    out.push(ESC, 0x61, center ? 1 : 0, GS, 0x21, SIZE[String(size)], ESC, 0x45, bold ? 1 : 0);
    for (const ch of clean(text)) out.push(ch.charCodeAt(0));
    out.push(0x0a);
  };
  line(k.prep_area_name.toUpperCase(), 2, true, true);
  if (job.kind === "cancel") { line("*** CANCELLED ***", 2, true, true); line("Do not make this order", 1, true, true); }
  else if (job.kind === "reprint") line(`*** REPRINT ${job.printed_before + 1} ***`, 1, true, true);
  if (k.is_addon_kot && job.kind !== "cancel") line("ADDED ITEMS", 1, true, true);
  if (copies > 1) line(`Copy ${copyNo} of ${copies}`, 1, false, true);
  line(`TOKEN ${k.token_no}`, 3, true, true);
  const where = k.table_label ? `Table ${k.table_label}` : k.car_number ? `Car ${k.car_number}` : null;
  if (where) line(where, 2, true, true);
  line(ORDER_TYPE_LABEL[k.order_type] || k.order_type, 1, true, true);
  const at = new Date(k.created_at).toLocaleString("en-IN", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "numeric", minute: "2-digit" });
  line(`KOT ${k.kot_no}  ${at}`, 1, false, true);
  const who = [k.customer_name && `For ${k.customer_name}`, job.taken_by && `By ${job.taken_by}`].filter(Boolean).join("  ");
  if (who) line(who.slice(0, w), 1, false, true);
  line("-".repeat(w));
  let count = 0;
  for (const l of k.lines) {
    count += l.qty;
    const name = l.item_name + (l.variant_name && l.variant_name !== "Regular" ? ` (${l.variant_name})` : "");
    for (const part of wrap(`${l.qty} x ${name}`, w, 4)) line(part, job.kind !== "cancel" ? "tall" : 1, true);
    if (l.addons.length) for (const part of wrap("+ " + l.addons.map((a) => a.name).join(", "), w - 4, 2)) line("    " + part);
    if (l.notes) for (const part of wrap("Note: " + l.notes, w - 4, 2)) line("    " + part, 1, true);
  }
  line("-".repeat(w));
  if (k.order_notes) for (const part of wrap("ORDER NOTE: " + k.order_notes, w)) line(part, 1, true);
  line(`Items: ${count}`);
  out.push(0x0a, 0x0a, 0x0a, GS, 0x56, 0x42, 0x03);   // feed a little, then cut
  return new Uint8Array(out);
}

export function testBytes(paperMm = 80): Uint8Array {
  const w = paperMm === 58 ? 32 : 48;
  const s = `\x1b@\x1ba\x01\x1d!\x11\x1bE\x01EASE MY BILL\n\x1d!\x00\x1bE\x00KOT printer test\n${"-".repeat(w)}\n${new Date().toLocaleString("en-IN")}\nIf you can read this, KOTs print here.\n\n\n`;
  return new Uint8Array([...Array.from(s).map((c) => c.charCodeAt(0) & 0xff), GS, 0x56, 0x42, 0x03]);
}

// ---------------------------------------------------------------- bills as printer text (sharp, cut right after)

const rs = (p: number) => `Rs ${(p / 100).toFixed(2).replace(/\.00$/, "")}`;
const BILL_MODE: Record<string, string> = { cash: "Cash", upi: "UPI", card: "Card", due: "Credit (due)", complimentary: "Complimentary" };

/** The printer draws the QR itself (GS ( k), so it is sharp and always scans. */
function qrBytes(text: string, dot = 6): number[] {
  const data = Array.from(new TextEncoder().encode(text));
  const n = data.length + 3;
  return [
    GS, 0x28, 0x6b, 4, 0, 0x31, 0x41, 0x32, 0x00,          // model 2
    GS, 0x28, 0x6b, 3, 0, 0x31, 0x43, dot,                  // dot size
    GS, 0x28, 0x6b, 3, 0, 0x31, 0x45, 0x31,                 // error correction M
    GS, 0x28, 0x6b, n & 0xff, n >> 8, 0x31, 0x50, 0x30, ...data,
    GS, 0x28, 0x6b, 3, 0, 0x31, 0x51, 0x30,                 // print it
  ];
}

/** Logo as a black-and-white raster (GS v 0). Skipped if it can't be loaded. */
export async function logoBytes(url: string | null | undefined, paperMm = 80): Promise<number[]> {
  if (!url) return [];
  try {
    const img = new Image();
    img.crossOrigin = "anonymous";
    await new Promise<void>((ok, bad) => { img.onload = () => ok(); img.onerror = () => bad(); img.src = url; setTimeout(bad, 3000); });
    const maxW = paperMm === 58 ? 256 : 320, maxH = 140;
    const k = Math.min(maxW / img.width, maxH / img.height, 1);
    const w = Math.max(8, Math.round(img.width * k)), h = Math.max(1, Math.round(img.height * k));
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const g = c.getContext("2d")!; g.fillStyle = "#fff"; g.fillRect(0, 0, w, h); g.drawImage(img, 0, 0, w, h);
    const px = g.getImageData(0, 0, w, h).data, bw = Math.ceil(w / 8);
    const out = [GS, 0x76, 0x30, 0, bw & 0xff, bw >> 8, h & 0xff, h >> 8];
    for (let y = 0; y < h; y++) for (let xb = 0; xb < bw; xb++) {
      let b = 0;
      for (let bit = 0; bit < 8; bit++) {
        const x = xb * 8 + bit; if (x >= w) continue;
        const i = (y * w + x) * 4, lum = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
        if (px[i + 3] > 128 && lum < 140) b |= 0x80 >> bit;
      }
      out.push(b);
    }
    return [ESC, 0x61, 1, ...out, 0x0a];
  } catch { return []; }
}

export function billBytes(bill: Bill, payQr: string | null, paperMm = 80, logo: number[] = []): Uint8Array {
  const o = bill.outlet, w = paperMm === 58 ? 32 : 48;
  const out: number[] = [ESC, 0x40, ESC, 0x74, 0, ...logo];
  const text = (t: string) => { for (const ch of clean(t)) out.push(ch.charCodeAt(0)); };
  const line = (t = "", size: Size = 1, bold = false, center = false) => {
    out.push(ESC, 0x61, center ? 1 : 0, GS, 0x21, SIZE[String(size)], ESC, 0x45, bold ? 1 : 0); text(t); out.push(0x0a);
  };
  const lr = (l: string, r: string, bold = false, big = false) => {
    const width = big ? Math.floor(w / 2) : w;
    l = clean(l); r = clean(r);
    const room = width - r.length - 1;
    const parts = wrap(l, Math.max(8, room));
    parts.forEach((p, i) => line(i === parts.length - 1 ? p.padEnd(width - r.length) + r : p, big ? 2 : 1, bold));
  };
  const rule = () => line("-".repeat(w));
  line(o.brand, 2, true, true);
  if (o.legal_name && o.legal_name !== o.brand) for (const p of wrap(o.legal_name, w)) line(p, 1, false, true);
  if (o.name) line(o.name, 1, false, true);
  if (o.address) for (const p of wrap(o.address, w)) line(p, 1, false, true);
  if (o.phone) line(`Ph: ${o.phone}`, 1, false, true);
  if (o.gstin) line(`GSTIN: ${o.gstin}`, 1, false, true);
  if (o.fssai) line(`FSSAI: ${o.fssai}`, 1, false, true);
  line(bill.training ? "TRAINING - NOT A BILL" : bill.status === "cancelled" ? "CANCELLED TAX INVOICE" : "TAX INVOICE", 1, true, true);
  if ((bill.revision || 1) > 1) line(`Revised bill (version ${bill.revision})`, 1, false, true);
  line(`Bill no: ${bill.invoice_no}`, 1, true);
  line(`Date: ${new Date(bill.created_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}`);
  lr(`Token #${bill.token_no}`, bill.order_type === "dine_in" ? `Table ${bill.table_label}` : bill.car_number ? `Car ${bill.car_number}` : bill.order_type === "takeaway" ? "Takeaway" : "Delivery");
  if (bill.customer_name || bill.customer_phone) for (const p of wrap(`Customer: ${[bill.customer_name, bill.customer_phone].filter(Boolean).join(" - ")}`, w)) line(p);
  if (bill.customer_gstin) line(`Customer GSTIN: ${bill.customer_gstin}`);
  rule();
  const qw = 4, aw = 10, iw = w - qw - aw;
  line("Item".padEnd(iw) + "Qty".padStart(qw) + "Amt".padStart(aw), 1, true);
  for (const l of bill.lines) {
    const name = l.item_name + (l.variant_name !== "Regular" ? ` (${l.variant_name})` : "");
    const parts = wrap(name, iw - 1);
    parts.forEach((p, i) => line(i === 0 ? p.padEnd(iw) + String(l.qty).padStart(qw) + rs(l.gross_paise).padStart(aw) : p));
    if (l.addons_text) for (const p of wrap(`+ ${l.addons_text}`, iw - 1, 2)) line("  " + p);
    line(`  @ ${rs(l.unit_paise)}${l.hsn_sac ? ` - SAC ${l.hsn_sac}` : ""}`);
  }
  rule();
  lr("Subtotal", rs(bill.gross_paise));
  if (bill.discount_paise > 0) lr(`Discount${bill.discount_reason ? ` (${bill.discount_reason})` : ""}`, `-${rs(bill.discount_paise)}`);
  lr("Taxable value", rs(bill.taxable_paise));
  for (const t of bill.tax_summary.filter((x) => x.rate_bp > 0)) {
    if (t.igst_paise > 0) lr(`IGST ${t.rate_bp / 100}%`, rs(t.igst_paise));
    else { lr(`CGST ${t.rate_bp / 200}%`, rs(t.cgst_paise)); lr(`SGST ${t.rate_bp / 200}%`, rs(t.sgst_paise)); }
  }
  if (bill.round_off_paise !== 0) lr("Round off", `${bill.round_off_paise > 0 ? "" : "-"}${rs(Math.abs(bill.round_off_paise))}`);
  rule();
  lr("TOTAL", rs(bill.total_paise), true, true);
  lr("Paid by", bill.payments.map((p) => `${BILL_MODE[p.mode] || p.mode} ${rs(p.amount_paise)}`).join(", "));
  if (bill.status === "cancelled") line(`Cancelled: ${bill.cancel_reason || ""}`, 1, true, true);
  if (payQr && bill.status !== "cancelled") {
    rule();
    line(`Pay ${rs(bill.total_paise)} by UPI`, 1, true, true);
    out.push(ESC, 0x61, 1, ...qrBytes(payQr, paperMm === 58 ? 5 : 6), 0x0a);
    line("Scan with GPay, PhonePe, Paytm or any UPI app", 1, false, true);
  }
  rule();
  for (const p of wrap(o.footer || "Thank you! Visit again.", w)) line(p, 1, false, true);
  if (bill.cashier_name) line(`Billed by ${bill.cashier_name}`, 1, false, true);
  out.push(0x0a, 0x0a, 0x0a, GS, 0x56, 0x42, 0x03);
  return new Uint8Array(out);
}
