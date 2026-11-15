import { useRef, useState } from "react";
import { CheckCircle2, AlertTriangle, CircleX, FileSearch } from "lucide-react";
import { api } from "../api";
import { formatPaise } from "../money";
import { Button, Card, ErrorText, Select, useAction } from "../ui";

/** Money › Supplier dues › Check a statement: the supplier's ledger against the POS and the dues ledger. */

interface Row { date: string; type: string; ref: string | null; amount_paise: number; particulars: string | null; bank: string | null }
interface Bill extends Row { in_pos: boolean; pos_invoice_no?: string | null; pos_date?: string; pos_total_paise?: number; matched_by?: string; differs_paise?: number }
interface Pay extends Row { recorded: boolean; kind: "payment" | "debit_note"; mode: string | null }
export interface StatementResult {
  kind?: "statement";
  statement: { supplier: string | null; period_from: string | null; period_to: string | null; adds_up: boolean };
  supplier: { id: string; name: string } | null;
  supplier_added?: boolean;
  choices?: { id: string; name: string }[];
  bills: Bill[]; bills_total: number; bills_in_pos: number; missing_paise: number;
  only_in_pos: { purchase_id: string; invoice_no: string | null; date: string; total_paise: number }[];
  payments: Pay[]; payments_new: number;
  opening: { theirs: number | null; ours: number | null; suggest: number | null };
  closing: { theirs: number | null; ours: number | null };
}

const day = (d: string | null) => d ? new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "—";
const prevDay = (d: string) => new Date(new Date(d + "T00:00:00Z").getTime() - 864e5).toISOString().slice(0, 10);

export function StatementUpload({ onResult }: { onResult: (r: StatementResult, file: File) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = async (file: File) => {
    setBusy(true); setError(null);
    const fd = new FormData();
    fd.append("file", file);
    try { onResult(await api<StatementResult>("/ledger/statement", { form: fd }), file); }
    catch (e) { setError(e instanceof Error ? e.message : "Couldn't read that statement"); }
    finally { setBusy(false); }
  };
  return (
    <div className="scan-bill">
      <input ref={input} type="file" accept="image/*,application/pdf" hidden
        onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) send(f); }} />
      <button className="btn btn-primary" disabled={busy} onClick={() => input.current?.click()}>
        <FileSearch size={17} aria-hidden />{busy ? "Reading the statement…" : "Check a statement"}
      </button>
      {busy && <span className="scan-busy" role="status">Usually 10–20 seconds</span>}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}

export function StatementView({ res, file, onBack, onChange }: { res: StatementResult; file: File | null; onBack: () => void; onChange: (r: StatementResult) => void }) {
  const { run, busy, error } = useAction();
  const st = res.statement;
  const period = st.period_from && st.period_to ? `${day(st.period_from)} – ${day(st.period_to)}` : "";

  if (!res.supplier) return <PickSupplier res={res} file={file} onBack={onBack} onChange={onChange} />;

  const missing = res.bills.filter((b) => !b.in_pos);
  const close = res.closing;
  const gap = close.theirs !== null && close.ours !== null ? close.theirs - close.ours : 0;
  return (
    <Card title={`${res.supplier.name}${period ? ` · ${period}` : ""}`} actions={<button className="link" onClick={onBack}>Back</button>}>
      {res.supplier_added && <p className="muted small">New supplier, added to Wise.</p>}
      {!st.adds_up && <p className="warn" role="alert"><AlertTriangle size={15} aria-hidden /> Their totals didn't add up when read. Check the amounts against the paper.</p>}
      <div className="stats">
        <div className="stat"><span>Bills saved in the POS</span><strong>{res.bills_in_pos} of {res.bills_total}</strong></div>
        <div className="stat"><span>Missing bills</span><strong>{formatPaise(res.missing_paise)}</strong></div>
        <div className="stat"><span>They say you owe</span><strong>{close.theirs === null ? "—" : formatPaise(close.theirs)}</strong></div>
        <div className="stat"><span>Wise says</span><strong>{close.ours === null ? "—" : formatPaise(close.ours)}</strong></div>
      </div>
      {Math.abs(gap) > 100 && <p className="muted">Difference {formatPaise(gap)}{missing.length ? `: ${missing.length} bill(s) are not in the POS yet.` : "."}</p>}

      <h3>Their bills</h3>
      <table className="table">
        <thead><tr><th>Date</th><th>Bill no.</th><th className="num">Amount</th><th>In the POS</th></tr></thead>
        <tbody>
          {res.bills.map((b, i) => (
            <tr key={i}>
              <td>{day(b.date)}</td><td>{b.ref || "—"}</td><td className="num">{formatPaise(b.amount_paise)}</td>
              <td>{!b.in_pos ? <span className="bad"><CircleX size={15} aria-hidden /> Missing</span>
                : b.differs_paise ? <span className="warn"><AlertTriangle size={15} aria-hidden /> Saved as {formatPaise(b.pos_total_paise || 0)}</span>
                : <span className="ok"><CheckCircle2 size={15} aria-hidden /> Saved{b.matched_by === "amount" ? ` (as ${b.pos_invoice_no || "no number"}, ${day(b.pos_date || null)})` : ""}</span>}</td>
            </tr>))}
        </tbody>
      </table>
      {missing.length > 0 && <p className="muted small">Add the missing ones under Stock › Purchases (scan the bill or type it in), then check the statement again.</p>}
      {res.only_in_pos.length > 0 && (
        <p className="muted small">In the POS but not on their statement: {res.only_in_pos.map((x) => `${x.invoice_no || day(x.date)} ${formatPaise(x.total_paise)}`).join(", ")}.
          It may be a bill from another supplier, or one they haven't entered yet.</p>)}

      <Payments res={res} busy={busy} onAdd={async (body) => {
        const r = await run(() => api<{ added: number }>("/ledger/statement/apply", { body }));
        if (r) onChange({ ...res, payments: res.payments.map((p) => ({ ...p, recorded: true })), payments_new: 0,
                         opening: { ...res.opening, suggest: null } });
      }} />
      <ErrorText error={error} />
    </Card>
  );
}

function Payments({ res, busy, onAdd }: { res: StatementResult; busy: boolean; onAdd: (body: unknown) => void }) {
  const [pick, setPick] = useState<Set<number>>(() => new Set(res.payments.map((p, i) => (p.recorded ? -1 : i)).filter((i) => i >= 0)));
  const [withOpening, setWithOpening] = useState(res.opening.suggest !== null);
  const pays = res.payments;
  if (!pays.length && res.opening.suggest === null) return null;
  const n = pick.size + (withOpening && res.opening.suggest !== null ? 1 : 0);
  const toggle = (i: number) => setPick((s) => { const t = new Set(s); if (t.has(i)) t.delete(i); else t.add(i); return t; });
  return (
    <>
      <h3>Payments on their statement</h3>
      <ul className="check-list">
        {res.opening.suggest !== null && res.statement.period_from && (
          <li><label><input type="checkbox" checked={withOpening} onChange={(e) => setWithOpening(e.target.checked)} />
            Opening balance {formatPaise(res.opening.suggest)} on {day(prevDay(res.statement.period_from))} <span className="muted">(nothing from before is in Wise)</span></label></li>)}
        {pays.map((p, i) => (
          <li key={i}>
            {p.recorded ? <span className="ok"><CheckCircle2 size={15} aria-hidden /> {day(p.date)} {formatPaise(p.amount_paise)} {p.bank || p.particulars || ""} · already in supplier dues</span>
              : <label><input type="checkbox" checked={pick.has(i)} onChange={() => toggle(i)} />
                  {day(p.date)} {formatPaise(p.amount_paise)} · {p.kind === "debit_note" ? "credit note" : (p.mode || "bank").toUpperCase()} {p.bank || p.particulars || ""}{p.ref ? ` · ${p.ref}` : ""}</label>}
          </li>))}
      </ul>
      {n > 0 && (
        <Button disabled={busy} onClick={() => onAdd({
          supplier_id: res.supplier!.id,
          opening: withOpening && res.opening.suggest !== null && res.statement.period_from ? { date: prevDay(res.statement.period_from), amount_paise: res.opening.suggest } : null,
          payments: pays.filter((_, i) => pick.has(i)).map((p) => ({ date: p.date, amount_paise: p.amount_paise, kind: p.kind, mode: p.mode || "bank",
            ref: p.ref, bank: p.bank, particulars: p.particulars })),
        })}>Add {n} to supplier dues</Button>)}
    </>
  );
}

function PickSupplier({ res, file, onBack, onChange }: { res: StatementResult; file: File | null; onBack: () => void; onChange: (r: StatementResult) => void }) {
  const [sel, setSel] = useState("");
  const { run, busy, error } = useAction();
  const go = async (form: Record<string, string>) => {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    Object.entries(form).forEach(([k, v]) => fd.append(k, v));
    const r = await run(() => api<StatementResult>("/ledger/statement", { form: fd }));
    if (r) onChange(r);
  };
  return (
    <Card title="Which supplier is this?" actions={<button className="link" onClick={onBack}>Back</button>}>
      <p>The statement is from <strong>{res.statement.supplier || "a supplier we couldn't name"}</strong>. It doesn't match a supplier in Wise.</p>
      {!file && <p className="muted">Upload it again from Money › Supplier dues to choose.</p>}
      {file && (
        <div className="row wrap">
          {res.statement.supplier && <Button disabled={busy} onClick={() => go({ new_supplier: "true" })}>Add “{res.statement.supplier}” as a new supplier</Button>}
          {!!res.choices?.length && (
            <>
              <Select value={sel} onChange={(e) => setSel(e.target.value)} aria-label="Or choose an existing supplier">
                <option value="">Or choose one you have…</option>
                {res.choices.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </Select>
              <Button variant="ghost" disabled={busy || !sel} onClick={() => go({ supplier_id: sel })}>Use this supplier</Button>
            </>)}
        </div>)}
      <ErrorText error={error} />
    </Card>
  );
}
