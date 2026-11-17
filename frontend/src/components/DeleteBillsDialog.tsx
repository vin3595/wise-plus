import { useState } from "react";
import { api } from "../api";
import { Button, ErrorText, Field, Input } from "../ui";

const REASONS = ["Entered twice", "Wrong supplier", "Wrong amount", "Goods not received", "Test entry"];

/** Deleting supplier bills: always a reason and an owner's or manager's PIN (kept with each bill and in the audit log). */
export function DeleteBillsDialog({ ids, label, onClose, onDone }: { ids: string[]; label: string; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ok = reason.trim().length >= 3 && pin.length >= 4;
  const go = async () => {
    if (!ok) return;
    setBusy(true); setErr(null);
    try {
      const body = { reason: reason.trim(), manager_pin: pin };
      if (ids.length === 1) await api(`/inventory/purchases/${ids[0]}/cancel`, { body });
      else await api("/inventory/purchases/delete", { body: { ids, ...body } });
      onDone();
    } catch (e) { setErr(e instanceof Error ? e.message : "Couldn't delete"); setPin(""); }
    finally { setBusy(false); }
  };
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`Delete ${label}`}>
        <h3>Delete {label}?</h3>
        <p className="muted small">Their stock comes back out and they leave supplier dues and reports. They stay under “Deleted” with the reason.</p>
        <Field label="Why?">
          <div className="chips">{REASONS.map((r) => (
            <button key={r} type="button" className={`chip${reason === r ? " on" : ""}`} onClick={() => setReason(r)}>{r}</button>))}</div>
          <Input value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} placeholder="Or type the reason" aria-label="Reason" />
        </Field>
        <Field label="Owner or manager PIN">
          <Input type="password" inputMode="numeric" autoComplete="off" maxLength={8} value={pin}
            onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} onKeyDown={(e) => { if (e.key === "Enter") void go(); }} aria-label="Manager PIN" />
        </Field>
        <ErrorText error={err} />
        <div className="row end">
          <Button variant="ghost" onClick={onClose} disabled={busy}>Keep</Button>
          <Button variant="danger" onClick={go} disabled={!ok || busy}>{busy ? "Deleting…" : "Delete"}</Button>
        </div>
      </div>
    </div>
  );
}
