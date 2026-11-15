import { useCallback, useEffect, useState } from "react";
import { BellRing, CheckCircle2, Droplets, ReceiptText, UtensilsCrossed } from "lucide-react";
import { api } from "../api";
import { formatPaise } from "../money";
import SelfOrder from "./SelfOrder";
import { PoweredBy } from "../components/Brand";

interface Req { id: string; kind: "water" | "waiter" | "bill"; status: "open" | "done"; since: string; times_asked: number; note: string | null }
interface Page {
  outlet_name: string; app_name: string; logo_url: string | null; primary_color: string | null; table: string; ordering: boolean; upi: boolean;
  tab: { items: { name: string; qty: number }[]; total_paise: number } | null;
  requests: Partial<Record<Req["kind"], Req>>;
  bill: { status: string; paid?: boolean; total_paise?: number; upi_link?: string | null; bill_url?: string; invoice_no?: string; note?: string | null } | null;
}

const DEVICE_KEY = "wiseplus.device";
const device = () => {
  let d = localStorage.getItem(DEVICE_KEY);
  if (!d) { d = Math.random().toString(36).slice(2) + Date.now().toString(36); localStorage.setItem(DEVICE_KEY, d); }
  return d;
};
const ago = (iso: string) => { const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000)); return s < 60 ? "just now" : `${Math.floor(s / 60)} min ago`; };

/** The table QR: Order food · Water · Call waiter · Ask for bill. Falls back to plain QR ordering
 *  when the outlet hasn't switched table service on. */
export default function TableService({ outletKey, table }: { outletKey: string; table: string }) {
  const [p, setP] = useState<Page | null>(null);
  const [off, setOff] = useState(false);
  const [ordering, setOrdering] = useState(false);
  const [billOpen, setBillOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api<Page>(`/public/table/${outletKey}?t=${encodeURIComponent(table)}`).then(setP).catch(() => setOff(true)), [outletKey, table]);
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);
  useEffect(() => { if (p?.primary_color) document.documentElement.style.setProperty("--primary", p.primary_color); }, [p?.primary_color]);
  if (off || ordering) return <SelfOrder mode="qr" outletKey={outletKey} />;
  if (!p) return <div className="splash">Loading…</div>;

  const ask = async (kind: Req["kind"], note?: string) => {
    setBusy(kind); setMsg(null);
    try {
      await api(`/public/table/${outletKey}/requests`, { body: { table: p.table, kind, device: device(), ...(note ? { note } : {}) } });
      if (navigator.vibrate) navigator.vibrate(40);
      await load();
    } catch (e) { setMsg(e instanceof Error ? e.message : "Couldn't send. Try again."); }
    finally { setBusy(null); }
  };
  const tile = (kind: Req["kind"], label: string, Icon: typeof Droplets, onClick: () => void) => {
    const r = p.requests[kind];
    const waiting = r?.status === "open";
    return (
      <button className={`ts-tile ${waiting ? "waiting" : ""}`} disabled={busy === kind} onClick={onClick}>
        <Icon size={34} aria-hidden />
        <strong>{label}</strong>
        <small>{waiting ? `Asked ${ago(r!.since)} · tap to remind` : r?.status === "done" && kind !== "bill" ? "Done ✓ · tap to ask again" : " "}</small>
      </button>
    );
  };
  const b = p.bill;
  return (
    <div className="ts">
      <header className="ts-head">
        {p.logo_url && <img src={p.logo_url} alt="" />}
        <div><strong>{p.app_name}</strong><span>{p.outlet_name} · Table {p.table}</span></div>
      </header>
      <div className="ts-grid">
        {p.ordering && (
          <button className="ts-tile order" onClick={() => setOrdering(true)}><UtensilsCrossed size={34} aria-hidden /><strong>Order food</strong><small>See the menu</small></button>
        )}
        {tile("water", "Water", Droplets, () => ask("water"))}
        {tile("waiter", "Call waiter", BellRing, () => ask("waiter"))}
        {tile("bill", "Ask for bill", ReceiptText, () => { setBillOpen(true); if (!b || b.paid) ask("bill"); })}
      </div>
      {msg && <p className="ts-msg">{msg}</p>}

      {(billOpen || (b && b.status === "open")) && b && (
        <section className="ts-card">
          {b.paid ? (<>
            <h2><CheckCircle2 size={22} aria-hidden /> Paid · thank you!</h2>
            <p>Bill {b.invoice_no} · {formatPaise(b.total_paise || 0)}</p>
            {b.bill_url && <a className="ts-btn" href={b.bill_url}>View your bill</a>}
          </>) : (<>
            <h2>Your bill · {formatPaise(b.total_paise || 0)}</h2>
            <p className="ts-sub">We've told the counter. {b.note === "paid_upi" ? "They'll check your UPI payment." : b.note === "pay_counter" ? "Please pay at the counter." : "How would you like to pay?"}</p>
            {b.upi_link && b.note !== "paid_upi" && <a className="ts-btn" href={b.upi_link}>Pay {formatPaise(b.total_paise || 0)} by UPI</a>}
            {b.upi_link && b.note !== "paid_upi" && <button className="ts-btn ghost" onClick={() => ask("bill", "paid_upi")}>I've paid by UPI</button>}
            {b.note !== "pay_counter" && b.note !== "paid_upi" && <button className="ts-btn ghost" onClick={() => ask("bill", "pay_counter")}>I'll pay at the counter / by card</button>}
          </>)}
        </section>
      )}

      {p.tab && (
        <section className="ts-card">
          <h3>On your table</h3>
          <ul>{p.tab.items.map((it) => <li key={it.name}><span>{it.qty} × {it.name}</span></li>)}</ul>
          <p className="ts-total">Total so far <strong>{formatPaise(p.tab.total_paise)}</strong></p>
        </section>
      )}
      <p className="ts-foot">Tap a button and the staff get it at once.</p>
      <PoweredBy />
    </div>
  );
}
