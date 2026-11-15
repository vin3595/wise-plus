import { useCallback, useEffect, useRef, useState } from "react";
import { BellRing, Droplets, ReceiptText, Volume2, VolumeX } from "lucide-react";
import { api } from "../api";
import { subscribe } from "../live";
import { formatPaise } from "../money";

interface Req { id: string; table: string; kind: "water" | "waiter" | "bill"; label: string; status: string; times_asked: number;
  note: string | null; since: string; last_asked: string; waiting_secs: number; total_paise?: number | null; done_by?: string | null }
const ICON = { water: Droplets, waiter: BellRing, bill: ReceiptText };
const MUTE_KEY = "wiseplus.tablealerts.mute";

/** A chime the browser can play without any sound file. */
function chime(times = 2) {
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    for (let i = 0; i < times; i++) {
      [880, 1320].forEach((f, j) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f; o.type = "sine";
        const t = ctx.currentTime + i * 0.45 + j * 0.16;
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.35, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.3);
        o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + 0.32);
      });
    }
    setTimeout(() => ctx.close(), 1500);
  } catch { /* no sound available */ }
  if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
}

const mins = (s: number) => (s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`);

/** Water / waiter / bill requests from table QRs, live on the POS (floating) or full screen on a waiter's phone.
 *  New or repeated requests chime; anything waiting over 2 minutes chimes again every minute until someone taps Done. */
export default function TableAlerts({ outletId, full = false }: { outletId: string; full?: boolean }) {
  const [open, setOpen] = useState<Req[]>([]);
  const [done, setDone] = useState<Req[]>([]);
  const [mute, setMute] = useState(() => localStorage.getItem(MUTE_KEY) === "1");
  const [, tick] = useState(0);
  const seen = useRef<Map<string, number>>(new Map());
  const first = useRef(true);
  const load = useCallback(async () => {
    try {
      const r = await api<{ open: Req[]; done: Req[] }>(`/table-requests?outlet_id=${outletId}`);
      let fresh = false;
      for (const q of r.open) {
        const before = seen.current.get(q.id);
        if (before === undefined || q.times_asked > before) fresh = true;
        seen.current.set(q.id, q.times_asked);
      }
      if (fresh && !first.current && localStorage.getItem(MUTE_KEY) !== "1") chime(2);
      first.current = false;
      setOpen(r.open); setDone(r.done);
    } catch { /* not allowed or offline: try again next time */ }
  }, [outletId]);
  useEffect(() => {
    load();
    const stop = subscribe({ outletId }, (topics) => { if (topics.includes("requests")) load(); });
    const poll = setInterval(load, 20000);
    const clock = setInterval(() => tick((n) => n + 1), 1000);
    return () => { stop(); clearInterval(poll); clearInterval(clock); };
  }, [outletId, load]);
  // Nobody answered for 2 minutes: remind every minute.
  useEffect(() => {
    const t = setInterval(() => {
      if (localStorage.getItem(MUTE_KEY) === "1") return;
      if (open.some((q) => Date.now() - new Date(q.since).getTime() > 120000)) chime(1);
    }, 60000);
    return () => clearInterval(t);
  }, [open]);
  const markDone = async (q: Req) => {
    setOpen((o) => o.filter((x) => x.id !== q.id));
    await api(`/table-requests/${q.id}/done`, { method: "POST" }).catch(() => {});
    load();
  };
  const toggleMute = () => { const m = !mute; setMute(m); localStorage.setItem(MUTE_KEY, m ? "1" : "0"); if (!m) chime(1); };
  const wait = (q: Req) => Math.max(0, Math.round((Date.now() - new Date(q.since).getTime()) / 1000));

  if (!full && open.length === 0) return null;
  return (
    <div className={full ? "ta ta-full" : "ta ta-float"} role="region" aria-label="Table requests" aria-live="polite">
      <div className="ta-head">
        <strong>{open.length ? `${open.length} table request${open.length > 1 ? "s" : ""}` : "No requests right now"}</strong>
        <button className="ta-mute" onClick={toggleMute} aria-label={mute ? "Turn sound on" : "Turn sound off"}>{mute ? <VolumeX size={18} /> : <Volume2 size={18} />}</button>
      </div>
      {open.map((q) => {
        const Icon = ICON[q.kind];
        const w = wait(q);
        return (
          <div key={q.id} className={`ta-card k-${q.kind} ${w > 300 ? "late" : w > 120 ? "slow" : ""}`}>
            <div className="ta-table">{q.table}</div>
            <div className="ta-what">
              <span className="ta-kind"><Icon size={18} aria-hidden /> {q.label}{q.times_asked > 1 && <em> · asked {q.times_asked}×</em>}</span>
              <span className="ta-sub">
                {mins(w)} waiting
                {q.kind === "bill" && q.total_paise ? ` · ${formatPaise(q.total_paise)}` : ""}
                {q.note === "paid_upi" ? " · says paid by UPI, check it" : q.note === "pay_counter" ? " · paying at counter" : ""}
              </span>
            </div>
            <button className="ta-done" onClick={() => markDone(q)}>Done</button>
          </div>
        );
      })}
      {full && done.length > 0 && (
        <div className="ta-done-list">
          <div className="small muted">Done in the last 10 minutes</div>
          {done.map((q) => <div key={q.id} className="small">{q.table} · {q.label} · {mins(q.waiting_secs)}{q.done_by ? ` · ${q.done_by}` : ""}</div>)}
        </div>
      )}
    </div>
  );
}
