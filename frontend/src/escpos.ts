/** KOTs as plain printer text (ESC/POS), the same slip as the print bridge: works on "Generic / Text Only"
 *  drivers and cuts right after the last line, so no paper is wasted. */
import type { PrintJob } from "./types";
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
