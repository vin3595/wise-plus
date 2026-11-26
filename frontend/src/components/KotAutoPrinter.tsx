import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { desktop, printNow } from "../desktop";
import type { PrintFeed, PrintJob } from "../types";
import { KotSlip } from "./KotSlip";

const KEY = "wise.kot_here";
export const kotsHere = () => { try { return localStorage.getItem(KEY) !== "off"; } catch { return true; } };
export const setKotsHere = (on: boolean) => { try { localStorage.setItem(KEY, on ? "on" : "off"); } catch { /* storage blocked */ } };

function stationId(): string {
  try {
    let id = localStorage.getItem("wise.print_station");
    if (!id) { id = `pc-${Math.random().toString(36).slice(2, 10)}`; localStorage.setItem("wise.print_station", id); }
    return id;
  } catch { return "pc-app"; }
}

/** In the Ease My Bill Windows app, the order screen is also the print station: every KOT for areas
 *  without a network printer prints here by itself, with no dialog. */
export default function KotAutoPrinter({ printKey, enabled }: { printKey: string; enabled: boolean }) {
  const station = useRef(stationId()).current;
  const [printing, setPrinting] = useState<{ job: PrintJob; copies: number } | null>(null);
  const [last, setLast] = useState<{ ok: boolean; text: string } | null>(null);
  const on = enabled && !!printKey && !!desktop() && kotsHere();
  useEffect(() => {
    if (!on) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const paint = () => new Promise<void>((r) => requestAnimationFrame(() => setTimeout(r, 150)));
    const tick = async () => {
      try {
        const q = new URLSearchParams({ station, mode: "browser", areas: "", peek: "false" });
        const f = await api<PrintFeed>(`/public/print/${encodeURIComponent(printKey)}/jobs?${q}`);
        for (const job of f.jobs) {
          if (stop) return;
          setPrinting({ job, copies: job.printer?.copies || 1 });
          await paint();
          const error = (await printNow("kot")) || undefined;
          setPrinting(null);
          await api(`/public/print/${encodeURIComponent(printKey)}/jobs/${job.kot_id}?station=${station}`,
            { body: { kind: job.kind, ok: !error, error: error || null } });
          setLast({ ok: !error, text: error ? `KOT didn't print: ${error}` : `KOT printed${job.kot.token_no ? ` · #${job.kot.token_no}` : ""}` });
        }
      } catch { /* offline: KOTs wait and print when back */ }
      if (!stop) timer = setTimeout(tick, 2500);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [on, printKey, station]);
  if (!on) return null;
  return (
    <>
      {last && !last.ok && <div className="banner warn small" role="status">{last.text}</div>}
      {printing && createPortal(
        <div id="print-root">
          {Array.from({ length: printing.copies }, (_, i) => (
            <KotSlip key={i} job={printing.job} copy={i + 1} copies={printing.copies} paper={printing.job.printer?.paper_mm || 80} />
          ))}
        </div>, document.body)}
    </>
  );
}
