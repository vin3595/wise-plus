import { Fragment, useEffect, useMemo, useState } from "react";
import { Download, FileText, Trash2 } from "lucide-react";
import { api, tokenStore } from "../api";
import { formatPaise, inputToPaise } from "../money";
import { Button, Card, ErrorText, Input, Select, useAction } from "../ui";

interface Line { name: string; qty_m: number; unit: string; amount_paise: number; gst_bp: number }
interface P { id: string; outlet: string | null; supplier: string | null; supplier_id: string | null; invoice_no: string | null; invoice_date: string;
  amount_paise: number; tax_paise: number; total_paise: number; lines: Line[]; status: string; file_id: string | null; notes: string | null }
interface Res { purchases: P[]; count: number; amount_paise: number; tax_paise: number; total_paise: number }

const iso = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const day = (d: string) => new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "2-digit" });
function range(k: string): [string, string] {
  const t = new Date();
  if (k === "this") return [iso(new Date(t.getFullYear(), t.getMonth(), 1)), iso(t)];
  if (k === "last") return [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))];
  if (k === "90") return [iso(new Date(t.getTime() - 89 * 864e5)), iso(t)];
  if (k === "fy") { const y = t.getMonth() >= 3 ? t.getFullYear() : t.getFullYear() - 1; return [`${y}-04-01`, iso(t)]; }
  return ["", ""];
}

/** Stock › Purchase bills: every supplier bill in one list, filtered, with totals; delete the ones entered by mistake. */
export default function PurchaseBills() {
  const [preset, setPreset] = useState("this");
  const [[d1, d2], setDates] = useState<[string, string]>(range("this"));
  const [outlet, setOutlet] = useState("");
  const [supplier, setSupplier] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("posted");
  const [minT, setMinT] = useState("");
  const [maxT, setMaxT] = useState("");
  const [res, setRes] = useState<Res | null>(null);
  const [outlets, setOutlets] = useState<{ id: string; name: string }[]>([]);
  const [sups, setSups] = useState<{ id: string; name: string }[]>([]);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<string | null>(null);
  const [n, setN] = useState(0);
  const { run, busy, error } = useAction();
  useEffect(() => {
    api<{ id: string; name: string }[]>("/tenant/outlets").then(setOutlets).catch(() => setOutlets([]));
    api<{ id: string; name: string }[]>("/inventory/suppliers").then(setSups).catch(() => setSups([]));
  }, []);
  const params = useMemo(() => {
    const p = new URLSearchParams({ status });
    if (d1) p.set("d1", d1);
    if (d2) p.set("d2", d2);
    if (outlet) p.set("outlet_id", outlet);
    if (supplier) p.set("supplier_id", supplier);
    if (q.trim()) p.set("q", q.trim());
    const a = inputToPaise(minT), b = inputToPaise(maxT);
    if (minT && a !== null) p.set("min_total", String(a));
    if (maxT && b !== null) p.set("max_total", String(b));
    return p.toString();
  }, [d1, d2, outlet, supplier, q, status, minT, maxT]);
  useEffect(() => {
    const t = setTimeout(() => { api<Res>(`/inventory/purchases/all?${params}`).then((r) => { setRes(r); setSel(new Set()); }).catch(() => setRes(null)); }, 250);
    return () => clearTimeout(t);
  }, [params, n]);

  const rows = res?.purchases || [];
  const live = rows.filter((p) => p.status === "posted");
  const toggle = (id: string) => setSel((s) => { const x = new Set(s); if (x.has(id)) x.delete(id); else x.add(id); return x; });
  const del = () => {
    if (!confirm(`Delete ${sel.size} bill${sel.size === 1 ? "" : "s"}? Their stock comes back out and they leave supplier dues. You'll still find them under "Deleted".`)) return;
    run(async () => { await api("/inventory/purchases/delete", { body: { ids: [...sel] } }); setN((x) => x + 1); });
  };
  const viewFile = async (p: P) => {
    const r = await fetch(`/api/inventory/purchases/scan/kept/${p.file_id}/file`, { headers: { Authorization: `Bearer ${tokenStore.get()}` } });
    if (r.ok) window.open(URL.createObjectURL(await r.blob()), "_blank");
  };
  const csv = () => {
    const head = ["Date", "Outlet", "Supplier", "Bill no.", "Amount", "GST", "Total", "Status"];
    const lines = rows.map((p) => [p.invoice_date, p.outlet || "", p.supplier || "", p.invoice_no || "", p.amount_paise / 100, p.tax_paise / 100, p.total_paise / 100,
      p.status === "posted" ? "Saved" : "Deleted"].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([[head.join(","), ...lines].join("\n")], { type: "text/csv" }));
    a.download = `purchase-bills-${d1 || "all"}-to-${d2 || "today"}.csv`;
    a.click();
  };

  return (
    <div className="stack">
      <h1>Purchase bills</h1>
      <Card title="Filter">
        <div className="row wrap">
          <Select value={preset} onChange={(e) => { setPreset(e.target.value); if (e.target.value !== "custom") setDates(range(e.target.value)); }} aria-label="Dates">
            <option value="this">This month</option><option value="last">Last month</option><option value="90">Last 90 days</option>
            <option value="fy">This financial year</option><option value="all">All time</option><option value="custom">Choose dates</option>
          </Select>
          {preset === "custom" && <>
            <Input type="date" value={d1} onChange={(e) => setDates([e.target.value, d2])} aria-label="From" />
            <Input type="date" value={d2} onChange={(e) => setDates([d1, e.target.value])} aria-label="To" />
          </>}
          {outlets.length > 1 && <Select value={outlet} onChange={(e) => setOutlet(e.target.value)} aria-label="Outlet">
            <option value="">All outlets</option>{outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select>}
          <Select value={supplier} onChange={(e) => setSupplier(e.target.value)} aria-label="Supplier">
            <option value="">All suppliers</option>{sups.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</Select>
          <Select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="posted">Saved</option><option value="cancelled">Deleted</option><option value="all">Saved and deleted</option></Select>
        </div>
        <div className="row wrap">
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Bill no., supplier or item" aria-label="Search" />
          <Input inputMode="decimal" value={minT} onChange={(e) => setMinT(e.target.value)} placeholder="Min ₹" aria-label="Minimum total" style={{ width: "7rem" }} />
          <Input inputMode="decimal" value={maxT} onChange={(e) => setMaxT(e.target.value)} placeholder="Max ₹" aria-label="Maximum total" style={{ width: "7rem" }} />
        </div>
      </Card>

      <div className="stats">
        <div className="stat"><span>Bills</span><strong>{live.length}</strong></div>
        <div className="stat"><span>Before GST</span><strong>{formatPaise(res?.amount_paise || 0)}</strong></div>
        <div className="stat"><span>GST</span><strong>{formatPaise(res?.tax_paise || 0)}</strong></div>
        <div className="stat"><span>Total</span><strong>{formatPaise(res?.total_paise || 0)}</strong></div>
      </div>

      <Card title={`${rows.length} bill${rows.length === 1 ? "" : "s"}`} actions={<div className="row">
        {sel.size > 0 && <Button variant="ghost" disabled={busy} onClick={del}><Trash2 size={16} aria-hidden /> Delete {sel.size}</Button>}
        {rows.length > 0 && <Button variant="ghost" onClick={csv}><Download size={16} aria-hidden /> Excel (CSV)</Button>}</div>}>
        <ErrorText error={error} />
        {!res ? <p className="muted">Loading…</p> : rows.length === 0 ? <p className="muted">No bills match.</p> : (
          <table className="table">
            <thead><tr>
              <th><input type="checkbox" aria-label="Select all" checked={live.length > 0 && live.every((p) => sel.has(p.id))}
                onChange={(e) => setSel(e.target.checked ? new Set(live.map((p) => p.id)) : new Set())} /></th>
              <th>Date</th><th>Supplier</th><th>Bill no.</th>{outlets.length > 1 && <th>Outlet</th>}<th className="num">Total</th><th></th>
            </tr></thead>
            <tbody>
              {rows.map((p) => (
                <Fragment key={p.id}>
                  <tr className={p.status === "cancelled" ? "inactive" : ""} onClick={() => setOpen(open === p.id ? null : p.id)} style={{ cursor: "pointer" }}>
                    <td onClick={(e) => e.stopPropagation()}>{p.status === "posted" && <input type="checkbox" checked={sel.has(p.id)} onChange={() => toggle(p.id)} aria-label={`Select ${p.invoice_no || "bill"}`} />}</td>
                    <td>{day(p.invoice_date)}</td>
                    <td>{p.supplier || <span className="muted">—</span>}</td>
                    <td>{p.invoice_no || <span className="muted">—</span>}{p.status === "cancelled" && <span className="pill">Deleted</span>}</td>
                    {outlets.length > 1 && <td>{p.outlet}</td>}
                    <td className="num">{formatPaise(p.total_paise)}</td>
                    <td onClick={(e) => e.stopPropagation()}>{p.file_id && <button className="link" onClick={() => viewFile(p)} title="See the bill photo / PDF"><FileText size={16} aria-label="View bill" /></button>}</td>
                  </tr>
                  {open === p.id && (
                    <tr className="sub-row"><td colSpan={outlets.length > 1 ? 7 : 6}>
                      <ul className="plain">{p.lines.map((l, i) => (
                        <li key={i}>{l.name} · {Number((l.qty_m / 1000).toFixed(3))} {l.unit} · {formatPaise(l.amount_paise)}{l.gst_bp ? ` + ${l.gst_bp / 100}% GST` : ""}</li>))}</ul>
                      <span className="muted small">Before GST {formatPaise(p.amount_paise)} · GST {formatPaise(p.tax_paise)}{p.notes ? ` · ${p.notes}` : ""}</span>
                    </td></tr>)}
                </Fragment>))}
            </tbody>
          </table>)}
      </Card>
    </div>
  );
}
