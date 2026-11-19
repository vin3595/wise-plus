import { useEffect, useState } from "react";
import { api } from "../api";
import { Button, Card, ErrorText, Input } from "../ui";

type Row = { id: string; at: string; entity: string; action: string; who: string; role: string | null; user_id: string | null;
  what: string; changes: string[]; outlet: string | null; details: Record<string, unknown> | null };
type Resp = { rows: Row[]; people: { id: string; name: string; role: string }[]; more: boolean };

const AREAS: [string, string][] = [["", "Everything"], ["item", "Menu items and prices"], ["override", "Outlet prices"], ["bill", "Bills"],
  ["order", "Orders"], ["user", "Staff"], ["day", "Day close"], ["tenant", "Settings"]];

const when = (iso: string) => new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

/** Who changed what, and when: every change in the business, newest first. */
export default function Activity() {
  const [rows, setRows] = useState<Row[]>([]);
  const [people, setPeople] = useState<Resp["people"]>([]);
  const [more, setMore] = useState(false);
  const [f, setF] = useState({ entity: "", user_id: "", q: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const load = async (append = false) => {
    setBusy(true); setErr(null);
    const p = new URLSearchParams();
    if (f.entity) p.set("entity", f.entity);
    if (f.user_id) p.set("user_id", f.user_id);
    if (f.q.trim()) p.set("q", f.q.trim());
    if (append && rows.length) p.set("before", rows[rows.length - 1].at);
    try {
      const r = await api<Resp>(`/activity?${p}`);
      setRows(append ? [...rows, ...r.rows] : r.rows); setPeople(r.people); setMore(r.more);
    } catch (e) { setErr(e instanceof Error ? e.message : "Couldn't load the activity log"); }
    finally { setBusy(false); }
  };
  useEffect(() => { load(); }, [f.entity, f.user_id]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="stack">
      <h1>Activity log</h1>
      <p className="muted">Every change in your business: who made it, when, and what it was before and after. Nobody can edit or delete this log.</p>
      <Card>
        <div className="row wrap" style={{ gap: 8 }}>
          <select value={f.entity} onChange={(e) => setF({ ...f, entity: e.target.value })} aria-label="What">
            {AREAS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
          <select value={f.user_id} onChange={(e) => setF({ ...f, user_id: e.target.value })} aria-label="Who">
            <option value="">Everyone</option>
            {people.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.role})</option>)}
          </select>
          <form className="row" style={{ gap: 8 }} onSubmit={(e) => { e.preventDefault(); load(); }}>
            <Input value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} placeholder="Search: item, bill no…" aria-label="Search" />
            <Button variant="ghost" disabled={busy}>Search</Button>
          </form>
        </div>
      </Card>
      <ErrorText error={err} />
      {!rows.length && !busy && !err && <p className="muted">Nothing found.</p>}
      <div className="stack" style={{ gap: 8 }}>
        {rows.map((r) => (
          <Card key={r.id}>
            <div className="row" style={{ justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
              <strong>{r.what}</strong>
              <span className="muted small">{when(r.at)}</span>
            </div>
            <div className="small">by <strong>{r.who}</strong>{r.role ? ` · ${r.role}` : ""}{r.outlet ? ` · ${r.outlet}` : ""}</div>
            {r.changes.length > 0 && <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>{r.changes.map((c, i) => <li key={i}>{c}</li>)}</ul>}
            {r.details && <>
              <button className="link small" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? "Hide details" : "Details"}</button>
              {open === r.id && <pre className="small" style={{ whiteSpace: "pre-wrap", overflowX: "auto", maxHeight: 240 }}>{JSON.stringify(r.details, null, 2)}</pre>}
            </>}
          </Card>
        ))}
      </div>
      {more && <Button variant="ghost" disabled={busy} onClick={() => load(true)}>{busy ? "Loading…" : "Show older"}</Button>}
    </div>
  );
}
