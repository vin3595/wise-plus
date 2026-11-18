import { useNavigate } from "react-router-dom";
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
export function ScanBill({ outletId, onDraft, onBatch }: { outletId: string; onDraft: (d: ScanDraft) => void; onBatch?: () => void }) {
  const [on, setOn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  useEffect(() => { api<{ enabled: boolean }>("/inventory/purchases/scan/status").then((r) => setOn(r.enabled)).catch(() => setOn(false)); }, []);
  if (!on) return null;
  const send = async (file: File) => {
    setBusy(true); setError(null);
    const fd = new FormData();
    fd.append("outlet_id", outletId);
    fd.append("file", file);
    try {
      const r = await api<ScanDraft & { kind?: string }>("/inventory/purchases/scan", { form: fd });
      if (r.kind === "statement") {
        // A supplier's statement, not a bill: show the check on Supplier dues.
        try { sessionStorage.setItem("wise-statement", JSON.stringify(r)); } catch { /* storage unavailable */ }
        buzz(20);
        navigate("/dues");
        return;
      }
      onDraft(r); buzz(20);
    }
    catch (e) { setError(e instanceof Error ? e.message : "Couldn't read that bill"); }
    finally { setBusy(false); }
  };
  // Several bills picked at once: each is read and put in "Bills to check", to confirm one by one.
  const sendMany = async (files: File[]) => {
    setBusy(true); setError(null);
    let ok = 0; const failed: string[] = [];
    for (let i = 0; i < files.length; i++) {
      setProgress(`Reading bill ${i + 1} of ${files.length}…`);
      const fd = new FormData();
      fd.append("outlet_id", outletId); fd.append("file", files[i]); fd.append("batch", "true");
      try { await api("/inventory/purchases/scan", { form: fd }); ok++; }
      catch (e) { failed.push(`${files[i].name}: ${e instanceof Error ? e.message : "couldn't read it"}`); }
    }
    setBusy(false); setProgress(null); buzz(20);
    onBatch?.();
    setError(failed.length ? `${ok} of ${files.length} read. Not read — ${failed.join("; ")}` : null);
  };
  return (
    <div className="scan-bill">
      <input ref={input} type="file" accept="image/*,application/pdf" hidden multiple={!!onBatch}
        onChange={(e) => { const fs = Array.from(e.target.files || []); e.target.value = "";
          if (fs.length > 1 && onBatch) sendMany(fs.slice(0, 30)); else if (fs[0]) send(fs[0]); }} />
      <button className="btn btn-primary" disabled={busy} onClick={() => input.current?.click()}>
        <ScanLine size={17} aria-hidden />{busy ? (progress || "Reading the bill…") : onBatch ? "Scan bills" : "Scan bill"}
      </button>
      {busy && <span className="scan-busy" role="status">{progress ? "Keep this screen open" : "Usually 5–15 seconds"}</span>}
      {!busy && onBatch && <span className="small muted">Pick several to scan them together</span>}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
