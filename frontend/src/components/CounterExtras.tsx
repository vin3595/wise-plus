import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { formatPaise } from "../money";
import type { Bill } from "../types";
import BillCorrections from "./BillCorrections";

/** A cart put on hold: the customer stepped aside, the next one is served, then it comes back. */
export interface Held<L> { id: string; at: number; lines: L[]; total: number; label: string; state: Record<string, unknown> }

const key = (outletId: string) => `wise.held.${outletId}`;
export function loadHeld<L>(outletId: string): Held<L>[] {
  try { return JSON.parse(localStorage.getItem(key(outletId)) || "[]") as Held<L>[]; } catch { return []; }
}
export function saveHeld<L>(outletId: string, held: Held<L>[]) {
  try { localStorage.setItem(key(outletId), JSON.stringify(held.slice(0, 20))); } catch { /* storage full or blocked: held carts live until reload */ }
}

const ago = (at: number) => {
  const m = Math.round((Date.now() - at) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.floor(m / 60)} h ${m % 60} min ago`;
};
const clock = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });

export function HeldList<L>({ held, onResume, onDrop, onClose }: {
  held: Held<L>[]; onResume: (h: Held<L>) => void; onDrop: (h: Held<L>) => void; onClose: () => void;
}) {
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal held-panel" role="dialog" aria-label="Orders on hold" onClick={(e) => e.stopPropagation()}>
        <div className="held-head"><h3>On hold</h3><button className="choice dialog-close" onClick={onClose}>Close</button></div>
        {held.length === 0 ? <p className="muted">Nothing on hold.</p> : (
          <ul className="held-list">
            {held.map((h) => (
              <li key={h.id}>
                <button className="held-main" onClick={() => onResume(h)}>
                  <strong>{h.label}</strong>
                  <span className="muted small">{h.lines.length} item{h.lines.length === 1 ? "" : "s"} · {ago(h.at)}</span>
                </button>
                <strong className="held-amt">{formatPaise(h.total)}</strong>
                <button className="link small" onClick={() => onDrop(h)} aria-label={`Remove ${h.label} from hold`}>Remove</button>
              </li>
            ))}
          </ul>
        )}
        <p className="muted small">Held orders stay on this device. Tap one to bring it back.</p>
      </div>
    </div>
  );
}

const MODE: Record<string, string> = { cash: "Cash", upi: "UPI", card: "Card", due: "Due", complimentary: "Free", online: "Online", part: "Part payment" };

/** Today's last bills at this outlet: see how each was paid and print it again. */
export function RecentBills({ outletId, q = "", isManager, onPrint, onClose }: {
  outletId: string; q?: string; isManager: boolean; onPrint: (b: Bill) => void; onClose: () => void;
}) {
  const [bills, setBills] = useState<Bill[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Bill | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    api<Bill[]>(`/bills?${new URLSearchParams({ outlet_id: outletId, ...(q ? { q } : {}) })}`).then((b) => setBills(b.slice(0, 25)))
      .catch((e) => setError(e instanceof Error ? e.message : "Couldn't load bills"));
  }, [outletId, q, n]);
  if (editing) return <BillCorrections bills={[editing]} outletId={outletId} isManager={isManager}
    onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setN((x) => x + 1); }} />;
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal held-panel" role="dialog" aria-label="Recent bills" onClick={(e) => e.stopPropagation()}>
        <div className="held-head"><h3>{q ? `Bills matching "${q}"` : "Recent bills"}</h3><button className="choice dialog-close" onClick={onClose}>Close</button></div>
        {error && <p className="error">{error}</p>}
        {!bills && !error && <p className="muted">Loading…</p>}
        {bills?.length === 0 && <p className="muted">{q ? "No bill with that number or mobile." : "No bills yet today."}</p>}
        {bills && bills.length > 0 && (
          <ul className="held-list">
            {bills.map((b) => (
              <li key={b.id} className={b.status === "cancelled" ? "cancelled" : ""}>
                <div className="held-main">
                  <strong>{b.invoice_no}{b.token_no ? ` · #${b.token_no}` : ""}{b.status === "cancelled" ? " · Cancelled" : ""}</strong>
                  <span className="muted small">{clock(b.created_at)} · {b.payments.map((p) => MODE[p.mode] || p.mode).join(" + ") || "Paid online"}
                    {b.customer_name ? ` · ${b.customer_name}` : ""}</span>
                </div>
                <strong className="held-amt">{formatPaise(b.total_paise)}</strong>
                {b.status === "paid" && !b.locked && <button className="choice" onClick={() => setEditing(b)}>Edit</button>}
                <button className="choice" onClick={() => onPrint(b)}>Print</button>
              </li>
            ))}
          </ul>
        )}
        <p className="muted small">Edit changes the payment (cash, UPI, card) or the items. <Link to="/bills" onClick={onClose}>All bills</Link> to search older ones or cancel.</p>
      </div>
    </div>
  );
}
