import { useEffect, useState } from "react";
import { Download, FileText, MessageCircle, Smartphone } from "lucide-react";
import { api, tokenStore } from "../api";
import { formatPaise } from "../money";
import { Card, Input } from "../ui";

interface Kept {
  id: string; source: "app" | "whatsapp"; status: "pending" | "saved"; scanned_at: string;
  vendor: string; invoice_no: string | null; invoice_date: string | null; total_paise: number;
  purchase_id: string | null; thumbnail: string | null; has_file: boolean; is_pdf: boolean;
}

const day = (d: string | null, fallback: string) =>
  new Date((d || fallback.slice(0, 10)) + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
const thisMonth = () => new Date().toISOString().slice(0, 7);

/** Money > Bill keeper: every supplier bill read from a photo (in the app or on WhatsApp), with the original. */
export default function BillKeeper() {
  const [q, setQ] = useState("");
  const [month, setMonth] = useState(thisMonth());
  const [rows, setRows] = useState<Kept[] | null>(null);
  const [open, setOpen] = useState<Kept | null>(null);
  const [file, setFile] = useState<string | null>(null);
  useEffect(() => {
    setRows(null);
    const t = setTimeout(() => {
      const p = new URLSearchParams();
      if (q.trim()) p.set("q", q.trim());
      if (month) p.set("month", month);
      api<Kept[]>(`/inventory/purchases/scan/kept?${p}`).then(setRows).catch(() => setRows([]));
    }, 250);
    return () => clearTimeout(t);
  }, [q, month]);
  // The original needs the sign-in token, so it's fetched and shown from memory.
  const fetchFile = async (k: Kept) => {
    const r = await fetch(`/api/inventory/purchases/scan/kept/${k.id}/file`, { headers: { Authorization: `Bearer ${tokenStore.get()}` } });
    return r.ok ? URL.createObjectURL(await r.blob()) : null;
  };
  const show = async (k: Kept) => { setOpen(k); setFile(null); if (k.has_file) setFile(await fetchFile(k)); };
  const download = async (k: Kept) => {
    const url = file || (await fetchFile(k));
    if (!url) return;
    const a = document.createElement("a");
    a.href = url; a.download = `${k.vendor || "bill"}${k.invoice_no ? "-" + k.invoice_no.replace(/\//g, "-") : ""}.${k.is_pdf ? "pdf" : "jpg"}`;
    a.click();
  };
  const total = (rows || []).reduce((s, r) => s + r.total_paise, 0);

  if (open) {
    return (
      <div className="stack">
        <div className="row between"><h1>{open.vendor || "Bill"}</h1><button className="link" onClick={() => { setOpen(null); setFile(null); }}>All bills</button></div>
        <Card>
          <p><strong>{open.invoice_no ? `Invoice ${open.invoice_no}` : "No invoice number"}</strong> · {day(open.invoice_date, open.scanned_at)} · {formatPaise(open.total_paise)}</p>
          <p className="muted small">{open.source === "whatsapp" ? "Sent on WhatsApp" : "Scanned in the app"} on {new Date(open.scanned_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })} · {open.status === "saved" ? "Saved in Purchases" : "Not saved in Purchases yet"}</p>
          {open.has_file && <button className="btn" onClick={() => download(open)}><Download size={16} aria-hidden /> Download original</button>}
        </Card>
        {!open.has_file ? <p className="muted">The original wasn't kept for this bill (scanned before the bill keeper).</p>
          : !file ? <p className="muted">Loading the original…</p>
          : open.is_pdf ? <iframe title="The bill" src={file} className="kept-pdf" />
          : <a href={file} target="_blank" rel="noreferrer"><img src={file} alt={`Bill from ${open.vendor}`} className="kept-full" /></a>}
      </div>
    );
  }

  return (
    <div className="stack">
      <h1>Bill keeper</h1>
      <Card>
        <p className="muted small">Every supplier bill you scan or send on WhatsApp is kept here with the original photo, so you can find any bill later.</p>
        <div className="row nowrap">
          <Input placeholder="Supplier or invoice number" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search bills" />
          <Input type="month" value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Month" />
          {month && <button className="link small" onClick={() => setMonth("")}>All months</button>}
        </div>
      </Card>
      {!rows ? <p className="muted">Loading…</p> : rows.length === 0 ? <Card><p className="muted">No bills {q || month ? "match" : "yet"}. Scan one under Purchases, or send a photo on WhatsApp.</p></Card> : (
        <>
          <p className="small muted">{rows.length} bill{rows.length === 1 ? "" : "s"} · {formatPaise(total)}</p>
          <div className="kept-grid">
            {rows.map((k) => (
              <button key={k.id} className="kept-card" onClick={() => show(k)}>
                <div className="kept-thumb">{k.thumbnail ? <img src={k.thumbnail} alt="" /> : <FileText size={32} aria-hidden />}</div>
                <div className="kept-info">
                  <strong>{k.vendor || "Unknown supplier"}</strong>
                  <span className="small">{day(k.invoice_date, k.scanned_at)}{k.invoice_no ? ` · ${k.invoice_no}` : ""}</span>
                  <span className="num">{formatPaise(k.total_paise)}</span>
                  <span className="small muted kept-src">{k.source === "whatsapp" ? <MessageCircle size={13} aria-hidden /> : <Smartphone size={13} aria-hidden />}
                    {k.status === "saved" ? "Saved" : "Not saved yet"}</span>
                </div>
              </button>))}
          </div>
        </>)}
    </div>
  );
}
