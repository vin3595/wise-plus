import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { formatPaise, inputToPaise } from "../money";
import { useSession } from "../session";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

interface Bal { supplier_id: string; name: string; opening_paise: number; bills_paise: number; paid_paise: number; balance_paise: number;
  last_bill_date: string | null; last_payment_date: string | null }
interface Entry { id: string; date: string; kind: "opening" | "bill" | "payment" | "debit_note" | "cash_drawer"; ref: string | null;
  debit_paise: number; credit_paise: number; balance_paise: number; mode?: string | null; note?: string | null }

const KIND: Record<Entry["kind"], string> = { opening: "Opening balance", bill: "Bill", payment: "Paid", debit_note: "Debit note", cash_drawer: "Cash from drawer" };
const today = () => new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 10);
const day = (d: string | null) => d ? new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "—";

/** Money > Supplier dues: what is owed to each supplier, their statement, and payments. */
export default function Dues() {
  const { can } = useSession();
  const [rows, setRows] = useState<Bal[] | null>(null);
  const [total, setTotal] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const load = useCallback(() => api<{ total_paise: number; suppliers: Bal[] }>("/ledger/suppliers?all=true")
    .then((r) => { setRows(r.suppliers); setTotal(r.total_paise); }), []);
  useEffect(() => { load(); }, [load]);
  if (!rows) return <p className="muted">Loading…</p>;
  const sel = rows.find((r) => r.supplier_id === open);
  return (
    <div className="stack">
      <h1>Supplier dues</h1>
      <div className="stats">
        <div className="stat"><span>You owe</span><strong>{formatPaise(total)}</strong></div>
        <div className="stat"><span>Suppliers with dues</span><strong>{rows.filter((r) => r.balance_paise > 0).length}</strong></div>
      </div>
      {sel ? <Statement bal={sel} canPay={can("inventory.manage")} onBack={() => setOpen(null)} onChange={load} /> : (
        <Card title="By supplier">
          {rows.length === 0 ? <p className="muted">No suppliers yet. Add them under Stock › Suppliers, or scan a bill.</p> : (
            <table className="table">
              <thead><tr><th>Supplier</th><th className="num">Due</th><th>Last bill</th><th>Last paid</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.supplier_id} className="clickable" onClick={() => setOpen(r.supplier_id)}>
                    <td><button className="link" onClick={() => setOpen(r.supplier_id)}>{r.name}</button></td>
                    <td className={`num ${r.balance_paise > 0 ? "" : "muted"}`}>{formatPaise(r.balance_paise)}</td>
                    <td>{day(r.last_bill_date)}</td><td>{day(r.last_payment_date)}</td>
                  </tr>))}
              </tbody>
            </table>)}
          <p className="muted small">Bills come from Stock › Purchases. Cash paid from a drawer counts when the cashier types the supplier's name as it is here.</p>
        </Card>)}
    </div>
  );
}

function Statement({ bal, canPay, onBack, onChange }: { bal: Bal; canPay: boolean; onBack: () => void; onChange: () => void }) {
  const [st, setSt] = useState<{ balance_paise: number; entries: Entry[] } | null>(null);
  const [form, setForm] = useState<null | "payment" | "opening" | "debit_note">(null);
  const { run, error } = useAction();
  const load = useCallback(() => api<{ balance_paise: number; entries: Entry[] }>(`/ledger/suppliers/${bal.supplier_id}`).then(setSt), [bal.supplier_id]);
  useEffect(() => { load(); }, [load]);
  const cancel = async (e: Entry) => {
    if (await run(() => api(`/ledger/entries/${e.id}/cancel`, { method: "POST" }))) { load(); onChange(); }
  };
  return (
    <Card title={bal.name} actions={<button className="link" onClick={onBack}>All suppliers</button>}>
      <p className="due-big">{formatPaise(st?.balance_paise ?? bal.balance_paise)} <span className="muted">due</span></p>
      {canPay && (
        <div className="row wrap">
          <Button onClick={() => setForm("payment")}>Record payment</Button>
          <Button variant="ghost" onClick={() => setForm("opening")}>Opening balance</Button>
          <Button variant="ghost" onClick={() => setForm("debit_note")}>Debit note</Button>
        </div>)}
      {form && <EntryForm kind={form} supplierId={bal.supplier_id} onDone={() => { setForm(null); load(); onChange(); }} onCancel={() => setForm(null)} />}
      <ErrorText error={error} />
      {!st ? <p className="muted">Loading…</p> : st.entries.length === 0 ? <p className="muted">Nothing yet.</p> : (
        <table className="table ledger">
          <thead><tr><th>Date</th><th>What</th><th className="num">Amount</th><th className="num">Balance</th></tr></thead>
          <tbody>
            {[...st.entries].reverse().map((e) => (
              <tr key={e.kind + e.id}>
                <td>{day(e.date)}</td>
                <td>{KIND[e.kind]}{e.mode ? ` · ${e.mode.toUpperCase()}` : ""}{e.ref ? ` · ${e.ref}` : ""}{e.note ? <div className="small muted">{e.note}</div> : null}
                  {canPay && ["payment", "debit_note", "opening"].includes(e.kind) && <div><button className="link small" onClick={() => cancel(e)}>Cancel this</button></div>}</td>
                <td className={`num ${e.credit_paise ? "paid" : ""}`}>{e.credit_paise ? `−${formatPaise(e.credit_paise)}` : `+${formatPaise(e.debit_paise)}`}</td>
                <td className="num">{formatPaise(e.balance_paise)}</td>
              </tr>))}
          </tbody>
        </table>)}
    </Card>
  );
}

function EntryForm({ kind, supplierId, onDone, onCancel }: { kind: "payment" | "opening" | "debit_note"; supplierId: string; onDone: () => void; onCancel: () => void }) {
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(today());
  const [mode, setMode] = useState("upi");
  const [ref, setRef] = useState("");
  const [note, setNote] = useState("");
  const { run, busy, error } = useAction();
  const paise = inputToPaise(amount.replace(/^-/, ""));
  const signed = paise === null ? null : (kind === "opening" && amount.trim().startsWith("-") ? -paise : paise);
  const save = async () => {
    if (signed === null || (kind !== "opening" && signed <= 0)) return;
    const r = await run(() => api("/ledger/entries", { body: { supplier_id: supplierId, kind, entry_date: date, amount_paise: signed,
      mode: kind === "payment" ? mode : null, reference: ref || null, note: note || null } }));
    if (r) onDone();
  };
  return (
    <div className="entry-form">
      <div className="grid-2">
        <Field label={kind === "opening" ? "Owed to them on that date (minus if they owe you)" : "Amount"}>
          <Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="0" autoFocus />
        </Field>
        <Field label="Date"><Input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></Field>
        {kind === "payment" && (
          <Field label="Paid by">
            <Select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="upi">UPI</option><option value="bank">Bank transfer</option><option value="cash">Cash</option>
              <option value="cheque">Cheque</option><option value="card">Card</option><option value="other">Other</option>
            </Select>
          </Field>)}
        {kind !== "opening" && <Field label={kind === "payment" ? "UTR / cheque no. (optional)" : "Debit note no. (optional)"}><Input value={ref} onChange={(e) => setRef(e.target.value)} /></Field>}
        <Field label="Note (optional)"><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </div>
      <ErrorText error={error} />
      <div className="row">
        <Button disabled={busy || signed === null || (kind !== "opening" && (signed ?? 0) <= 0)} onClick={save}>Save</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}
