import { Fragment, useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

interface Lead { id: string; name: string; phone: string; restaurant: string | null; city: string | null; outlets: number | null; uses_now: string | null;
  source: string | null; campaign: string | null; ref_code: string | null; consent: boolean; status: string; demo_at: string | null; notes: string | null;
  step: number; next_at: string | null; stopped: boolean; tenant_id: string | null; created_at: string }
interface Res { leads: Lead[]; funnel: Record<string, number>; sources: Record<string, number>; referrals: Record<string, number> }
const STATES = ["new", "talking", "demo", "trial", "paid", "lost"];
const LABEL: Record<string, string> = { new: "New", talking: "Talking", demo: "Demo", trial: "On trial", paid: "Paid", lost: "Lost" };
const when = (iso: string) => new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

/** Yours only: everyone who asked about Ease My Bill, where they came from, and how far they've got. */
export default function Leads() {
  const [r, setR] = useState<Res | null>(null);
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const act = useAction();
  const load = useCallback(() => {
    const p = new URLSearchParams({ ...(state ? { state } : {}), ...(q ? { q } : {}) });
    api<Res>(`/platform/leads?${p}`).then(setR).catch(() => setR(null));
  }, [state, q]);
  useEffect(() => { load(); }, [load]);
  const edit = (id: string, body: object) => act.run(async () => { await api(`/platform/leads/${id}`, { method: "PATCH", body }); load(); });
  if (!r) return <p className="muted">Loading…</p>;
  const total = Object.values(r.funnel).reduce((a, b) => a + b, 0);
  const pct = (n: number) => (total ? `${Math.round((n * 100) / total)}%` : "");
  return (
    <div className="stack">
      <h1>Leads</h1>
      <p className="muted">Everyone who asked about Ease My Bill: from the website, ads, referrals, or added by you. Follow-ups on WhatsApp go out by themselves
        (only to people who agreed). You get a WhatsApp for every new lead and before every demo.</p>
      <div className="chips">
        <button className={`chip${!state ? " on" : ""}`} onClick={() => setState("")}>All {total}</button>
        {STATES.map((s) => <button key={s} className={`chip${state === s ? " on" : ""}`} onClick={() => setState(state === s ? "" : s)}>
          {LABEL[s]} {r.funnel[s] || 0} <small className="muted">{pct(r.funnel[s] || 0)}</small></button>)}
      </div>
      <p className="muted small">From: {Object.entries(r.sources).map(([k, v]) => `${k} ${v}`).join(" · ") || "nothing yet"}
        {Object.keys(r.referrals).length ? ` · Referrals: ${r.referrals.signed || 0} signed up, ${r.referrals.rewarded || 0} paid` : ""}</p>
      <div className="row wrap">
        <Input placeholder="Search name, restaurant, phone" value={q} onChange={(e) => setQ(e.target.value)} />
        <Button variant="ghost" onClick={() => setAdding(!adding)}>+ Add a lead</Button>
        <a className="link" href="/start" target="_blank" rel="noreferrer">Open the website ↗</a>
      </div>
      {adding && <AddLead onDone={() => { setAdding(false); load(); }} />}
      <ErrorText error={act.error} />
      {r.leads.length === 0 ? <Card title="No leads yet"><p className="muted">Share <strong>{location.origin}/start</strong>, or run the ads. Leads appear here.</p></Card> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Who</th><th>Restaurant</th><th>From</th><th>Status</th><th>Next</th></tr></thead>
          <tbody>{r.leads.map((l) => (<Fragment key={l.id}>
            <tr onClick={() => setOpen(open === l.id ? null : l.id)} style={{ cursor: "pointer" }}>
              <td><strong>{l.name}</strong><div className="small"><a href={`https://wa.me/91${l.phone}`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>+91 {l.phone}</a></div></td>
              <td>{l.restaurant || "—"}<div className="small muted">{[l.city, l.outlets ? `${l.outlets} outlet${l.outlets === 1 ? "" : "s"}` : null, l.uses_now].filter(Boolean).join(" · ")}</div></td>
              <td className="small">{l.source || "—"}{l.campaign ? <div className="muted">{l.campaign}</div> : null}{l.ref_code ? <div className="muted">ref {l.ref_code}</div> : null}<div className="muted">{when(l.created_at)}</div></td>
              <td onClick={(e) => e.stopPropagation()}><Select value={l.status} onChange={(e) => edit(l.id, { status: e.target.value })} aria-label="Status">
                {STATES.map((s) => <option key={s} value={s}>{LABEL[s]}</option>)}</Select></td>
              <td className="small">{l.demo_at ? <div>📅 {when(l.demo_at)}</div> : null}
                {l.stopped ? <span className="muted">stopped messages</span> : !l.consent ? <span className="muted">no WhatsApp OK</span>
                  : l.next_at ? <span className="muted">follow-up {l.step + 1} on {when(l.next_at)}</span> : <span className="muted">follow-ups done</span>}</td>
            </tr>
            {open === l.id && <tr><td colSpan={5}><LeadNotes lead={l} onSave={(b) => edit(l.id, b)} /></td></tr>}
          </Fragment>))}</tbody>
        </table></div>)}
    </div>
  );
}

function LeadNotes({ lead, onSave }: { lead: Lead; onSave: (b: object) => void }) {
  const [notes, setNotes] = useState(lead.notes || "");
  const [demo, setDemo] = useState(lead.demo_at ? lead.demo_at.slice(0, 16) : "");
  return (
    <div className="sub-form">
      <Field label="Notes"><textarea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} style={{ width: "100%" }} /></Field>
      <div className="row wrap">
        <Field label="Demo"><Input type="datetime-local" value={demo} onChange={(e) => setDemo(e.target.value)} /></Field>
        <Button onClick={() => onSave({ notes, ...(demo ? { demo_at: new Date(demo).toISOString() } : {}) })}>Save</Button>
        {!lead.stopped && <Button variant="ghost" onClick={() => onSave({ stop: true })}>Stop follow-ups</Button>}
      </div>
    </div>
  );
}

function AddLead({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState({ name: "", phone: "", restaurant: "", city: "Jodhpur", outlets: "1", source: "walk-in", consent: false });
  const act = useAction();
  return (
    <Card title="Add a lead">
      <div className="row wrap">
        <Field label="Name"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Mobile"><Input inputMode="tel" value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} /></Field>
        <Field label="Restaurant"><Input value={f.restaurant} onChange={(e) => setF({ ...f, restaurant: e.target.value })} /></Field>
        <Field label="City"><Input value={f.city} onChange={(e) => setF({ ...f, city: e.target.value })} /></Field>
        <Field label="Outlets"><Input inputMode="numeric" value={f.outlets} onChange={(e) => setF({ ...f, outlets: e.target.value })} /></Field>
        <Field label="Where from"><Select value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })}>
          {["walk-in", "call", "event", "friend", "instagram", "other"].map((x) => <option key={x}>{x}</option>)}</Select></Field>
      </div>
      <label className="check"><input type="checkbox" checked={f.consent} onChange={(e) => setF({ ...f, consent: e.target.checked })} /> They said yes to WhatsApp messages (starts the follow-ups)</label>
      <ErrorText error={act.error} />
      <Button disabled={act.busy || f.name.length < 2 || f.phone.length < 10} onClick={() => act.run(async () => {
        await api("/platform/leads", { body: { ...f, outlets: Number(f.outlets) || null } }); onDone();
      })}>Add</Button>
    </Card>
  );
}
