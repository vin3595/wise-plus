import { useEffect, useState } from "react";
import { Send } from "lucide-react";
import { api } from "../api";
import { formatPaise, inputToPaise } from "../money";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

interface Filters { active_days?: number; away_days?: number; new_days?: number; min_visits?: number; max_visits?: number; min_spend?: number; max_spend?: number;
  min_avg?: number; max_avg?: number; top_pct?: number; bottom_pct?: number; offers?: "users" | "never"; outlet_ids?: string[] }
interface Preview { count: number; credits: number; have: number | null; credits_each: number;
  sample: { name: string; phone: string; visits: number; spend_paise: number; last_visit: string }[] }
interface Sent { id: string; name: string; text: string; recipients: number; credits: number; created_at: string; sent: number; waiting: number; failed: number; offer_codes: number }

const GROUPS: { key: string; label: string; f: Filters }[] = [
  { key: "all", label: "Everyone", f: {} },
  { key: "active", label: "Active (came in 30 days)", f: { active_days: 30 } },
  { key: "away", label: "Haven't come for 90+ days", f: { away_days: 90 } },
  { key: "high", label: "High spenders (top 20%)", f: { top_pct: 20 } },
  { key: "low", label: "Low spenders (bottom 50%)", f: { bottom_pct: 50 } },
  { key: "offers", label: "Offer lovers (used an offer)", f: { offers: "users" } },
  { key: "new", label: "New (first visit in 30 days)", f: { new_days: 30 } },
  { key: "regular", label: "Regulars (5+ visits)", f: { min_visits: 5 } },
];

/** Customers › Broadcast: a WhatsApp message to the customers you choose, with an offer they can claim in one tap. */
export default function Broadcast() {
  const [group, setGroup] = useState("away");
  const [f, setF] = useState<Filters>(GROUPS[2].f);
  const [more, setMore] = useState(false);
  const [pv, setPv] = useState<Preview | null>(null);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [offerId, setOfferId] = useState("");
  const [offers, setOffers] = useState<{ id: string; name: string; keyword: string; is_active: boolean }[]>([]);
  const [outlets, setOutlets] = useState<{ id: string; name: string }[]>([]);
  const [history, setHistory] = useState<Sent[]>([]);
  const [done, setDone] = useState<string | null>(null);
  const { run, busy, error } = useAction();
  const loadHist = () => api<Sent[]>("/broadcasts").then(setHistory).catch(() => setHistory([]));
  useEffect(() => {
    api<{ offers: { id: string; name: string; keyword: string; is_active: boolean }[] }>("/offers").then((r) => setOffers(r.offers.filter((o) => o.is_active))).catch(() => {});
    api<{ id: string; name: string }[]>("/tenant/outlets").then(setOutlets).catch(() => {});
    loadHist();
  }, []);
  useEffect(() => {
    setPv(null);
    const t = setTimeout(() => { api<Preview>("/broadcasts/preview", { body: f }).then(setPv).catch(() => setPv(null)); }, 250);
    return () => clearTimeout(t);
  }, [f]);
  const num = (k: keyof Filters, rupees = false) => (e: { target: { value: string } }) => {
    const v = e.target.value.trim();
    setGroup("custom");
    setF((x) => { const y = { ...x } as Record<string, unknown>; if (!v) delete y[k]; else y[k] = rupees ? inputToPaise(v) ?? undefined : Number(v); return y as Filters; });
  };
  const val = (k: keyof Filters, rupees = false) => (f[k] === undefined ? "" : rupees ? String((f[k] as number) / 100) : String(f[k]));
  const short = pv && pv.have !== null && pv.credits > pv.have;
  const send = () => {
    if (!pv || !confirm(`Send "${name}" to ${pv.count} customer${pv.count === 1 ? "" : "s"} on WhatsApp?${pv.have !== null ? ` It uses ${pv.credits.toLocaleString("en-IN")} message credits.` : ""}`)) return;
    run(async () => {
      const r = await api<{ recipients: number }>("/broadcasts", { body: { name: name.trim(), text, offer_id: offerId || null, filters: f } });
      setDone(`Sending to ${r.recipients} customers now. Results update below.`);
      setText(""); setName(""); loadHist();
    });
  };
  return (
    <div className="stack">
      <h1>WhatsApp broadcast</h1>
      <Card title="1. Who gets it">
        <div className="chips">
          {GROUPS.map((g) => <button key={g.key} className={`chip ${group === g.key ? "on" : ""}`} onClick={() => { setGroup(g.key); setF(g.f); }}>{g.label}</button>)}
        </div>
        <button className="link small" onClick={() => setMore(!more)}>{more ? "Hide filters" : "More filters (combine any)"}</button>
        {more && <div className="grid-2">
          <Field label="Came in the last … days"><Input type="number" min={1} value={val("active_days")} onChange={num("active_days")} /></Field>
          <Field label="Haven't come for … days or more"><Input type="number" min={1} value={val("away_days")} onChange={num("away_days")} /></Field>
          <Field label="First visit in the last … days"><Input type="number" min={1} value={val("new_days")} onChange={num("new_days")} /></Field>
          <Field label="Visits at least"><Input type="number" min={1} value={val("min_visits")} onChange={num("min_visits")} /></Field>
          <Field label="Spent in total at least ₹"><Input inputMode="decimal" value={val("min_spend", true)} onChange={num("min_spend", true)} /></Field>
          <Field label="Spent in total at most ₹"><Input inputMode="decimal" value={val("max_spend", true)} onChange={num("max_spend", true)} /></Field>
          <Field label="Average bill at least ₹"><Input inputMode="decimal" value={val("min_avg", true)} onChange={num("min_avg", true)} /></Field>
          <Field label="Average bill at most ₹"><Input inputMode="decimal" value={val("max_avg", true)} onChange={num("max_avg", true)} /></Field>
          <Field label="Offers">
            <Select value={f.offers || ""} onChange={(e) => { setGroup("custom"); setF((x) => ({ ...x, offers: (e.target.value || undefined) as Filters["offers"] })); }}>
              <option value="">Anyone</option><option value="users">Have used an offer</option><option value="never">Never used an offer</option></Select>
          </Field>
          {outlets.length > 1 && <Field label="Bought at">
            <Select value={f.outlet_ids?.[0] || ""} onChange={(e) => { setGroup("custom"); setF((x) => ({ ...x, outlet_ids: e.target.value ? [e.target.value] : undefined })); }}>
              <option value="">Any outlet</option>{outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select>
          </Field>}
        </div>}
        {!pv ? <p className="muted">Counting…</p> : (
          <>
            <p className="big-line"><strong>{pv.count}</strong> customer{pv.count === 1 ? "" : "s"}{pv.have !== null && <span className="muted"> · {pv.credits.toLocaleString("en-IN")} message credits ({pv.credits_each} each)</span>}</p>
            {pv.sample.length > 0 && <p className="muted small">{pv.sample.map((s) => `${s.name} ${s.phone} (${s.visits} visit${s.visits === 1 ? "" : "s"}, ${formatPaise(s.spend_paise)})`).join(" · ")}</p>}
            {short && <p className="warn-text">You have {pv.have?.toLocaleString("en-IN")} credits. Buy a message pack in Settings › Billing, or narrow the group.</p>}
          </>)}
        <p className="muted small">Only customers who gave their number on a bill, and never anyone who tapped "Stop offers".</p>
      </Card>

      <Card title="2. The message">
        <Field label="Name (for you)"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Come back offer, Nov" /></Field>
        <Field label="Message">
          <textarea className="input" rows={4} maxLength={700} value={text} onChange={(e) => setText(e.target.value)}
            placeholder="We miss you! Get a free topping on any shake this week." />
        </Field>
        <Field label="Attach an offer (adds a 'Get my offer' button)">
          <Select value={offerId} onChange={(e) => setOfferId(e.target.value)}>
            <option value="">No offer</option>{offers.map((o) => <option key={o.id} value={o.id}>{o.name} ({o.keyword})</option>)}</Select>
        </Field>
        <div className="wa-preview" aria-label="Preview">
          <p>Hi {pv?.sample[0]?.name?.split(" ")[0] || "Ravi"}, {text.trim().replace(/\s*\n+\s*/g, " · ") || "your message"}</p>
          <p className="muted small">— your business name</p>
          {offerId && <div className="wa-btn">Get my offer</div>}<div className="wa-btn">Stop offers</div>
        </div>
        <ErrorText error={error} />
        {done && <p className="ok-text">{done}</p>}
        <Button disabled={busy || !pv?.count || !!short || name.trim().length < 2 || text.trim().length < 5} onClick={send}><Send size={16} aria-hidden /> Send to {pv?.count ?? 0}</Button>
        <p className="muted small">WhatsApp shows new lines as " · ". Meta charges about ₹0.86 per marketing message; each one uses {pv?.credits_each ?? 6} message credits.</p>
      </Card>

      {history.length > 0 && (
        <Card title="Sent">
          <table className="table">
            <thead><tr><th>When</th><th>Name</th><th className="num">To</th><th className="num">Sent</th><th className="num">Offer codes</th></tr></thead>
            <tbody>{history.map((b) => (
              <tr key={b.id}><td>{new Date(b.created_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}</td>
                <td>{b.name}</td><td className="num">{b.recipients}</td>
                <td className="num">{b.sent}{b.waiting ? <span className="muted"> (+{b.waiting} going)</span> : null}{b.failed ? <span className="bad-text"> · {b.failed} failed</span> : null}</td>
                <td className="num">{b.offer_codes || "—"}</td></tr>))}
            </tbody>
          </table>
        </Card>)}
    </div>
  );
}
