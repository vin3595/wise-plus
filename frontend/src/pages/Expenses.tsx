import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Camera, FileText, Plus } from "lucide-react";
import { api, tokenStore } from "../api";
import { formatPaise, inputToPaise } from "../money";
import { clearShared, sharedFiles } from "../shared";
import { useSession } from "../session";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

type From = "petty" | "counter" | "owner" | "upi";
const FROM: Record<From, string> = { petty: "Petty cash", counter: "Counter cash", owner: "Owner's pocket", upi: "UPI / bank" };
interface Rate { unit: string | null; qty_m: number; amount_paise: number; paise_per_unit: number; stock_item_id: string | null }
interface Setup {
  categories: string[]; stock_items: { id: string; name: string; unit: string; units: string[] }[]; vendors: Vendor[];
  links: Record<string, { stock_item_id: string | null; rate: Rate | null }>;
  wallet: { in_hand_paise: number; since_topup_paise: number; since_topup_count: number; since_topup_no_slip: number; last_topup: { amount_paise: number; at: string } | null } | null;
}
interface Vendor { id: string; name: string; outlet_id: string | null; category: string; what: string | null; qty_m: number | null; unit: string | null;
  stock_item_id: string | null; amount_paise: number; paid_from: From; is_active: boolean }
interface Exp { id: string; spent_on: string; amount_paise: number; category: string; what: string | null; qty_m: number | null; unit: string | null;
  stock_item: string | null; paid_from: From; paid_label: string; has_slip: boolean; source: string; status: string; cancel_reason: string | null }
interface List { expenses: Exp[]; total_paise: number; no_slip: number; by_category: { category: string; amount_paise: number; count: number; qty: Record<string, number> }[];
  by_paid_from: { paid_from: From; label: string; amount_paise: number }[] }
interface Row { spent_on: string; amount: string; category: string; what: string; qty: string; unit: string; stock_item_id: string; paid_from: From; qtyAuto: boolean }

const iso = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const today = () => iso(new Date());
const day = (d: string) => new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" });
const qtyText = (m: number | null, u: string | null) => (m ? `${+(m / 1000).toFixed(3)} ${u || ""}`.trim() : "");
const blank = (): Row => ({ spent_on: today(), amount: "", category: "", what: "", qty: "", unit: "", stock_item_id: "", paid_from: "petty", qtyAuto: false });
function month(k: string): [string, string] {
  const t = new Date();
  if (k === "last") return [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))];
  if (k === "today") return [today(), today()];
  return [iso(new Date(t.getFullYear(), t.getMonth(), 1)), today()];
}

/** Money › Petty expenses: loose slips (snap or type), with quantity for stock and recipe costs; petty cash; regular vendors. */
export default function Expenses() {
  const { can } = useSession();
  const boss = can("approve");
  const [outlets, setOutlets] = useState<{ id: string; name: string }[]>([]);
  const [outlet, setOutlet] = useState("");
  const [setup, setSetup] = useState<Setup | null>(null);
  const [period, setPeriod] = useState("this");
  const [cat, setCat] = useState("");
  const [list, setList] = useState<List | null>(null);
  const [n, setN] = useState(0);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [slip, setSlip] = useState<{ token: string; hash: string; note: string | null; gst: boolean; already: { spent_on: string; amount_paise: number; category: string } | null } | null>(null);
  const [topup, setTopup] = useState(false);
  const [vendorEdit, setVendorEdit] = useState<Vendor | "new" | null>(null);
  const [useV, setUseV] = useState<{ v: Vendor; amount: string; qty: string; from: From } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const act = useAction();
  const file = useRef<HTMLInputElement>(null);

  useEffect(() => { api<{ id: string; name: string }[]>("/tenant/outlets").then((o) => { setOutlets(o); setOutlet((x) => x || o[0]?.id || ""); }).catch(() => setOutlets([])); }, []);
  const reload = useCallback(() => setN((x) => x + 1), []);
  useEffect(() => { if (outlet) api<Setup>(`/expenses/setup?outlet_id=${outlet}`).then(setSetup).catch(() => setSetup(null)); }, [outlet, n]);
  useEffect(() => {
    if (!outlet) return;
    const [d1, d2] = month(period);
    const q = new URLSearchParams({ outlet_id: outlet, d1, d2, state: "all", ...(cat ? { category: cat } : {}) });
    api<List>(`/expenses?${q}`).then(setList).catch(() => setList(null));
  }, [outlet, period, cat, n]);

  const flash = (t: string) => { setMsg(t); setTimeout(() => setMsg(null), 4000); };
  const snap = (f: File | undefined) => {
    if (!f) return;
    act.run(async () => {
      const fd = new FormData();
      fd.append("outlet_id", outlet);
      fd.append("file", f);
      const r = await api<{ slip_token: string; slip_hash: string; has_gstin: boolean; note: string | null; already: NonNullable<typeof slip>["already"];
        drafts: { spent_on: string; amount_paise: number; category: string; what: string | null; qty_m: number | null; unit: string | null; stock_item_id: string | null; qty_guessed?: boolean }[] }>("/expenses/scan", { form: fd });
      setSlip({ token: r.slip_token, hash: r.slip_hash, note: r.note, gst: r.has_gstin, already: r.already });
      setRows(r.drafts.map((d) => ({ spent_on: d.spent_on, amount: String(d.amount_paise / 100), category: d.category, what: d.what || "",
        qty: d.qty_m ? String(+(d.qty_m / 1000).toFixed(3)) : "", unit: d.unit || "", stock_item_id: d.stock_item_id || "", paid_from: "petty", qtyAuto: !!d.qty_guessed })));
    });
    if (file.current) file.current.value = "";
  };
  // A slip shared in from WhatsApp/Gallery (Android share sheet → "Petty cash slip"): read it straight away.
  const sharedDone = useRef(false);
  useEffect(() => {
    if (sharedDone.current || !outlet || !new URLSearchParams(location.search).has("shared")) return;
    sharedDone.current = true;
    sharedFiles().then(async (f) => { await clearShared(); history.replaceState(null, "", "/expenses"); if (f[0]) snap(f[0]); }).catch(() => undefined);
  }, [outlet]);   // eslint-disable-line react-hooks/exhaustive-deps
  const save = () => act.run(async () => {
    const items = (rows || []).filter((r) => (inputToPaise(r.amount) ?? 0) > 0).map((r) => ({
      amount_paise: inputToPaise(r.amount)!, category: r.category || "Other", what: r.what || null, spent_on: r.spent_on || null, paid_from: r.paid_from,
      qty_m: r.qty ? Math.round(Number(r.qty) * 1000) : null, unit: r.qty ? r.unit || null : null, stock_item_id: r.qty && r.stock_item_id ? r.stock_item_id : null }));
    if (!items.length) throw new Error("Enter an amount");
    const r = await api<{ saved: number; total_paise: number }>("/expenses", { body: { outlet_id: outlet, items, slip_token: slip?.token || null, slip_hash: slip?.hash || null } });
    setRows(null); setSlip(null); reload();
    flash(`Saved ${r.saved} · ${formatPaise(r.total_paise)}`);
  });
  const useVendor = () => act.run(async () => {
    if (!useV) return;
    const amt = inputToPaise(useV.amount);
    await api(`/expenses/vendors/${useV.v.id}/use`, { body: { outlet_id: outlet, amount_paise: amt && amt !== useV.v.amount_paise ? amt : null,
      qty_m: useV.qty ? Math.round(Number(useV.qty) * 1000) : null, paid_from: useV.from } });
    flash(`${useV.v.name}: ${formatPaise(amt || useV.v.amount_paise)} saved`);
    setUseV(null); reload();
  });
  const cancel = (e: Exp) => {
    const reason = prompt(`Cancel ${formatPaise(e.amount_paise)} · ${e.category}? Why?`);
    if (!reason || reason.trim().length < 3) return;
    act.run(async () => { await api(`/expenses/${e.id}/cancel`, { body: { reason } }); reload(); });
  };
  const viewSlip = async (e: Exp) => {
    const r = await fetch(`/api/expenses/${e.id}/slip`, { headers: { Authorization: `Bearer ${tokenStore.get()}` } });
    if (r.ok) window.open(URL.createObjectURL(await r.blob()), "_blank");
  };
  const w = setup?.wallet;
  const vendors = setup?.vendors || [];

  return (
    <div className="stack">
      <h1>Petty expenses</h1>
      <p className="muted">Loose slips and small cash spends. Snap the slips (or type them), and add the quantity: milk, ice, fruit and the like go
        into stock at the price you paid, so recipe costs stay right. You can also send a slip photo, or <strong>kharcha milk 300 6L</strong>, on WhatsApp.</p>
      <div className="row wrap">
        <Select value={outlet} onChange={(e) => setOutlet(e.target.value)} aria-label="Outlet">{outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select>
      </div>
      {msg && <p className="ok-text">{msg}</p>}

      {w && (
        <Card title="Petty cash" actions={boss && <Button variant="ghost" onClick={() => setTopup(!topup)}>Top up</Button>}>
          <p className="roi-big">{formatPaise(w.in_hand_paise)} <small className="muted">in hand</small></p>
          <p className="muted small">{w.last_topup ? <>Since the last top-up ({formatPaise(w.last_topup.amount_paise)}, {new Date(w.last_topup.at).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}): </> : "Spent so far: "}
            {formatPaise(w.since_topup_paise)} in {w.since_topup_count} entr{w.since_topup_count === 1 ? "y" : "ies"}
            {w.since_topup_no_slip ? <strong className="error-text"> · {w.since_topup_no_slip} without a slip</strong> : null}</p>
          {topup && <Topup outlet={outlet} noSlip={w.since_topup_no_slip} onDone={() => { setTopup(false); reload(); }} />}
        </Card>
      )}

      <Card title="Add">
        <div className="row wrap">
          <Button onClick={() => file.current?.click()} disabled={act.busy || !outlet}><Camera size={16} aria-hidden /> {act.busy && !rows ? "Reading…" : "Snap slips"}</Button>
          <input ref={file} type="file" accept="image/*,application/pdf" capture="environment" hidden onChange={(e) => snap(e.target.files?.[0])} />
          <Button variant="ghost" onClick={() => { setSlip(null); setRows([blank()]); }} disabled={!outlet}><Plus size={16} aria-hidden /> Type it in</Button>
        </div>
        {vendors.length > 0 && <>
          <p className="muted small" style={{ marginTop: "0.8rem" }}>Regulars: one tap for today's delivery</p>
          <div className="chips">{vendors.map((v) => (
            <button key={v.id} type="button" className="chip" onClick={() => setUseV({ v, amount: String(v.amount_paise / 100), qty: v.qty_m ? String(v.qty_m / 1000) : "", from: v.paid_from })}>
              {v.name} · {formatPaise(v.amount_paise)}{v.qty_m ? ` · ${qtyText(v.qty_m, v.unit)}` : ""}</button>))}</div>
        </>}
        {useV && (
          <div className="sub-form">
            <strong>{useV.v.name}</strong>
            <div className="row wrap">
              <Field label="Amount ₹"><Input inputMode="decimal" value={useV.amount} onChange={(e) => {
                const a = e.target.value; const v = useV.v;
                const q = v.qty_m && v.amount_paise && inputToPaise(a) ? String(+((v.qty_m / 1000) * (inputToPaise(a)! / v.amount_paise)).toFixed(2)) : useV.qty;
                setUseV({ ...useV, amount: a, qty: q });
              }} /></Field>
              {useV.v.unit && <Field label={`Quantity (${useV.v.unit})`}><Input inputMode="decimal" value={useV.qty} onChange={(e) => setUseV({ ...useV, qty: e.target.value })} /></Field>}
              <Field label="Paid from"><Select value={useV.from} onChange={(e) => setUseV({ ...useV, from: e.target.value as From })}>
                {Object.entries(FROM).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</Select></Field>
            </div>
            <div className="row"><Button onClick={useVendor} disabled={act.busy}>Save</Button><Button variant="ghost" onClick={() => setUseV(null)}>Cancel</Button></div>
          </div>
        )}
        {boss && <p className="small" style={{ marginTop: "0.6rem" }}><button className="link" onClick={() => setVendorEdit("new")}>+ Add a regular vendor</button>
          {vendors.length > 0 && <> · <button className="link" onClick={() => setVendorEdit(vendors[0])}>Edit regulars</button></>}</p>}
        {vendorEdit && setup && <VendorForm setup={setup} outlets={outlets} vendors={vendors} start={vendorEdit} onDone={() => { setVendorEdit(null); reload(); }} />}

        {rows && setup && (
          <div className="sub-form">
            {slip?.already && <p className="error-text">This slip photo was already saved on {day(slip.already.spent_on)} ({formatPaise(slip.already.amount_paise)} · {slip.already.category}). Saving again would count it twice.</p>}
            {slip?.gst && <p className="muted small">This slip has a GSTIN. If it's a proper bill, add it as a purchase instead so you can claim the GST.</p>}
            {slip?.note && <p className="muted small">Note from the reader: {slip.note}</p>}
            {rows.map((r, i) => <RowEdit key={i} row={r} setup={setup} onChange={(x) => setRows(rows.map((y, j) => (j === i ? x : y)))}
              onRemove={rows.length > 1 ? () => setRows(rows.filter((_, j) => j !== i)) : undefined} />)}
            <p className="small"><button className="link" onClick={() => setRows([...rows, { ...blank(), paid_from: rows[rows.length - 1]?.paid_from || "petty", spent_on: rows[rows.length - 1]?.spent_on || today() }])}>+ Another line</button></p>
            <ErrorText error={act.error} />
            <div className="row">
              <Button onClick={save} disabled={act.busy}>Save {formatPaise(rows.reduce((s, r) => s + (inputToPaise(r.amount) ?? 0), 0))}</Button>
              <Button variant="ghost" onClick={() => { setRows(null); setSlip(null); }}>Cancel</Button>
            </div>
          </div>
        )}
        {!rows && <ErrorText error={act.error} />}
      </Card>

      <Card title="Spent" actions={<Select value={period} onChange={(e) => setPeriod(e.target.value)} aria-label="Period">
        <option value="today">Today</option><option value="this">This month</option><option value="last">Last month</option></Select>}>
        {!list ? <p className="muted">Loading…</p> : <>
          <p className="roi-big">{formatPaise(list.total_paise)}{list.no_slip ? <small className="error-text"> · {list.no_slip} without a slip</small> : null}</p>
          <p className="muted small">{list.by_paid_from.map((p) => `${p.label} ${formatPaise(p.amount_paise)}`).join(" · ")}</p>
          <div className="chips">
            {cat && <button className="chip on" onClick={() => setCat("")}>All ✕</button>}
            {list.by_category.map((c) => (
              <button key={c.category} className={`chip${cat === c.category ? " on" : ""}`} onClick={() => setCat(cat === c.category ? "" : c.category)}>
                {c.category} {formatPaise(c.amount_paise)}{Object.entries(c.qty).map(([u, m]) => ` · ${+(m / 1000).toFixed(2)} ${u}`).join("")}</button>))}
          </div>
          {list.expenses.length === 0 ? <p className="muted">Nothing yet.</p> : (
            <table className="table">
              <thead><tr><th>Day</th><th>What</th><th>Qty</th><th>Paid from</th><th className="num">₹</th><th /></tr></thead>
              <tbody>{list.expenses.map((e) => (
                <tr key={e.id} className={e.status === "cancelled" ? "cancelled" : ""}>
                  <td>{day(e.spent_on)}</td>
                  <td><strong>{e.category}</strong>{e.what && e.what.toLowerCase() !== e.category.toLowerCase() ? <span className="muted"> · {e.what}</span> : null}
                    {e.status === "cancelled" && <div className="small muted">Cancelled: {e.cancel_reason}</div>}</td>
                  <td>{qtyText(e.qty_m, e.unit)}{e.stock_item ? <div className="small muted">📦 {e.stock_item}</div> : null}</td>
                  <td>{e.paid_label}</td>
                  <td className="num">{formatPaise(e.amount_paise)}</td>
                  <td className="nowrap">{e.has_slip ? <button className="link" onClick={() => viewSlip(e)} title="See the slip"><FileText size={16} aria-label="Slip" /></button>
                    : e.source !== "vendor" && e.status === "ok" ? <span className="error-text small" title="No slip photo">no slip</span> : null}
                    {boss && e.status === "ok" && <> <button className="link danger small" onClick={() => cancel(e)}>Cancel</button></>}</td>
                </tr>))}</tbody>
            </table>)}
        </>}
      </Card>
    </div>
  );
}

function RowEdit({ row: r, setup, onChange, onRemove }: { row: Row; setup: Setup; onChange: (r: Row) => void; onRemove?: () => void }) {
  const set = (p: Partial<Row>) => onChange({ ...r, ...p });
  const item = setup.stock_items.find((s) => s.id === r.stock_item_id);
  // ₹300 of milk at last time's rate = 6 L (filled in, change it if it was different)
  const fromRate = (amount: string, category: string): Partial<Row> => {
    const l = setup.links[category];
    const p = inputToPaise(amount);
    const out: Partial<Row> = {};
    if (l?.stock_item_id && !r.stock_item_id) out.stock_item_id = l.stock_item_id;
    if (l?.rate && p && (r.qtyAuto || !r.qty)) {
      out.qty = String(+((p * 1000) / l.rate.paise_per_unit / 1000).toFixed(2));
      out.unit = l.rate.unit || "";
      out.qtyAuto = true;
    }
    return out;
  };
  const units = item ? [...new Set([item.unit === "ml" ? "L" : item.unit === "g" ? "kg" : "pc", item.unit, ...item.units])] : ["L", "kg", "pc", "dozen", "packet", "block", "can"];
  return (
    <div className="exp-row">
      <Input type="date" value={r.spent_on} onChange={(e) => set({ spent_on: e.target.value })} aria-label="Day" />
      <Input inputMode="decimal" placeholder="₹" value={r.amount} onChange={(e) => set({ amount: e.target.value, ...fromRate(e.target.value, r.category) })} aria-label="Amount" />
      <Input list="exp-cats" placeholder="Milk, Ice, Auto…" value={r.category} onChange={(e) => set({ category: e.target.value, ...fromRate(r.amount, e.target.value) })} aria-label="Category" />
      <Input placeholder="Note (optional)" value={r.what} onChange={(e) => set({ what: e.target.value })} aria-label="What" />
      <span className="qty">
        <Input inputMode="decimal" placeholder="Qty" value={r.qty} onChange={(e) => set({ qty: e.target.value, qtyAuto: false })} aria-label="Quantity" />
        <Select value={r.unit} onChange={(e) => set({ unit: e.target.value })} aria-label="Unit"><option value="">unit</option>{units.map((u) => <option key={u}>{u}</option>)}</Select>
      </span>
      <Select value={r.stock_item_id} onChange={(e) => set({ stock_item_id: e.target.value })} aria-label="Stock item" title="Adds the quantity to this stock item at the price paid">
        <option value="">No stock item</option>{setup.stock_items.map((s) => <option key={s.id} value={s.id}>📦 {s.name}</option>)}</Select>
      <Select value={r.paid_from} onChange={(e) => set({ paid_from: e.target.value as From })} aria-label="Paid from">
        {Object.entries(FROM).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</Select>
      {r.qtyAuto && r.qty && <span className="muted small">qty from last time's rate</span>}
      {onRemove && <button className="link danger small" onClick={onRemove}>Remove</button>}
      <datalist id="exp-cats">{setup.categories.map((c) => <option key={c} value={c} />)}</datalist>
    </div>
  );
}

function Topup({ outlet, noSlip, onDone }: { outlet: string; noSlip: number; onDone: () => void }) {
  const [amt, setAmt] = useState("");
  const [src, setSrc] = useState("owner");
  const act = useAction();
  return (
    <div className="sub-form">
      {noSlip > 0 && <p className="error-text small">{noSlip} spend{noSlip === 1 ? "" : "s"} since the last top-up have no slip. Ask for them before topping up.</p>}
      <div className="row wrap">
        <Field label="Amount ₹"><Input inputMode="decimal" value={amt} onChange={(e) => setAmt(e.target.value)} placeholder="5000" /></Field>
        <Field label="From"><Select value={src} onChange={(e) => setSrc(e.target.value)}>
          <option value="owner">Owner</option><option value="counter">Counter cash (open shift)</option><option value="bank">Bank</option></Select></Field>
      </div>
      <ErrorText error={act.error} />
      <Button disabled={act.busy || !inputToPaise(amt)} onClick={() => act.run(async () => {
        await api("/expenses/topups", { body: { outlet_id: outlet, amount_paise: inputToPaise(amt), source: src } }); onDone();
      })}>Add to petty cash</Button>
    </div>
  );
}

function VendorForm({ setup, outlets, vendors, start, onDone }: { setup: Setup; outlets: { id: string; name: string }[]; vendors: Vendor[]; start: Vendor | "new"; onDone: () => void }) {
  const [cur, setCur] = useState<Vendor | "new">(start);
  const v = cur === "new" ? null : cur;
  const init = useMemo(() => ({ name: v?.name || "", outlet_id: v?.outlet_id || "", category: v?.category || "", what: v?.what || "",
    qty: v?.qty_m ? String(v.qty_m / 1000) : "", unit: v?.unit || "", stock_item_id: v?.stock_item_id || "", amount: v ? String(v.amount_paise / 100) : "",
    paid_from: (v?.paid_from || "petty") as From, is_active: v?.is_active ?? true }), [v]);
  const [f, setF] = useState(init);
  useEffect(() => setF(init), [init]);
  const act = useAction();
  const item = setup.stock_items.find((s) => s.id === f.stock_item_id);
  return (
    <div className="sub-form">
      {vendors.length > 0 && <Select value={v?.id || "new"} onChange={(e) => setCur(vendors.find((x) => x.id === e.target.value) || "new")} aria-label="Vendor">
        <option value="new">+ New regular</option>{vendors.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</Select>}
      <div className="row wrap">
        <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Milkman" /></Field>
        <Field label="Category"><Input list="exp-cats-v" value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })} placeholder="Milk" /></Field>
        <Field label="Usual amount ₹"><Input inputMode="decimal" value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} placeholder="300" /></Field>
        <Field label="Usual quantity"><span className="qty"><Input inputMode="decimal" value={f.qty} onChange={(e) => setF({ ...f, qty: e.target.value })} placeholder="6" />
          <Input value={f.unit} onChange={(e) => setF({ ...f, unit: e.target.value })} placeholder={item ? (item.unit === "ml" ? "L" : item.unit === "g" ? "kg" : "pc") : "L"} /></span></Field>
        <Field label="Stock item"><Select value={f.stock_item_id} onChange={(e) => setF({ ...f, stock_item_id: e.target.value })}>
          <option value="">None</option>{setup.stock_items.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</Select></Field>
        <Field label="Outlet"><Select value={f.outlet_id} onChange={(e) => setF({ ...f, outlet_id: e.target.value })}>
          <option value="">All outlets</option>{outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select></Field>
        <Field label="Usually paid from"><Select value={f.paid_from} onChange={(e) => setF({ ...f, paid_from: e.target.value as From })}>
          {Object.entries(FROM).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</Select></Field>
      </div>
      <label className="check"><input type="checkbox" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} /> In use</label>
      <datalist id="exp-cats-v">{setup.categories.map((c) => <option key={c} value={c} />)}</datalist>
      <ErrorText error={act.error} />
      <div className="row">
        <Button disabled={act.busy || !f.name || !f.category || !inputToPaise(f.amount)} onClick={() => act.run(async () => {
          const body = { name: f.name, outlet_id: f.outlet_id || null, category: f.category, what: f.what || null, amount_paise: inputToPaise(f.amount),
            qty_m: f.qty ? Math.round(Number(f.qty) * 1000) : null, unit: f.qty ? f.unit || null : null, stock_item_id: f.stock_item_id || null,
            paid_from: f.paid_from, is_active: f.is_active };
          if (v) await api(`/expenses/vendors/${v.id}`, { method: "PUT", body }); else await api("/expenses/vendors", { body });
          onDone();
        })}>Save</Button>
        <Button variant="ghost" onClick={onDone}>Close</Button>
      </div>
    </div>
  );
}
