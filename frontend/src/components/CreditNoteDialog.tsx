import { useEffect, useState } from "react";
import { api } from "../api";
import { formatPaise } from "../money";
import { Button, ErrorText, Input, Select, useAction } from "../ui";

interface Line { id: string; item_name: string; variant_name: string; qty: number; left: number; unit_total_paise: number }
interface Note { id: string; number: string; total_paise: number; refund_mode: string; status: string; reason: string; date: string }

/** Give money back on a bill without editing it: a GST credit note for the items returned. */
export default function CreditNoteDialog({ billId, invoiceNo, isManager, onClose, onDone }:
  { billId: string; invoiceNo: string; isManager: boolean; onClose: () => void; onDone: () => void }) {
  const [lines, setLines] = useState<Line[] | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [qty, setQty] = useState<Record<string, number>>({});
  const [reason, setReason] = useState("");
  const [mode, setMode] = useState("cash");
  const [pin, setPin] = useState("");
  const [made, setMade] = useState<Note | null>(null);
  const { run, busy, error } = useAction();
  useEffect(() => {
    api<{ lines: Line[]; notes: Note[] }>(`/bills/${billId}/returnable`).then((r) => { setLines(r.lines); setNotes(r.notes); });
  }, [billId]);
  const picked = (lines || []).filter((l) => (qty[l.id] || 0) > 0);
  const estimate = picked.reduce((s, l) => s + l.unit_total_paise * (qty[l.id] || 0), 0);
  const save = async () => {
    const r = await run(() => api<Note>(`/bills/${billId}/credit-notes`, { body: {
      lines: picked.map((l) => ({ bill_line_id: l.id, qty: qty[l.id] })), reason: reason.trim(), refund_mode: mode, manager_pin: isManager ? null : pin } }));
    if (r) { setMade(r); onDone(); }
  };
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`Credit note on ${invoiceNo}`}>
        <h2>Give back on {invoiceNo}</h2>
        {made ? (
          <>
            <p>Credit note <strong>{made.number}</strong> for <strong>{formatPaise(made.total_paise)}</strong> ({made.refund_mode === "none" ? "nothing paid back" : `paid back by ${made.refund_mode.toUpperCase()}`}).</p>
            <p className="muted small">The bill stays as it was; reports, GST and Tally show the credit note.</p>
            <div className="row"><Button onClick={onClose}>Done</Button></div>
          </>
        ) : !lines ? <p className="muted">Loading…</p> : (
          <>
            <table className="table">
              <thead><tr><th>Item</th><th className="num">Sold</th><th className="num">Give back</th></tr></thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.id} className={l.left ? "" : "inactive"}>
                    <td>{l.item_name} <span className="muted small">{l.variant_name}</span></td>
                    <td className="num">{l.qty}{l.left < l.qty ? <span className="muted small"> ({l.qty - l.left} already)</span> : null}</td>
                    <td className="num">
                      <Input type="number" min={0} max={l.left} disabled={!l.left} value={qty[l.id] || ""} aria-label={`How many ${l.item_name}`}
                        onChange={(e) => setQty((q) => ({ ...q, [l.id]: Math.max(0, Math.min(l.left, Number(e.target.value) || 0)) }))} style={{ width: "4.5rem" }} />
                    </td>
                  </tr>))}
              </tbody>
            </table>
            <div className="grid-2">
              <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why? e.g. Shake spilt" aria-label="Reason" />
              <Select value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Paid back by">
                <option value="cash">Cash from the drawer</option><option value="upi">UPI</option><option value="card">Card</option>
                <option value="bank">Bank transfer</option><option value="none">Not paid back (credit only)</option>
              </Select>
              {!isManager && <Input type="password" inputMode="numeric" value={pin} onChange={(e) => setPin(e.target.value)} placeholder="Manager PIN" aria-label="Manager PIN" />}
            </div>
            {picked.length > 0 && <p>About <strong>{formatPaise(estimate)}</strong> back, with the GST on it.</p>}
            <ErrorText error={error} />
            <div className="row">
              <Button disabled={busy || !picked.length || reason.trim().length < 3 || (!isManager && pin.length < 4)} onClick={save}>Make credit note</Button>
              <Button variant="ghost" onClick={onClose}>Cancel</Button>
            </div>
          </>)}
        {notes.length > 0 && !made && (
          <p className="muted small">Earlier: {notes.map((n) => `${n.number} ${formatPaise(n.total_paise)}${n.status === "cancelled" ? " (cancelled)" : ""}`).join(", ")}</p>)}
      </div>
    </div>
  );
}
