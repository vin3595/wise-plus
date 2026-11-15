import { useCallback, useEffect, useRef, useState } from "react";
import { ScanLine } from "lucide-react";
import { api, tokenStore } from "../api";
import { formatPaise } from "../money";
import { useSession } from "../session";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

type Named = { name: string; amount: number };
type Cash = { d500: number; d200: number; d100: number; d50: number; d20: number; d10: number; coins: number; total: number };
interface SheetData { date: string | null; cash_in: Cash; cash_out: Cash; vijay_ras_bhandar: Named[]; expenses: Named[]; expense_payouts: Named[];
  vijay_ras: Named[]; absent: string[]; advances: Named[]; dues: Named[]; notes: string }
interface Row { id: string; sheet_date: string | null; status: string; cash_in: number; cash_out: number; expenses: number; absent: number; has_photo: boolean }
interface Full extends Row { data: SheetData; summary: string; note?: string | null }
interface Outlet { id: string; name: string }

const DENOMS = [500, 200, 100, 50, 20, 10] as const;
const rupees = (n: number) => formatPaise(Math.round(n * 100));
const day = (d: string | null) => d ? new Date(d + "T00:00:00").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" }) : "No date";
const counted = (c: Cash) => DENOMS.reduce((s, d) => s + d * (Number(c[`d${d}` as keyof Cash]) || 0), 0) + (Number(c.coins) || 0);

/** Money > Daily sheets: the printed cash sheet, read from a photo (here or on WhatsApp), checked and saved. */
export default function Sheets() {
  const { me, deviceOutletId } = useSession();
  const [rows, setRows] = useState<Row[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [outlets, setOutlets] = useState<Outlet[]>([]);
  const [outletId, setOutletId] = useState(me?.outlet_id || deviceOutletId || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const load = useCallback(() => api<Row[]>("/daily-sheets").then(setRows), []);
  useEffect(() => { load(); api<Outlet[]>("/tenant/outlets").then((o) => { setOutlets(o); setOutletId((c) => c || o[0]?.id || ""); }); }, [load]);
  const upload = async (f: File) => {
    setBusy(true); setError(null);
    const fd = new FormData(); fd.append("outlet_id", outletId); fd.append("file", f);
    try { const s = await api<Full>("/daily-sheets/scan", { form: fd }); await load(); setOpen(s.id); }
    catch (e) { setError(e instanceof Error ? e.message : "Couldn't read that sheet"); }
    finally { setBusy(false); }
  };
  if (open) return <Sheet id={open} onBack={() => { setOpen(null); load(); }} />;
  return (
    <div className="stack">
      <h1>Daily sheets</h1>
      <Card title="Add today's sheet" actions={
        <div className="scan-bill">
          <input ref={file} type="file" accept="image/*,application/pdf" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) upload(f); }} />
          <button className="btn btn-primary" disabled={busy || !outletId} onClick={() => file.current?.click()}><ScanLine size={17} aria-hidden />{busy ? "Reading…" : "Scan sheet"}</button>
        </div>}>
        <p className="muted small">Photograph the whole sheet, or send it on WhatsApp. You check it before it's saved; one sheet per day.</p>
        {outlets.length > 1 && !me?.outlet_id && (
          <Field label="Outlet"><Select value={outletId} onChange={(e) => setOutletId(e.target.value)}>{outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select></Field>)}
        <ErrorText error={error} />
      </Card>
      <Card title="Saved sheets">
        {!rows ? <p className="muted">Loading…</p> : rows.length === 0 ? <p className="muted">No sheets yet.</p> : (
          <table className="table ledger">
            <thead><tr><th>Date</th><th>Expenses</th><th className="num">Cash in</th><th className="num">Cash out</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => setOpen(r.id)}>
                  <td>{day(r.sheet_date)}{r.status === "pending" && <span className="badge warn-badge">To check</span>}</td>
                  <td><button className="link" onClick={() => setOpen(r.id)}>{rupees(r.expenses)} expenses{r.absent ? ` · ${r.absent} absent` : ""}</button></td>
                  <td className="num">{rupees(r.cash_in)}</td>
                  <td className="num">{rupees(r.cash_out)}</td>
                </tr>))}
            </tbody>
          </table>)}
      </Card>
    </div>
  );
}

function Sheet({ id, onBack }: { id: string; onBack: () => void }) {
  const [s, setS] = useState<Full | null>(null);
  const [d, setD] = useState<SheetData | null>(null);
  const [date, setDate] = useState("");
  const [photo, setPhoto] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const { run, busy, error } = useAction();
  useEffect(() => {
    api<Full>(`/daily-sheets/${id}`).then((r) => { setS(r); setD(r.data); setDate(r.sheet_date || ""); });
    // The photo needs the sign-in token, so it's fetched rather than linked.
    fetch(`/api/daily-sheets/${id}/photo`, { headers: { Authorization: `Bearer ${tokenStore.get()}` } })
      .then((r) => (r.ok ? r.blob() : null)).then((b) => b && setPhoto(URL.createObjectURL(b))).catch(() => {});
  }, [id]);
  if (!s || !d) return <p className="muted">Loading…</p>;
  const setCash = (k: "cash_in" | "cash_out", f: keyof Cash, v: string) => setD({ ...d, [k]: { ...d[k], [f]: Number(v) || 0 } });
  const setRow = (k: "vijay_ras_bhandar" | "expenses" | "vijay_ras" | "advances" | "dues", i: number, patch: Partial<Named>) =>
    setD({ ...d, [k]: d[k].map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const addRow = (k: "vijay_ras_bhandar" | "expenses" | "vijay_ras" | "advances" | "dues") => setD({ ...d, [k]: [...d[k], { name: "", amount: 0 }] });
  const save = (final: boolean) => run(async () => {
    const r = await api<Full>(`/daily-sheets/${id}`, { method: "PUT", body: { data: { ...d, date: date || null }, sheet_date: date || null, save: final } });
    setS(r); setMsg(final || r.status === "saved" ? `Saved.${r.note ? " " + r.note : ""}` : "Changes kept. Tap Save when it's right.");
  });
  const discard = () => run(async () => { await api(`/daily-sheets/${id}/discard`, { method: "POST" }); onBack(); });
  const cashBox = (label: string, k: "cash_in" | "cash_out") => {
    const c = d[k]; const sum = counted(c);
    return (
      <div className="sheet-cash">
        <h3>{label}</h3>
        {DENOMS.map((den) => (
          <label key={den} className="sheet-den"><span>{den} ×</span>
            <Input inputMode="numeric" value={String(c[`d${den}` as keyof Cash] ?? 0)} onChange={(e) => setCash(k, `d${den}` as keyof Cash, e.target.value)} /></label>))}
        <label className="sheet-den"><span>Coins ₹</span><Input inputMode="decimal" value={String(c.coins ?? 0)} onChange={(e) => setCash(k, "coins", e.target.value)} /></label>
        <p className="sheet-total"><span>Total</span><strong>{rupees(sum)}</strong></p>
        <label className="sheet-den"><span>Total on sheet</span><Input inputMode="decimal" placeholder="Not written" value={c.total ? String(c.total) : ""} onChange={(e) => setCash(k, "total", e.target.value)} /></label>
        {c.total > 0 && Math.abs(c.total - sum) > 1 && <p className="small warn-text">⚠️ The sheet says {rupees(c.total)}, but the notes and coins add up to {rupees(sum)}.</p>}
      </div>);
  };
  const named = (label: string, k: "vijay_ras_bhandar" | "expenses" | "vijay_ras" | "advances" | "dues", hideZero = true) => {
    const list = d[k].map((r, i) => ({ r, i })).filter(({ r }) => !hideZero || r.amount || !r.name);
    const total = d[k].reduce((a, r) => a + (Number(r.amount) || 0), 0);
    return (
      <div className="sheet-sec">
        <h3>{label} <span className="muted small">{k === "vijay_ras_bhandar" || k === "vijay_ras" ? `${total} qty` : rupees(total)}</span></h3>
        {list.map(({ r, i }) => (
          <div key={i} className="sheet-row">
            <Input value={r.name} onChange={(e) => setRow(k, i, { name: e.target.value })} aria-label="Name" />
            <Input inputMode="decimal" className="num" value={String(r.amount)} onChange={(e) => setRow(k, i, { amount: Number(e.target.value) || 0 })} aria-label={`Amount for ${r.name}`} />
          </div>))}
        <button className="link small" onClick={() => addRow(k)}>+ Add a line</button>
      </div>);
  };
  return (
    <div className="stack">
      <div className="row between"><h1>{day(date || null)}</h1><button className="link" onClick={onBack}>All sheets</button></div>
      {s.status === "pending" && <p className="banner info">Read from the photo and not saved yet. Check the numbers against the sheet, then Save.</p>}
      <div className="sheet-grid">
        {photo && <a href={photo} target="_blank" rel="noreferrer" className="sheet-photo"><img src={photo} alt="The daily sheet" /></a>}
        <Card>
          <Field label="Date on the sheet"><Input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></Field>
          <div className="sheet-cash-pair">{cashBox("Cash in", "cash_in")}{cashBox("Cash out", "cash_out")}</div>
          {named("Expenses", "expenses")}
          {named("Vijay Ras in", "vijay_ras_bhandar")}
          {named("Vijay Ras out", "vijay_ras")}
          {named("Advances", "advances", false)}
          {named("Dues", "dues", false)}
          <Field label="Absent (comma between names)"><Input value={d.absent.join(", ")} onChange={(e) => setD({ ...d, absent: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} /></Field>
          <Field label="Notes"><Input value={d.notes} onChange={(e) => setD({ ...d, notes: e.target.value })} /></Field>
          <ErrorText error={error} />
          {msg && <p className="ok">{msg}</p>}
          <div className="row">
            <Button disabled={busy || !date} onClick={() => save(true)}>{s.status === "saved" ? "Save changes" : "Save"}</Button>
            <Button variant="ghost" disabled={busy} onClick={discard}>{s.status === "saved" ? "Delete" : "Discard"}</Button>
          </div>
          {!date && <p className="small warn-text">Set the date to save.</p>}
        </Card>
      </div>
    </div>
  );
}
