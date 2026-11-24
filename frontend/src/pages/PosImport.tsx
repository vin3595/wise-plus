import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { formatPaise } from "../money";
import type { Outlet } from "../types";
import { Button, Card, ErrorText, Field, Select, useAction } from "../ui";

interface FieldDef { key: string; label: string; required: boolean }
interface Check {
  ok: boolean; fields: FieldDef[]; headers: string[]; mapping: Record<string, string>; item_wise: boolean;
  errors: { row: number; message: string }[]; warnings: string[];
  bills_in_file?: number; already_imported?: number; bills?: number; cancelled?: number; date_from?: string; date_to?: string;
  sales_paise?: number; tax_paise?: number; discount_paise?: number; items?: number;
  months?: { month: string; bills: number; sales_paise: number; tax_paise: number }[];
  payments?: Record<string, number>;
  sample?: { bill_no: string; date: string; time: string | null; total_paise: number; tax_paise: number; payments: Record<string, number>; status: string; items: string[] }[];
  imported?: number;
}
interface Past { id: string; outlet: string; source: string; file_name: string; date_from: string; date_to: string; bills: number; total_paise: number; item_wise: boolean; created_at: string }

const MODE: Record<string, string> = { cash: "Cash", upi: "UPI", card: "Card", due: "Credit", complimentary: "Complimentary", online: "Online (Zomato / Swiggy)", other: "Other", part: "Part payment" };
const SOURCES = ["Petpooja", "Posist", "DotPe", "Rista", "Torqus", "Zomato / Swiggy", "Excel", "Other"];
const month = (m: string) => new Date(`${m}-01T00:00:00`).toLocaleDateString("en-IN", { month: "short", year: "numeric" });
const day = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });

/** Bring sales from the POS used before EMB, so reports and GST cover the whole year. */
export default function PosImport() {
  const [outlets, setOutlets] = useState<Outlet[]>([]);
  const [outletId, setOutletId] = useState("");
  const [source, setSource] = useState("Petpooja");
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState<Record<string, string> | null>(null);
  const [check, setCheck] = useState<Check | null>(null);
  const [done, setDone] = useState<Check | null>(null);
  const [past, setPast] = useState<Past[]>([]);
  const [showCols, setShowCols] = useState(false);
  const [sure, setSure] = useState<string | null>(null);
  const { busy, error, run } = useAction();

  const loadPast = () => api<Past[]>("/pos-import").then(setPast).catch(() => undefined);
  useEffect(() => {
    api<Outlet[]>("/tenant/outlets").then((o) => { setOutlets(o); setOutletId((c) => c || o[0]?.id || ""); });
    void loadPast();
  }, []);

  const form = (m: Record<string, string> | null) => {
    const fd = new FormData();
    fd.append("outlet_id", outletId);
    fd.append("source", source);
    fd.append("file", file!);
    if (m) fd.append("mapping", JSON.stringify(m));
    return fd;
  };
  const doCheck = (m: Record<string, string> | null) => run(async () => {
    const c = await api<Check>("/pos-import/check", { form: form(m) });
    setCheck(c); setMapping(c.mapping); setDone(null);
    if (!c.ok && c.headers.length) setShowCols(true);
  });
  const doImport = () => run(async () => {
    const r = await api<Check>("/pos-import", { form: form(mapping) });
    setDone(r); setCheck(null); setFile(null); void loadPast();
  });
  const undo = (p: Past) => run(async () => {
    await api(`/pos-import/${p.id}`, { method: "DELETE" });
    setSure(null);
    void loadPast();
  });

  return (
    <div className="stack">
      <div className="page-head"><h1>Import from another POS</h1></div>
      <p className="muted">Switched to EMB part way through the year? Bring in the sales from the POS you used before (Petpooja, Posist, DotPe, Rista or any Excel). Day-wise, item-wise, payment and GST reports then cover the whole year. Your EMB bills, bill numbers and cash drawers aren't touched.</p>

      <Card title="1. Export from your old POS">
        <ul className="small">
          <li><strong>Best:</strong> the <strong>item-wise / order-wise sales report</strong> (one row per item sold, with the bill number). This gives item reports too.</li>
          <li>Or the <strong>bill-wise / invoice report</strong> (one row per bill). Totals, payments and GST only.</li>
          <li>As Excel (.xlsx) or CSV. A few months at a time is fine; uploading the same bills twice is safe, they're skipped.</li>
          <li>In Petpooja: Reports › Item wise / Order report › pick the dates › Export to Excel.</li>
          <li>Stop the file the day <strong>before</strong> you started billing on EMB, so no day is counted twice.</li>
        </ul>
      </Card>

      <Card title="2. Upload and check">
        <div className="row wrap">
          {outlets.length > 1 && <Field label="Outlet"><Select value={outletId} onChange={(e) => { setOutletId(e.target.value); setCheck(null); }}>
            {outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select></Field>}
          <Field label="Old POS"><Select value={source} onChange={(e) => setSource(e.target.value)}>
            {SOURCES.map((s) => <option key={s}>{s}</option>)}</Select></Field>
        </div>
        <input type="file" accept=".xlsx,.csv" onChange={(e) => { setFile(e.target.files?.[0] || null); setCheck(null); setMapping(null); setDone(null); }} />
        <div className="row">
          <Button disabled={!file || !outletId || busy} onClick={() => doCheck(null)}>{busy && !check ? "Reading…" : "Check file"}</Button>
        </div>
        <ErrorText error={error} />
      </Card>

      {check && (
        <Card title={check.ok ? (check.bills ? "Ready to import" : "Nothing new in this file") : "Match the columns"}>
          {check.errors.map((e, i) => <p key={i} className="error">{e.row ? `Row ${e.row}: ` : ""}{e.message}</p>)}
          {check.ok && check.bills !== undefined && (
            <>
              <p><strong>{check.bills.toLocaleString("en-IN")} bills</strong> · {check.date_from && day(check.date_from)} to {check.date_to && day(check.date_to)} ·
                sales {formatPaise(check.sales_paise || 0)} · GST {formatPaise(check.tax_paise || 0)}
                {check.item_wise ? ` · ${check.items?.toLocaleString("en-IN")} item lines` : " · no items (bill-wise file)"}</p>
              <p className="muted small">{check.cancelled ? `${check.cancelled} cancelled bills kept aside (not counted). ` : ""}
                {check.already_imported ? `${check.already_imported} bills were imported earlier and will be skipped.` : ""}</p>
              {check.months && check.months.length > 0 && (
                <table className="table compact"><thead><tr><th>Month</th><th className="num">Bills</th><th className="num">Sales</th><th className="num">GST</th></tr></thead>
                  <tbody>{check.months.map((m) => <tr key={m.month}><td>{month(m.month)}</td><td className="num">{m.bills.toLocaleString("en-IN")}</td>
                    <td className="num">{formatPaise(m.sales_paise)}</td><td className="num">{formatPaise(m.tax_paise)}</td></tr>)}</tbody></table>
              )}
              {check.payments && <p className="small">Paid by: {Object.entries(check.payments).map(([m, a]) => `${MODE[m] || m} ${formatPaise(a)}`).join(" · ")}</p>}
              <p className="small muted">Check these against your old POS's own totals for the same months before importing.</p>
            </>
          )}
          {check.warnings.map((w, i) => <p key={i} className="warn small">⚠ {w}</p>)}
          {check.sample && check.sample.length > 0 && (
            <details><summary className="small">First bills as we read them</summary>
              <ul className="small">{check.sample.map((s) => (
                <li key={s.bill_no + s.date}><strong>{s.bill_no}</strong> · {day(s.date)}{s.time ? ` ${s.time}` : ""} · {formatPaise(s.total_paise)}
                  {" · "}{Object.keys(s.payments).map((m) => MODE[m] || m).join(" + ") || "—"}{s.status === "cancelled" ? " · cancelled" : ""}
                  {s.items.length > 0 && <div className="muted">{s.items.join(", ")}</div>}</li>))}</ul>
            </details>
          )}
          {check.headers.length > 0 && (
            <details open={showCols} onToggle={(e) => setShowCols((e.target as HTMLDetailsElement).open)}>
              <summary className="small">Columns we matched {check.ok ? "(change if something looks wrong)" : ""}</summary>
              <div className="pos-map">
                {check.fields.map((f) => (
                  <label key={f.key} className="field">
                    <span className="field-label">{f.label}{f.required ? " *" : ""}</span>
                    <Select value={mapping?.[f.key] || ""} onChange={(e) => setMapping({ ...(mapping || {}), [f.key]: e.target.value })}>
                      <option value="">— not in file —</option>
                      {check.headers.map((h) => <option key={h} value={h}>{h}</option>)}
                    </Select>
                  </label>
                ))}
              </div>
              <Button variant="ghost" disabled={busy} onClick={() => doCheck(mapping)}>Check again with these columns</Button>
            </details>
          )}
          {check.ok && !!check.bills && (
            <div className="row"><Button disabled={busy} onClick={doImport}>{busy ? "Importing…" : `Import ${check.bills.toLocaleString("en-IN")} bills`}</Button></div>
          )}
        </Card>
      )}

      {done && (
        <Card title="Imported">
          <p className="ok">{done.imported?.toLocaleString("en-IN")} bills from {done.date_from && day(done.date_from)} to {done.date_to && day(done.date_to)} are in.</p>
          <p className="small">See them in <Link to="/reports">Reports</Link>: Day-wise sales, Item-wise, Payments, GST by rate and HSN include them, and "Bills imported from your earlier POS" lists every one.</p>
        </Card>
      )}

      {past.length > 0 && (
        <Card title="Imported so far">
          <table className="table compact"><thead><tr><th>File</th><th>Outlet</th><th>Dates</th><th className="num">Bills</th><th className="num">Sales</th><th /></tr></thead>
            <tbody>{past.map((p) => (
              <tr key={p.id}><td>{p.file_name}<div className="muted small">{p.source}{p.item_wise ? " · with items" : " · bills only"}</div></td><td>{p.outlet}</td>
                <td>{day(p.date_from)} – {day(p.date_to)}</td><td className="num">{p.bills.toLocaleString("en-IN")}</td>
                <td className="num">{formatPaise(p.total_paise)}</td>
                <td>{sure === p.id
                  ? <span className="small">Take out these {p.bills.toLocaleString("en-IN")} bills? <button className="link" disabled={busy} onClick={() => undo(p)}>Yes, take out</button> · <button className="link" onClick={() => setSure(null)}>Keep</button></span>
                  : <button className="link small" onClick={() => setSure(p.id)}>Take out</button>}</td></tr>))}</tbody></table>
        </Card>
      )}
    </div>
  );
}
