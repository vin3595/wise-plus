import { useEffect, useRef, useState } from "react";
import { ScanLine } from "lucide-react";
import { api } from "../api";
import { buzz } from "../haptics";

/** What POST /inventory/purchases/scan returns: a purchase to check, not yet saved. */
export interface ScanDraft {
  scan_id?: string;          // the kept original (bill keeper), linked to the purchase once saved
  vendor: string | null; gstin: string | null;
  supplier: { id: string; name: string; how: "gstin" | "name" } | null;
  invoice_no: string | null; invoice_date: string | null;
  total_paise: number; taxable_paise: number | null; tax_paise: number | null;
  lines: { bill_name: string; qty_m: number; unit_price_paise: number; amount_paise: number; gst_bp: number; hsn: string | null;
    stock_item_id: string | null; stock_item_name: string | null; unit: string | null; match: number; check: string | null }[];
  adjusted: { from_paise: number; to_paise: number; why: string } | null;
  needs_review: boolean; review_reasons: string[]; low_confidence: boolean;
  duplicate?: { id: string; invoice_date: string; total_paise: number };
  thumbnail: string | null;
}

/** "Scan bill": photograph a supplier's bill (or pick a PDF) and the purchase form fills itself in.
 *  Hidden when the server has no bill reader set up. */
export function ScanBill({ outletId, onDraft }: { outletId: string; onDraft: (d: ScanDraft) => void }) {
  const [on, setOn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { api<{ enabled: boolean }>("/inventory/purchases/scan/status").then((r) => setOn(r.enabled)).catch(() => setOn(false)); }, []);
  if (!on) return null;
  const send = async (file: File) => {
    setBusy(true); setError(null);
    const fd = new FormData();
    fd.append("outlet_id", outletId);
    fd.append("file", file);
    try { onDraft(await api<ScanDraft>("/inventory/purchases/scan", { form: fd })); buzz(20); }
    catch (e) { setError(e instanceof Error ? e.message : "Couldn't read that bill"); }
    finally { setBusy(false); }
  };
  return (
    <div className="scan-bill">
      <input ref={input} type="file" accept="image/*,application/pdf" hidden
        onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) send(f); }} />
      <button className="btn btn-primary" disabled={busy} onClick={() => input.current?.click()}>
        <ScanLine size={17} aria-hidden />{busy ? "Reading the bill…" : "Scan bill"}
      </button>
      {busy && <span className="scan-busy" role="status">Usually 5–15 seconds</span>}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
