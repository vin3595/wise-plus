import { useState } from "react";
import { Sparkles } from "lucide-react";
import { api } from "../api";

const CHIPS = ["dues", "sales", "last ", "price "];

/** Home > Ask: the owner's keyword answers (same as on WhatsApp). "dues", "dues kanha", "last kanha", "price vanilla". */
export function AskBox() {
  const [q, setQ] = useState("");
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ask = async (question: string) => {
    if (!question.trim()) return;
    setBusy(true);
    try { setText((await api<{ text: string }>("/assistant/ask", { body: { q: question } })).text); }
    catch (e) { setText(e instanceof Error ? e.message : "Couldn't ask right now"); }
    finally { setBusy(false); }
  };
  return (
    <div className="card ask-box">
      <form className="ask-row" onSubmit={(e) => { e.preventDefault(); ask(q); }}>
        <Sparkles size={18} aria-hidden className="ask-icon" />
        <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask: dues, last kanha, price vanilla…"
          aria-label="Ask about dues, last orders or prices" enterKeyHint="search" autoComplete="off" />
        <button className="btn btn-primary" disabled={busy || !q.trim()}>{busy ? "…" : "Ask"}</button>
      </form>
      <div className="ask-chips">
        {CHIPS.map((c) => <button key={c} type="button" className="chip" onClick={() => { setQ(c); if (!c.endsWith(" ")) ask(c); else (document.querySelector(".ask-row input") as HTMLInputElement | null)?.focus(); }}>{c.trim()}</button>)}
      </div>
      {text && <WaText text={text} />}
    </div>
  );
}

/** WhatsApp-style text: *bold*, _italic_, one line per line. */
export function WaText({ text }: { text: string }) {
  return (
    <div className="ask-answer" role="status">
      {text.split("\n").map((line, i) => (
        <p key={i}>{line.split(/(\*[^*]+\*|_[^_]+_)/g).map((part, j) =>
          part.startsWith("*") && part.endsWith("*") && part.length > 2 ? <strong key={j}>{part.slice(1, -1)}</strong>
            : part.startsWith("_") && part.endsWith("_") && part.length > 2 ? <em key={j}>{part.slice(1, -1)}</em> : part)}</p>
      ))}
    </div>
  );
}
