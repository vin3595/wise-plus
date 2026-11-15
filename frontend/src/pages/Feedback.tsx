import { useEffect, useState } from "react";
import { api } from "../api";
import { formatPaise } from "../money";
import { Card, Select } from "../ui";

interface Item { id: string; rating: 1 | 3 | 5; comment: string | null; phone: string; at: string; invoice_no: string; total_paise: number; date: string; outlet: string }
interface Data { days: number; counts: { loved: number; okay: number; bad: number }; total: number; happy_pct: number | null; items: Item[] }
const FACE: Record<number, string> = { 5: "😍", 3: "🙂", 1: "😞" };

/** Customers > Feedback: ratings from the buttons under the WhatsApp bill. */
export default function Feedback() {
  const [days, setDays] = useState("30");
  const [only, setOnly] = useState("");
  const [d, setD] = useState<Data | null>(null);
  useEffect(() => { setD(null); api<Data>(`/feedback?days=${days}${only ? `&rating=${only}` : ""}`).then(setD).catch(() => setD(null)); }, [days, only]);
  return (
    <div className="stack">
      <h1>Feedback</h1>
      <Card actions={<Select value={days} onChange={(e) => setDays(e.target.value)} aria-label="Period">{[["7", "7 days"], ["30", "30 days"], ["90", "90 days"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select>}>
        {!d ? <p className="muted">Loading…</p> : d.total === 0 ? (
          <p className="muted">No ratings yet. Switch on <strong>Send the bill on WhatsApp</strong> for an outlet (Setup › Outlets); customers who give their number get the bill with three buttons.</p>
        ) : (
          <div className="kpis">
            <div><span>Happy</span><strong>{d.happy_pct}%</strong></div>
            <div><span>😍 Loved it</span><strong>{d.counts.loved}</strong></div>
            <div><span>🙂 Okay</span><strong>{d.counts.okay}</strong></div>
            <div><span>😞 Not good</span><strong>{d.counts.bad}</strong></div>
          </div>)}
      </Card>
      {d && d.total > 0 && (
        <Card title="Ratings" actions={<Select value={only} onChange={(e) => setOnly(e.target.value)} aria-label="Show">
          <option value="">All</option><option value="1">😞 Not good</option><option value="3">🙂 Okay</option><option value="5">😍 Loved it</option></Select>}>
          <ul className="fb-list">
            {d.items.map((it) => (
              <li key={it.id} className={it.rating === 1 ? "bad" : ""}>
                <span className="fb-face" aria-label={`rating ${it.rating}`}>{FACE[it.rating]}</span>
                <div>
                  <div className="small muted">{new Date(it.at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })} · {it.outlet} · bill {it.invoice_no} · {formatPaise(it.total_paise)} · {it.phone}</div>
                  {it.comment ? <p>{it.comment}</p> : it.rating < 5 ? <p className="muted small">No reason given.</p> : null}
                </div>
              </li>))}
            {d.items.length === 0 && <li className="muted">None.</li>}
          </ul>
        </Card>)}
    </div>
  );
}
