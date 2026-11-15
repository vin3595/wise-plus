import { useEffect, useState } from "react";
import { api } from "../api";
import { formatPaise } from "../money";
import type { Outlet } from "../types";
import { Button, Card, ErrorText, Field, Input, Select, Toggle, useAction } from "../ui";
import { CountTab, fmtQty, parseQty, type StockItem, type Unit } from "./Inventory";

// wise-plus: Stock > Transfers, Spot check, Menu profit, Trends (from the Shakewallah stock app)

interface XLine { stock_item_id: string; name: string; unit: Unit; sent_m: number; received_m?: number; short_m?: number; value_paise?: number; short_paise?: number }
interface Xfer { id: string; from: string; to: string; date: string; status: "sent" | "received" | "cancelled"; note: string | null; incoming: boolean; lines: XLine[]; value_paise?: number; short_paise?: number }

export function TransfersTab({ outletId, outlets, items, costs, onChange }: { outletId: string; outlets: Outlet[]; items: StockItem[]; costs: boolean; onChange: () => void }) {
  const [list, setList] = useState<Xfer[] | null>(null);
  const [to, setTo] = useState("");
  const [lines, setLines] = useState<{ id: string; qty: string }[]>([{ id: "", qty: "" }]);
  const [note, setNote] = useState("");
  const [open, setOpen] = useState<Xfer | null>(null);
  const [got, setGot] = useState<Record<string, string>>({});
  const { busy, error, run } = useAction();
  const load = () => api<Xfer[]>(`/inventory/transfers?outlet_id=${outletId}`).then(setList);
  useEffect(() => { load(); setTo(""); }, [outletId]); // eslint-disable-line react-hooks/exhaustive-deps
  const others = outlets.filter((o) => o.id !== outletId);
  const stockable = items.filter((i) => i.is_active && !i.made_on_sale);
  const byId = new Map(items.map((i) => [i.id, i]));
  const ready = lines.filter((l) => l.id && parseQty(l.qty));
  const send = () => run(async () => {
    await api("/inventory/transfers", { body: { from_outlet_id: outletId, to_outlet_id: to, note: note || null,
      lines: ready.map((l) => ({ stock_item_id: l.id, qty_m: parseQty(l.qty)! })) } });
    setLines([{ id: "", qty: "" }]); setNote(""); load(); onChange();
  });
  const receive = (t: Xfer) => run(async () => {
    const body = { lines: t.lines.map((l) => ({ stock_item_id: l.stock_item_id, received_m: parseQty(got[l.stock_item_id] ?? "") ?? l.sent_m })) };
    await api(`/inventory/transfers/${t.id}/receive`, { body }); setOpen(null); load(); onChange();
  });
  const cancel = (t: Xfer) => run(async () => { await api(`/inventory/transfers/${t.id}/cancel`, { method: "POST" }); setOpen(null); load(); onChange(); });

  if (open) {
    const canReceive = open.status === "sent" && open.incoming;
    return (
      <Card title={`${open.from} → ${open.to}`} actions={<button className="link" onClick={() => setOpen(null)}>All transfers</button>}>
        <p className="muted small">{open.date} · {open.status === "sent" ? "On the way" : open.status === "received" ? "Received" : "Cancelled"}{open.note ? ` · ${open.note}` : ""}</p>
        {canReceive && <p className="small">Check what arrived. Change a number only if less came.</p>}
        <table className="table">
          <thead><tr><th>Item</th><th className="num">Sent</th><th className="num">{canReceive ? "Arrived" : "Received"}</th>{costs && <th className="num">Value</th>}</tr></thead>
          <tbody>{open.lines.map((l) => (
            <tr key={l.stock_item_id} className={l.short_m ? "row-warn" : ""}>
              <td>{l.name}</td><td className="num">{fmtQty(l.sent_m, l.unit)}</td>
              <td className="num">{canReceive
                ? <input className="input num" inputMode="decimal" placeholder={String(l.sent_m / 1000)} value={got[l.stock_item_id] ?? ""} onChange={(e) => setGot((g) => ({ ...g, [l.stock_item_id]: e.target.value }))} aria-label={`${l.name} arrived, ${l.unit}`} />
                : l.received_m !== undefined ? fmtQty(l.received_m, l.unit) + (l.short_m ? ` (${fmtQty(l.short_m, l.unit)} short)` : "") : "–"}</td>
              {costs && <td className="num">{formatPaise(l.value_paise || 0)}</td>}
            </tr>))}</tbody>
        </table>
        {costs && open.short_paise ? <p className="error">Short on arrival: {formatPaise(open.short_paise)}</p> : null}
        <ErrorText error={error} />
        <div className="row">
          {canReceive && <Button disabled={busy} onClick={() => receive(open)}>Received</Button>}
          {open.status === "sent" && !open.incoming && <Button variant="ghost" disabled={busy} onClick={() => cancel(open)}>Cancel transfer</Button>}
        </div>
      </Card>
    );
  }
  return (
    <>
      <Card title="Send stock to another outlet">
        {others.length === 0 ? <p className="muted">Add another outlet first (for example your central kitchen and a shop).</p> : <>
          <Field label="Send to"><Select value={to} onChange={(e) => setTo(e.target.value)}><option value="">Choose outlet</option>{others.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select></Field>
          {lines.map((l, i) => (
            <div key={i} className="row nowrap">
              <Select value={l.id} onChange={(e) => setLines((ls) => ls.map((x, j) => (j === i ? { ...x, id: e.target.value } : x)))} aria-label="Item">
                <option value="">Item</option>{stockable.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </Select>
              <input className="input num" inputMode="decimal" placeholder="Qty" value={l.qty} onChange={(e) => setLines((ls) => ls.map((x, j) => (j === i ? { ...x, qty: e.target.value } : x)))} aria-label="Quantity" />
              <span className="unit">{byId.get(l.id)?.unit || ""}</span>
            </div>))}
          <button className="link" onClick={() => setLines((ls) => [...ls, { id: "", qty: "" }])}>+ Add an item</button>
          <Field label="Note (optional)"><Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Morning run" /></Field>
          <ErrorText error={error} />
          <Button disabled={busy || !to || !ready.length} onClick={send}>Send {ready.length || ""} item{ready.length === 1 ? "" : "s"}</Button>
          <p className="muted small">It leaves this outlet's stock now and reaches the other outlet when they tap Received.</p>
        </>}
      </Card>
      <Card title="Transfers">
        {!list ? <p className="muted">Loading…</p> : list.length === 0 ? <p className="muted">No transfers yet.</p> : (
          <table className="table">
            <thead><tr><th>Date</th><th>From → To</th><th>Status</th>{costs && <th className="num">Value</th>}</tr></thead>
            <tbody>{list.map((t) => (
              <tr key={t.id} className="clickable" onClick={() => { setGot({}); setOpen(t); }}>
                <td>{t.date}</td><td><button className="link">{t.from} → {t.to}</button></td>
                <td>{t.status === "sent" ? (t.incoming ? <span className="badge warn-badge">To receive</span> : "On the way") : t.status === "received" ? (t.short_paise ? "Received, short" : "Received") : "Cancelled"}</td>
                {costs && <td className="num">{formatPaise(t.value_paise || 0)}</td>}
              </tr>))}</tbody>
          </table>)}
      </Card>
    </>
  );
}

interface Spot { id: string; date: string; at: string | null; by: string; off: number; short_paise?: number; lines: { name: string; unit: Unit; expected_m: number; counted_m: number; diff_m: number; value_paise?: number }[] }

export function SpotTab({ outletId, items, costs, manage, onSaved }: { outletId: string; items: StockItem[]; costs: boolean; manage: boolean; onSaved: () => void }) {
  const [pick, setPick] = useState<StockItem[] | null>(null);
  const [past, setPast] = useState<Spot[]>([]);
  const [n, setN] = useState("5");
  const { busy, error, run } = useAction();
  const loadPast = () => { if (manage) api<Spot[]>(`/inventory/spot-checks?outlet_id=${outletId}`).then(setPast); };
  useEffect(loadPast, [outletId, manage]); // eslint-disable-line react-hooks/exhaustive-deps
  const start = () => run(async () => {
    const r = await api<{ items: { id: string }[] }>(`/inventory/spot-check?outlet_id=${outletId}&n=${Number(n) || 5}`);
    const byId = new Map(items.map((i) => [i.id, i]));
    setPick(r.items.map((x) => byId.get(x.id)!).filter(Boolean));
  });
  if (pick) return <CountTab outletId={outletId} items={pick} costs={costs} kind="spot" title="Spot check" onSaved={() => { onSaved(); loadPast(); }} onDone={() => setPick(null)} />;
  return (
    <>
      <Card title="Surprise spot check">
        <p className="muted small">Picks a few items at random to count right now. Costly and fast-moving items come up more often, and nobody knows beforehand which ones. Hand the phone to the staff on duty.</p>
        <div className="row nowrap">
          <Field label="How many items"><Select value={n} onChange={(e) => setN(e.target.value)}>{["3", "5", "8", "10"].map((x) => <option key={x}>{x}</option>)}</Select></Field>
        </div>
        <ErrorText error={error} />
        <Button disabled={busy} onClick={start}>Start spot check</Button>
      </Card>
      {manage && (
        <Card title="Past spot checks">
          {past.length === 0 ? <p className="muted">None yet.</p> : past.map((sp) => (
            <details key={sp.id} className="spot">
              <summary><strong>{sp.date}</strong> · {sp.by} · {sp.off ? <span className="error">{sp.off} item{sp.off > 1 ? "s" : ""} off{costs && sp.short_paise ? `, ${formatPaise(sp.short_paise)} short` : ""}</span> : <span className="ok">all matched</span>}</summary>
              <table className="table"><thead><tr><th>Item</th><th className="num">Expected</th><th className="num">Found</th><th className="num">Difference</th></tr></thead>
                <tbody>{sp.lines.map((l) => <tr key={l.name} className={l.diff_m < 0 ? "row-warn" : ""}><td>{l.name}</td><td className="num">{fmtQty(l.expected_m, l.unit)}</td><td className="num">{fmtQty(l.counted_m, l.unit)}</td><td className="num">{l.diff_m ? fmtQty(l.diff_m, l.unit) : "–"}</td></tr>)}</tbody></table>
            </details>))}
        </Card>
      )}
    </>
  );
}

interface ProfitRow { variant_id: string; item: string; variant: string; qty: number; sales_paise: number; unit_cost_paise: number | null; cost_paise: number | null; profit_paise: number | null; margin_bp: number | null }
const iso = (d: Date) => d.toISOString().slice(0, 10);

export function ProfitTab({ outletId }: { outletId: string }) {
  const today = new Date();
  const [d1, setD1] = useState(iso(new Date(today.getTime() - 29 * 864e5)));
  const [d2, setD2] = useState(iso(today));
  const [data, setData] = useState<{ items: ProfitRow[]; total: { qty: number; sales_paise: number; cost_paise: number; profit_paise: number; no_recipe_sales_paise: number } } | null>(null);
  const [sort, setSort] = useState<"profit" | "qty" | "margin">("profit");
  useEffect(() => { setData(null); api<typeof data>(`/inventory/menu-profit?outlet_id=${outletId}&d1=${d1}&d2=${d2}`).then(setData); }, [outletId, d1, d2]);
  const rows = [...(data?.items || [])].sort((a, b) => sort === "qty" ? b.qty - a.qty : sort === "margin" ? (a.margin_bp ?? 1e9) - (b.margin_bp ?? 1e9) : (b.profit_paise ?? -1e12) - (a.profit_paise ?? -1e12));
  const t = data?.total;
  return (
    <Card title="Menu profit">
      <div className="row nowrap">
        <Field label="From"><Input type="date" value={d1} onChange={(e) => setD1(e.target.value)} /></Field>
        <Field label="To"><Input type="date" value={d2} onChange={(e) => setD2(e.target.value)} /></Field>
        <Field label="Sort by"><Select value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}><option value="profit">Most profit</option><option value="qty">Most sold</option><option value="margin">Lowest margin</option></Select></Field>
      </div>
      {!data ? <p className="muted">Loading…</p> : <>
        {t && <div className="kpis">
          <div><span>Sales (before GST)</span><strong>{formatPaise(t.sales_paise)}</strong></div>
          <div><span>Recipe cost</span><strong>{formatPaise(t.cost_paise)}</strong></div>
          <div><span>Profit</span><strong>{formatPaise(t.profit_paise)}</strong></div>
        </div>}
        {t?.no_recipe_sales_paise ? <p className="small warn-text">{formatPaise(t.no_recipe_sales_paise)} of sales are items without a recipe, so their cost isn't counted.</p> : null}
        <table className="table">
          <thead><tr><th>Item</th><th className="num">Sold</th><th className="num">Sales</th><th className="num">Cost each</th><th className="num">Profit</th><th className="num">Margin</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.variant_id} className={r.margin_bp !== null && r.margin_bp < 5000 ? "row-warn" : ""}>
              <td>{r.item} <span className="muted small">{r.variant}</span></td><td className="num">{r.qty}</td><td className="num">{formatPaise(r.sales_paise)}</td>
              <td className="num">{r.unit_cost_paise === null ? <span className="muted small">no recipe</span> : formatPaise(r.unit_cost_paise)}</td>
              <td className="num">{r.profit_paise === null ? "–" : formatPaise(r.profit_paise)}</td>
              <td className="num">{r.margin_bp === null ? "–" : `${Math.round(r.margin_bp / 100)}%`}</td>
            </tr>))}
            {rows.length === 0 && <tr><td colSpan={6} className="muted">Nothing sold in these dates.</td></tr>}</tbody>
        </table>
        <p className="muted small">Cost uses today's ingredient costs and each size's recipe (add-ons not included).</p>
      </>}
    </Card>
  );
}

interface Day { date: string; sales_paise: number; cost_paise: number; food_cost_bp: number | null; waste_paise: number; count_diff_paise: number; purchases_paise: number }
interface Mover { stock_item_id: string; name: string; this_week_paise: number; last_week_paise: number; change_bp: number | null }

export function TrendsTab({ outletId }: { outletId: string }) {
  const [days, setDays] = useState("30");
  const [data, setData] = useState<{ days: Day[]; movers: Mover[] } | null>(null);
  useEffect(() => { setData(null); api<typeof data>(`/inventory/trends?outlet_id=${outletId}&days=${days}`).then(setData); }, [outletId, days]);
  if (!data) return <p className="muted">Loading…</p>;
  const max = Math.max(1, ...data.days.map((d) => d.sales_paise));
  const sum = (k: keyof Day) => data.days.reduce((a, d) => a + (Number(d[k]) || 0), 0);
  const sales = sum("sales_paise"), cost = sum("cost_paise");
  const fc = (bp: number | null) => (bp === null ? "–" : `${Math.round(bp / 100)}%`);
  const w = 100 / data.days.length;
  return (
    <>
      <Card title="Trends" actions={<Select value={days} onChange={(e) => setDays(e.target.value)} aria-label="Period">{[["7", "7 days"], ["30", "30 days"], ["90", "90 days"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select>}>
        <div className="kpis">
          <div><span>Sales</span><strong>{formatPaise(sales)}</strong></div>
          <div><span>Food cost</span><strong>{sales && cost ? fc(Math.round(cost * 10000 / sales)) : "–"}</strong></div>
          <div><span>Wastage</span><strong>{formatPaise(sum("waste_paise"))}</strong></div>
          <div><span>Count short</span><strong>{formatPaise(-Math.min(0, sum("count_diff_paise")))}</strong></div>
        </div>
        <svg className="trend-bars" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label="Sales per day">
          {data.days.map((d, i) => { const hgt = (d.sales_paise / max) * 38; return <rect key={d.date} x={i * w + w * 0.15} width={w * 0.7} y={40 - hgt} height={hgt} rx={0.4}><title>{`${d.date}: ${formatPaise(d.sales_paise)}, food cost ${fc(d.food_cost_bp)}`}</title></rect>; })}
        </svg>
        <div className="trend-axis small muted"><span>{data.days[0]?.date}</span><span>{data.days[data.days.length - 1]?.date}</span></div>
        <details><summary className="small">Day by day</summary>
          <table className="table"><thead><tr><th>Date</th><th className="num">Sales</th><th className="num">Food cost</th><th className="num">Waste</th><th className="num">Count diff</th><th className="num">Bought</th></tr></thead>
            <tbody>{[...data.days].reverse().map((d) => <tr key={d.date}><td>{d.date}</td><td className="num">{formatPaise(d.sales_paise)}</td><td className="num">{fc(d.food_cost_bp)}</td><td className="num">{d.waste_paise ? formatPaise(d.waste_paise) : "–"}</td><td className={`num ${d.count_diff_paise < 0 ? "error" : ""}`}>{d.count_diff_paise ? formatPaise(d.count_diff_paise) : "–"}</td><td className="num">{d.purchases_paise ? formatPaise(d.purchases_paise) : "–"}</td></tr>)}</tbody></table>
        </details>
      </Card>
      <Card title="Biggest changes in use (this week vs last)">
        {data.movers.length === 0 ? <p className="muted">Not enough history yet.</p> : (
          <table className="table"><thead><tr><th>Item</th><th className="num">Last week</th><th className="num">This week</th><th className="num">Change</th></tr></thead>
            <tbody>{data.movers.map((m) => <tr key={m.stock_item_id}><td>{m.name}</td><td className="num">{formatPaise(m.last_week_paise)}</td><td className="num">{formatPaise(m.this_week_paise)}</td>
              <td className={`num ${m.change_bp !== null && m.change_bp > 2000 ? "warn-text" : ""}`}>{m.change_bp === null ? "new" : `${m.change_bp > 0 ? "+" : ""}${Math.round(m.change_bp / 100)}%`}</td></tr>)}</tbody></table>)}
        <p className="muted small">Use at cost: sales, prep, wastage and transfers out. A big jump with flat sales is worth a look.</p>
      </Card>
    </>
  );
}

/** Day close: get every outlet's closing summary on WhatsApp each night. */
export function NightlyToggle() {
  const [st, setSt] = useState<{ nightly: boolean; phone: string | null } | null>(null);
  const { error, run } = useAction();
  useEffect(() => { api<typeof st>("/whatsapp-alerts/me").then(setSt).catch(() => setSt(null)); }, []);
  if (!st) return null;
  return (
    <Card title="Nightly summary on WhatsApp">
      <p className="muted small">Sales, food cost, wastage, low stock and tomorrow's order for every outlet, sent to {st.phone ? `+91 ${st.phone.slice(-10)}` : "your mobile"} at 11:30 pm. You can also type <strong>summary</strong> to the WhatsApp number any time.</p>
      <Toggle checked={st.nightly} onChange={(v) => run(async () => setSt(await api<typeof st>("/whatsapp-alerts/me", { method: "PUT", body: { nightly: v } })))} label="Send me the nightly summary" />
      <ErrorText error={error} />
    </Card>
  );
}
