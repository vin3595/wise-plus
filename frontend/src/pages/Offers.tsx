import { useCallback, useEffect, useState } from "react";
import { Gift, Printer } from "lucide-react";
import { api } from "../api";
import { formatPaise, inputToPaise } from "../money";
import { QrImage } from "../qr";
import { useSession } from "../session";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

interface Offer { id: string; name: string; keyword: string; variant_id: string; item: string | null; regular_paise: number | null; price_paise: number;
  outlet_ids: string[] | null; valid_from: string | null; valid_to: string | null; per_phone: number; code_days: number; daily_limit: number | null;
  message: string | null; is_active: boolean; require_otp: boolean; issued: number; used: number }
interface Code { code: string; phone: string; status: string; issued_on: string; expires_on: string; used_at: string | null }

const day = (d: string | null) => (d ? new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "");

/** Marketing › WhatsApp offers: a customer sends the keyword, gets a QR; the counter scans it and bills the offer price. */
export default function Offers() {
  const { can } = useSession();
  const [data, setData] = useState<{ offers: Offer[]; whatsapp_number: string | null } | null>(null);
  const [edit, setEdit] = useState<Offer | "new" | null>(null);
  const [codes, setCodes] = useState<{ offer: Offer; rows: Code[] } | null>(null);
  const [poster, setPoster] = useState<Offer | null>(null);
  const load = useCallback(() => api<{ offers: Offer[]; whatsapp_number: string | null }>("/offers").then(setData), []);
  useEffect(() => { load(); }, [load]);
  const manage = can("menu.manage");
  if (!data) return <p className="muted">Loading…</p>;
  const wa = data.whatsapp_number?.replace(/\D/g, "");
  return (
    <div className="stack">
      <h1>WhatsApp offers</h1>
      <p className="muted">A customer sends the keyword to your WhatsApp number and gets their own QR code. At the counter, scan it: the item goes on the bill
        at the offer price. Each code works once.</p>
      {manage && !edit && <div className="row"><Button onClick={() => setEdit("new")}><Gift size={16} aria-hidden /> New offer</Button></div>}
      {edit && <OfferForm offer={edit === "new" ? null : edit} onDone={() => { setEdit(null); load(); }} onCancel={() => setEdit(null)} />}
      {data.offers.length === 0 && !edit && <Card title="No offers yet"><p className="muted">Example: keyword <strong>COFFEE29</strong> → Cold Coffee at ₹29, one per number, code valid 7 days.</p></Card>}
      {data.offers.map((o) => (
        <Card key={o.id} title={o.name} actions={<span className={`pill ${o.is_active ? "ok" : ""}`}>{o.is_active ? "On" : "Off"}</span>}>
          <p>Send <strong className="mono">{o.keyword}</strong> → {o.item} at <strong>{formatPaise(o.price_paise)}</strong>
            {o.regular_paise ? <s className="muted"> {formatPaise(o.regular_paise)}</s> : null}</p>
          <p className="muted small">
            {o.per_phone === 1 ? "One per number" : `${o.per_phone} per number`} · code valid {o.code_days} day{o.code_days === 1 ? "" : "s"}
            {o.daily_limit ? ` · ${o.daily_limit} codes a day` : ""}{o.require_otp ? " · OTP check at the counter" : ""}{o.valid_from || o.valid_to ? ` · ${day(o.valid_from) || "now"} – ${day(o.valid_to) || "no end"}` : ""}
          </p>
          <p><strong>{o.issued}</strong> codes sent · <strong>{o.used}</strong> used{o.issued ? ` (${Math.round((o.used / o.issued) * 100)}%)` : ""}</p>
          <div className="row wrap">
            {manage && <Button variant="ghost" onClick={() => setEdit(o)}>Edit</Button>}
            <Button variant="ghost" onClick={() => api<Code[]>(`/offers/${o.id}/codes`).then((rows) => setCodes({ offer: o, rows }))}>Codes</Button>
            {wa && <Button variant="ghost" onClick={() => setPoster(o)}><Printer size={16} aria-hidden /> Poster</Button>}
          </div>
        </Card>))}
      {!wa && data.offers.length > 0 && <p className="muted small">Posters with a scan-to-WhatsApp QR appear once the business WhatsApp number is set on the server.</p>}
      {codes && (
        <Card title={`Codes · ${codes.offer.name}`} actions={<button className="link" onClick={() => setCodes(null)}>Close</button>}>
          {codes.rows.length === 0 ? <p className="muted">No codes sent yet.</p> : (
            <table className="table">
              <thead><tr><th>Code</th><th>Number</th><th>Sent</th><th>Status</th></tr></thead>
              <tbody>{codes.rows.map((c) => (
                <tr key={c.code}><td className="mono">{c.code}</td><td>{c.phone}</td><td>{day(c.issued_on)}</td>
                  <td>{c.status === "used" ? `Used ${c.used_at ? new Date(c.used_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : ""}`
                    : new Date(c.expires_on + "T23:59:59") < new Date() ? "Expired" : `Valid till ${day(c.expires_on)}`}</td></tr>))}
              </tbody>
            </table>)}
        </Card>)}
      {poster && wa && (
        <div className="offer-poster-wrap">
          <div className="offer-poster">
            <h2>{poster.name}</h2>
            <p className="big">{poster.item} for just <strong>{formatPaise(poster.price_paise)}</strong></p>
            <QrImage text={`https://wa.me/${wa}?text=${encodeURIComponent(poster.keyword)}`} size={260} label="Scan to get your offer on WhatsApp" />
            <p>Scan, or WhatsApp <strong>{poster.keyword}</strong> to <strong>+{wa}</strong></p>
          </div>
          <div className="row no-print"><Button onClick={() => window.print()}><Printer size={16} aria-hidden /> Print poster</Button>
            <Button variant="ghost" onClick={() => setPoster(null)}>Close</Button></div>
        </div>)}
    </div>
  );
}

function OfferForm({ offer, onDone, onCancel }: { offer: Offer | null; onDone: () => void; onCancel: () => void }) {
  const [menu, setMenu] = useState<{ variant_id: string; name: string; size?: string | null; price_paise: number }[]>([]);
  const [outlets, setOutlets] = useState<{ id: string; name: string }[]>([]);
  const [f, setF] = useState({
    name: offer?.name || "", keyword: offer?.keyword || "", variant_id: offer?.variant_id || "", price: offer ? String(offer.price_paise / 100) : "",
    valid_from: offer?.valid_from || "", valid_to: offer?.valid_to || "", per_phone: String(offer?.per_phone ?? 1), code_days: String(offer?.code_days ?? 7),
    daily_limit: offer?.daily_limit ? String(offer.daily_limit) : "", message: offer?.message || "", is_active: offer?.is_active ?? true,
    require_otp: offer?.require_otp ?? false,
    outlet_ids: offer?.outlet_ids || [] as string[],
  });
  const { run, busy, error } = useAction();
  useEffect(() => {
    api<{ items: { variant_id: string; name: string; size?: string | null; price_paise: number }[] }>("/barcodes").then((r) => setMenu(r.items));
    api<{ id: string; name: string }[]>("/tenant/outlets").then(setOutlets).catch(() => setOutlets([]));
  }, []);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }));
  const save = async () => {
    const body = { name: f.name.trim(), keyword: f.keyword.trim(), variant_id: f.variant_id, price_paise: inputToPaise(f.price) ?? 0,
      valid_from: f.valid_from || null, valid_to: f.valid_to || null, per_phone: Number(f.per_phone) || 1, code_days: Number(f.code_days) || 7,
      daily_limit: f.daily_limit ? Number(f.daily_limit) : null, message: f.message || null, is_active: f.is_active, require_otp: f.require_otp, outlet_ids: f.outlet_ids.length ? f.outlet_ids : null };
    const r = await run(() => (offer ? api(`/offers/${offer.id}`, { method: "PUT", body }) : api("/offers", { body })));
    if (r) onDone();
  };
  const picked = menu.find((m) => m.variant_id === f.variant_id);
  return (
    <Card title={offer ? `Edit · ${offer.name}` : "New WhatsApp offer"}>
      <div className="grid-2">
        <Field label="Name customers see"><Input value={f.name} onChange={set("name")} placeholder="Cold Coffee at ₹29" /></Field>
        <Field label="Keyword they send"><Input value={f.keyword} onChange={(e) => setF((x) => ({ ...x, keyword: e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "") }))} placeholder="COFFEE29" /></Field>
        <Field label="Item">
          <Select value={f.variant_id} onChange={set("variant_id")}>
            <option value="">Choose…</option>
            {menu.map((m) => <option key={m.variant_id} value={m.variant_id}>{m.name}{m.size ? ` (${m.size})` : ""} · {formatPaise(m.price_paise)}</option>)}
          </Select>
        </Field>
        <Field label={`Offer price${picked ? ` (usually ${formatPaise(picked.price_paise)})` : ""}`}><Input inputMode="decimal" value={f.price} onChange={set("price")} placeholder="29" /></Field>
        <Field label="Starts (optional)"><Input type="date" value={f.valid_from} onChange={set("valid_from")} /></Field>
        <Field label="Ends (optional)"><Input type="date" value={f.valid_to} onChange={set("valid_to")} /></Field>
        <Field label="Codes per number"><Input type="number" min={1} value={f.per_phone} onChange={set("per_phone")} /></Field>
        <Field label="Each code works for (days)"><Input type="number" min={1} value={f.code_days} onChange={set("code_days")} /></Field>
        <Field label="Codes a day, at most (optional)"><Input type="number" min={1} value={f.daily_limit} onChange={set("daily_limit")} placeholder="No limit" /></Field>
        <Field label="Extra line in the message (optional)"><Input value={f.message} onChange={set("message")} placeholder="Valid on takeaway only" /></Field>
      </div>
      {outlets.length > 1 && (
        <fieldset className="plain-fieldset"><legend className="small">Where it works (none ticked = all outlets)</legend>
          <div className="row wrap">{outlets.map((o) => (
            <label key={o.id} className="check"><input type="checkbox" checked={f.outlet_ids.includes(o.id)}
              onChange={(e) => setF((x) => ({ ...x, outlet_ids: e.target.checked ? [...x.outlet_ids, o.id] : x.outlet_ids.filter((y) => y !== o.id) }))} /> {o.name}</label>))}
          </div></fieldset>)}
      <label className="check"><input type="checkbox" checked={f.require_otp} onChange={(e) => setF((x) => ({ ...x, require_otp: e.target.checked }))} /> Validation (OTP):
        the cashier taps Send OTP after scanning; the customer gets a code on WhatsApp and reads it out</label>
      <label className="check"><input type="checkbox" checked={f.is_active} onChange={(e) => setF((x) => ({ ...x, is_active: e.target.checked }))} /> Offer is on</label>
      <ErrorText error={error} />
      <div className="row">
        <Button disabled={busy || !f.name.trim() || f.keyword.length < 3 || !f.variant_id || !f.price} onClick={save}>{offer ? "Save" : "Create offer"}</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </Card>
  );
}
