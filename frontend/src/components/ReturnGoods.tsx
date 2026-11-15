import { useState } from "react";
import { api } from "../api";
import { formatPaise } from "../money";
import { Button, ErrorText, Input, useAction } from "../ui";

interface Line { stock_item_id?: string; name: string; qty_m: number; unit: string }

/** Goods sent back to the supplier: stock goes down and a debit note lowers what's owed to them. */
export default function ReturnGoods({ purchase, onClose, onDone }:
  { purchase: { id: string; invoice_no: string | null; supplier: string | null; lines: Line[] }; onClose: () => void; onDone: () => void }) {
  const [qty, setQty] = useState<Record<number, string>>({});
  const [reason, setReason] = useState("");
  const [done, setDone] = useState<{ total_paise: number } | null>(null);
  const { run, busy, error } = useAction();
  const lines = purchase.lines.map((l, i) => ({ ...l, i, back: Number(qty[i] || 0) })).filter((l) => l.stock_item_id);
  const picked = lines.filter((l) => l.back > 0);
  const save = async () => {
    const r = await run(() => api<{ total_paise: number }>(`/inventory/purchases/${purchase.id}/return`, { body: {
      reason: reason.trim(), lines: picked.map((l) => ({ stock_item_id: l.stock_item_id, qty_m: Math.round(l.back * 1000) })) } }));
    if (r) { setDone(r); onDone(); }
  };
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Return goods">
        <h2>Return goods{purchase.supplier ? ` to ${purchase.supplier}` : ""}</h2>
        {done ? (
          <>
            <p>Stock taken out, and a debit note of <strong>{formatPaise(done.total_paise)}</strong> lowers what you owe {purchase.supplier || "them"}.</p>
            <div className="row"><Button onClick={onClose}>Done</Button></div>
          </>
        ) : (
          <>
            <p className="muted small">Bill {purchase.invoice_no || "without a number"}. Enter how much goes back, in the unit it was bought in.</p>
            <table className="table">
              <thead><tr><th>Item</th><th className="num">Bought</th><th className="num">Back</th></tr></thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.i}>
                    <td>{l.name}</td><td className="num">{Number((l.qty_m / 1000).toFixed(3))} {l.unit}</td>
                    <td className="num"><Input inputMode="decimal" value={qty[l.i] || ""} style={{ width: "5rem" }} aria-label={`How much ${l.name} goes back`}
                      onChange={(e) => setQty((q) => ({ ...q, [l.i]: e.target.value.replace(/[^\d.]/g, "") }))} /></td>
                  </tr>))}
              </tbody>
            </table>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why? e.g. Expired, damaged" aria-label="Reason" />
            <ErrorText error={error} />
            <div className="row">
              <Button disabled={busy || !picked.length || reason.trim().length < 3} onClick={save}>Return and make debit note</Button>
              <Button variant="ghost" onClick={onClose}>Cancel</Button>
            </div>
          </>)}
      </div>
    </div>
  );
}
