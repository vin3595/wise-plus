import { useCallback, useEffect, useRef, useState } from "react";
import { Landmark, Upload, CheckCircle2, AlertTriangle } from "lucide-react";
import { api } from "../api";
import { formatPaise, inputToPaise } from "../money";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

interface Account { id: string; name: string; bank: string | null; last4: string | null; outlet_ids: string[] | null; opening_paise: number;
  opening_date: string | null; tally_ledger: string | null; balance_paise: number | null; to_check: number; last_line: string | null }
interface Txn { id: string; date: string; narration: string; ref: string | null; amount_paise: number; status: string; kind: string | null;
  kind_label: string | null; category: string | null; supplier_id: string | null; note: string | null; matched_by: string | null; account_id: string }
interface Summary { account: Account; account_added: boolean; period_from: string; period_to: string; lines_new: number; lines_read: number;
  money_in_paise: number; money_out_paise: number; matched: number; to_check: number;
  groups: { label: string; count: number; in_paise: number; out_paise: number }[];
  missing: { date: string; mode: string; sales_paise: number; short_paise: number }[]; closing_paise: number | null; closing_matches: boolean | null }

const day = (d: string | null) => d ? new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "—";
const OUT = [["supplier", "Supplier"], ["expense", "Expense"], ["drawing", "Owner's drawing"], ["salary", "Salary"], ["rent", "Rent"],
  ["cash_withdrawal", "Cash withdrawn"], ["transfer", "To own account"], ["tax", "Tax / GST"], ["loan", "Loan / EMI"], ["charges", "Bank charges"], ["other_out", "Other"]];
const IN = [["sales", "UPI / card sales"], ["aggregator", "Zomato / Swiggy"], ["cash_deposit", "Cash deposited"], ["transfer", "From own account"],
  ["capital", "Owner's money"], ["refund", "Refund"], ["other_in", "Other"]];

/** Money › Banks: balances, statement uploads, and the lines the app couldn't sort. */
export default function Banks() {
  const [accts, setAccts] = useState<Account[] | null>(null);
  const [total, setTotal] = useState(0);
  const [sel, setSel] = useState<string | null>(null);
  const [last, setLast] = useState<Summary | null>(null);
  const [adding, setAdding] = useState(false);
  const load = useCallback(() => api<{ accounts: Account[]; total_paise: number }>("/banks").then((r) => { setAccts(r.accounts); setTotal(r.total_paise); }), []);
  useEffect(() => { load(); }, [load]);
  if (!accts) return <p className="muted">Loading…</p>;
  const toCheck = accts.reduce((n, a) => n + a.to_check, 0);
  const acct = accts.find((a) => a.id === sel) || null;
  return (
    <div className="stack">
      <h1>Banks</h1>
      <div className="stats">
        <div className="stat"><span>In the bank</span><strong>{formatPaise(total)}</strong></div>
        <div className="stat"><span>To check</span><strong>{toCheck}</strong></div>
      </div>
      <div className="row wrap">
        <UploadStatement accountId={sel} onDone={(s) => { setLast(s); setSel(s.account.id); load(); }} />
        <Button variant="ghost" onClick={() => setAdding((x) => !x)}>Add account</Button>
      </div>
      {adding && <AccountForm onDone={() => { setAdding(false); load(); }} onCancel={() => setAdding(false)} />}
      {last && <SummaryCard s={last} onClose={() => setLast(null)} />}
      {accts.length === 0 ? (
        <Card title="No bank accounts yet"><p className="muted">Upload a statement from net banking (PDF, Excel or CSV): the account is added from it.
          You can also send the statement on WhatsApp.</p></Card>
      ) : (
        <div className="bank-cards">
          {accts.map((a) => (
            <button key={a.id} className={`bank-card ${sel === a.id ? "on" : ""}`} onClick={() => setSel(sel === a.id ? null : a.id)}>
              <Landmark size={18} aria-hidden />
              <span className="bank-name">{a.name}</span>
              <strong>{a.balance_paise === null ? "—" : formatPaise(a.balance_paise)}</strong>
              <span className="small muted">{a.last_line ? `till ${day(a.last_line)}` : "no statement yet"}{a.to_check ? ` · ${a.to_check} to check` : ""}</span>
            </button>))}
        </div>)}
      <Lines account={acct} accounts={accts} onChange={load} />
      {acct && <AccountForm acct={acct} onDone={load} />}
    </div>
  );
}

function UploadStatement({ accountId, onDone }: { accountId: string | null; onDone: (s: Summary) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = async (f: File) => {
    setBusy(true); setError(null);
    const fd = new FormData();
    fd.append("file", f);
    if (accountId) fd.append("account_id", accountId);
    try { onDone(await api<Summary>("/banks/statements", { form: fd })); }
    catch (e) { setError(e instanceof Error ? e.message : "Couldn't read that statement"); }
    finally { setBusy(false); }
  };
  return (
    <div className="scan-bill">
      <input ref={input} type="file" hidden accept=".pdf,.xlsx,.xls,.csv,.txt,image/*" onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) send(f); }} />
      <button className="btn btn-primary" disabled={busy} onClick={() => input.current?.click()}><Upload size={17} aria-hidden />{busy ? "Reading…" : "Upload bank statement"}</button>
      {busy && <span className="scan-busy" role="status">PDFs take 10–30 seconds; Excel is instant</span>}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}

function SummaryCard({ s, onClose }: { s: Summary; onClose: () => void }) {
  return (
    <Card title={`${s.account.name} · ${day(s.period_from)} – ${day(s.period_to)}`} actions={<button className="link" onClick={onClose}>Close</button>}>
      {s.account_added && <p className="muted small">New account, added from the statement.</p>}
      {s.lines_new === 0 ? <p>Every line of this statement was already here.</p> : (
        <>
          <p><CheckCircle2 size={16} className="ok-ico" aria-hidden /> Sorted <strong>{s.matched} of {s.lines_new}</strong> lines{s.to_check ? ` · ${s.to_check} to check below` : ""}.
            {s.lines_read > s.lines_new ? <span className="muted"> {s.lines_read - s.lines_new} were already here.</span> : null}</p>
          <ul className="plain">
            {s.groups.map((g) => <li key={g.label}>{g.label}: <strong>{formatPaise(g.in_paise || g.out_paise)}</strong> <span className="muted">({g.count})</span></li>)}
          </ul>
        </>)}
      {s.missing.map((m) => (
        <p key={m.date + m.mode} className="warn-text"><AlertTriangle size={15} aria-hidden /> {m.mode.toUpperCase()} sales of {day(m.date)} ({formatPaise(m.sales_paise)}): {formatPaise(m.short_paise)} hasn't reached this account.</p>))}
      {s.closing_matches === false && <p className="warn-text">The balance doesn't match the statement's closing balance ({formatPaise(s.closing_paise || 0)}). A page may be missing.</p>}
    </Card>
  );
}

function Lines({ account, accounts, onChange }: { account: Account | null; accounts: Account[]; onChange: () => void }) {
  const [show, setShow] = useState<"check" | "all">("check");
  const [rows, setRows] = useState<Txn[] | null>(null);
  const [sups, setSups] = useState<{ id: string; name: string }[]>([]);
  const [cats, setCats] = useState<string[]>([]);
  const load = useCallback(() => api<Txn[]>(`/banks/txns?show=${show}${account ? `&account_id=${account.id}` : ""}`).then(setRows), [show, account]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    api<{ id: string; name: string }[]>("/inventory/suppliers").then(setSups).catch(() => setSups([]));
    api<string[]>("/banks/categories").then(setCats).catch(() => setCats([]));
  }, []);
  if (!accounts.length) return null;
  return (
    <Card title={account ? account.name : "All accounts"} actions={
      <div className="seg" role="tablist">
        <button role="tab" aria-selected={show === "check"} className={show === "check" ? "on" : ""} onClick={() => setShow("check")}>To check</button>
        <button role="tab" aria-selected={show === "all"} className={show === "all" ? "on" : ""} onClick={() => setShow("all")}>All lines</button>
      </div>}>
      {!rows ? <p className="muted">Loading…</p> : rows.length === 0 ? (
        <p className="muted">{show === "check" ? "Nothing to check. 🎉" : "No lines yet."}</p>
      ) : (
        <ul className="bank-lines">
          {rows.map((t) => <Line key={t.id} t={t} sups={sups} cats={cats} accounts={accounts} onDone={() => { load(); onChange(); }} />)}
        </ul>)}
    </Card>
  );
}

function Line({ t, sups, cats, accounts, onDone }: { t: Txn; sups: { id: string; name: string }[]; cats: string[]; accounts: Account[]; onDone: () => void }) {
  const open = t.status === "unmatched" || t.status === "suggested";
  const [kind, setKind] = useState(t.kind || "");
  const [sup, setSup] = useState(t.supplier_id || "");
  const [cat, setCat] = useState(t.category || "");
  const [edit, setEdit] = useState(false);
  const { run, busy, error } = useAction();
  const choices = t.amount_paise < 0 ? OUT : IN;
  const save = async () => {
    const r = await run(() => api(`/banks/txns/${t.id}`, { body: { kind, supplier_id: kind === "supplier" ? sup : null, category: kind === "expense" ? cat : null } }));
    if (r) { setEdit(false); onDone(); }
  };
  const skip = async () => { if (await run(() => api(`/banks/txns/${t.id}/ignore`, { method: "POST" }))) onDone(); };
  const ready = kind && (kind !== "supplier" || sup) && (kind !== "expense" || cat.trim());
  return (
    <li className={`bank-line ${open ? "open" : ""}`}>
      <div className="bank-line-top">
        <span className="small muted">{day(t.date)}</span>
        <span className="bank-narr">{t.narration}</span>
        <strong className={t.amount_paise > 0 ? "in" : "out"}>{t.amount_paise > 0 ? "+" : "−"}{formatPaise(Math.abs(t.amount_paise))}</strong>
      </div>
      {!open && !edit && (
        <div className="small muted">{t.status === "ignored" ? "Left out" : t.kind_label}{t.category ? ` · ${t.category}` : ""}{t.note ? ` · ${t.note}` : ""}
          {" "}<button className="link small" onClick={() => setEdit(true)}>Change</button></div>)}
      {(open || edit) && (
        <div className="row wrap bank-pick">
          {t.note && <span className="small muted">{t.note}</span>}
          <Select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="What was this money?">
            <option value="">What was it?</option>
            {choices.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </Select>
          {kind === "supplier" && (
            <Select value={sup} onChange={(e) => setSup(e.target.value)} aria-label="Supplier">
              <option value="">Which supplier?</option>
              {sups.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>)}
          {kind === "expense" && (
            <>
              <Input list={`cats-${t.id}`} value={cat} onChange={(e) => setCat(e.target.value)} placeholder="For what? e.g. Electricity" aria-label="Expense head" />
              <datalist id={`cats-${t.id}`}>{cats.map((c) => <option key={c} value={c} />)}</datalist>
            </>)}
          <Button disabled={busy || !ready} onClick={save}>Save</Button>
          {open && <Button variant="ghost" disabled={busy} onClick={skip}>Leave out</Button>}
          {edit && <Button variant="ghost" onClick={() => setEdit(false)}>Cancel</Button>}
          {accounts.length > 1 && kind === "transfer" && <span className="small muted">Found automatically when the other account's last 4 digits are in the description.</span>}
        </div>)}
      <ErrorText error={error} />
    </li>
  );
}

function AccountForm({ acct, onDone, onCancel }: { acct?: Account; onDone: () => void; onCancel?: () => void }) {
  const [name, setName] = useState(acct?.name || "");
  const [bank, setBank] = useState(acct?.bank || "");
  const [last4, setLast4] = useState(acct?.last4 || "");
  const [opening, setOpening] = useState(acct ? String(acct.opening_paise / 100) : "");
  const [openDate, setOpenDate] = useState(acct?.opening_date || "");
  const [tally, setTally] = useState(acct?.tally_ledger || "");
  const [outlets, setOutlets] = useState<{ id: string; name: string }[]>([]);
  const [picked, setPicked] = useState<string[]>(acct?.outlet_ids || []);
  const { run, busy, error } = useAction();
  useEffect(() => { api<{ id: string; name: string }[]>("/tenant/outlets").then(setOutlets).catch(() => setOutlets([])); }, []);
  const save = async () => {
    const body = { name: name.trim(), bank: bank || null, last4: last4 || null, outlet_ids: picked.length ? picked : null,
      opening_paise: inputToPaise(opening.replace(/^-/, "")) ?? 0, opening_date: openDate || null, tally_ledger: tally || null, is_active: true };
    if (opening.trim().startsWith("-")) body.opening_paise = -body.opening_paise;
    const r = await run(() => acct ? api(`/banks/${acct.id}`, { method: "PUT", body }) : api("/banks", { body }));
    if (r) onDone();
  };
  return (
    <Card title={acct ? `Settings · ${acct.name}` : "Add a bank account"}>
      <div className="grid-2">
        <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="HDFC Current" /></Field>
        <Field label="Bank"><Input value={bank} onChange={(e) => setBank(e.target.value)} placeholder="HDFC Bank" /></Field>
        <Field label="Last 4 digits of the account"><Input inputMode="numeric" maxLength={4} value={last4} onChange={(e) => setLast4(e.target.value.replace(/\D/g, ""))} /></Field>
        <Field label="Name in Tally (optional)"><Input value={tally} onChange={(e) => setTally(e.target.value)} placeholder={name || "HDFC Bank"} /></Field>
        <Field label="Opening balance (optional)"><Input inputMode="decimal" value={opening} onChange={(e) => setOpening(e.target.value)} /></Field>
        <Field label="On date"><Input type="date" value={openDate} onChange={(e) => setOpenDate(e.target.value)} /></Field>
      </div>
      {outlets.length > 0 && (
        <fieldset className="plain-fieldset">
          <legend className="small">Card and UPI money from these outlets lands here (to find missing money). None ticked = all.</legend>
          <div className="row wrap">
            {outlets.map((o) => (
              <label key={o.id} className="check"><input type="checkbox" checked={picked.includes(o.id)}
                onChange={(e) => setPicked((p) => e.target.checked ? [...p, o.id] : p.filter((x) => x !== o.id))} /> {o.name}</label>))}
          </div>
        </fieldset>)}
      <p className="muted small">Only the last 4 digits are kept, never the full account number.</p>
      <ErrorText error={error} />
      <div className="row">
        <Button disabled={busy || !name.trim()} onClick={save}>{acct ? "Save" : "Add account"}</Button>
        {onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}
      </div>
    </Card>
  );
}
