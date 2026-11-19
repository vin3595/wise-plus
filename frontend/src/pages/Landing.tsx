import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { EmbLogo, SUPPORT_LABEL, supportWa } from "../components/Brand";
import "./landing.css";

interface Info { bundle_paise: number; founding_paise: number; founding_slots: number; founding_left: number; trial_days: number;
  msgs_per_outlet: number; pixel_id: string | null; whatsapp: string | null }
declare global { interface Window { fbq?: (...a: unknown[]) => void; _fbq?: unknown } }

const rs = (p: number) => "₹" + (p / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const FEATURES: [string, string, string][] = [
  ["💬", "Bills on WhatsApp", "Every bill reaches your customer on WhatsApp the moment it's paid, with a 1-tap rating. No paper rolls to run out of."],
  ["🎁", "Offers that bring people back", "Customers send a keyword like COFFEE29, get their own QR, and you scan it at the counter. Every offer shows its ROI."],
  ["📱", "Run it from WhatsApp", "Ask “sales today”, send a supplier's bill photo, type “kharcha milk 300 6L”. Your restaurant answers on WhatsApp."],
  ["🧾", "Supplier bills read from a photo", "Snap a bill: items, GST and supplier dues go in by themselves. Loose petty slips too, with the quantity for stock."],
  ["📦", "Stock and recipe costs", "Every sale uses up its recipe. Know your real food cost, wastage and what to order tomorrow."],
  ["⚡", "UPI QR on the bill", "The bill prints with its own QR for the exact amount, and the POS says “350 rupees received” when it's paid."],
  ["📊", "GST and Tally for your CA", "One tap: sales, purchases, credit notes, bank lines and expenses as a Tally file, with every bill photo."],
  ["🍳", "Kitchen screens and table QR", "Orders go to the kitchen screen; guests order, call a waiter or ask for the bill by scanning the table QR."],
];
const FAQ: [string, string][] = [
  ["Do I need new hardware?", "No. Any Android phone, tablet or laptop with your existing thermal printer works."],
  ["I'm on another POS today. Is moving hard?", "We move your menu for you: send a photo or an Excel file on WhatsApp and it's set up the same day."],
  ["Is my data safe?", "Backed up every night to a separate, encrypted store. You can download all of it from Settings, any time."],
  ["How do I get help?", "On WhatsApp, the same number your bills come from. We're based in Jodhpur and can visit."],
];

/** The public page that sells Ease My Bill: what it does, the price, a free trial, a demo booking. */
export default function Landing() {
  const params = useMemo(() => new URLSearchParams(location.search), []);
  const ref = (params.get("ref") || "").toUpperCase();
  const utm = useMemo(() => Object.fromEntries([...params.entries()].filter(([k]) => k.startsWith("utm_") || k === "fbclid" || k === "gclid")), [params]);
  const [info, setInfo] = useState<Info | null>(null);
  const [invitedBy, setInvitedBy] = useState<string | null>(null);
  const [slots, setSlots] = useState<string[]>([]);
  const [f, setF] = useState({ name: "", phone: "", restaurant: "", city: "Jodhpur", outlets: "1", uses_now: "", demo_at: "", consent: true, website: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<{ trial_url: string; demo_at: string | null } | null>(null);

  useEffect(() => {
    document.title = "Ease My Bill · Restaurant POS with WhatsApp built in";
    api<Info>("/public/growth/info").then(setInfo).catch(() => setInfo(null));
    api<{ slots: string[] }>("/public/growth/demo-slots").then((x) => setSlots(x.slots)).catch(() => setSlots([]));
    if (ref) api<{ restaurant: string }>(`/public/growth/ref/${encodeURIComponent(ref)}`).then((x) => setInvitedBy(x.restaurant)).catch(() => setInvitedBy(null));
  }, [ref]);
  useEffect(() => {                          // Meta pixel, for the ads (only when set up)
    const id = info?.pixel_id;
    if (!id || window.fbq) return;
    /* eslint-disable */
    (function (f: any, b: Document, e: string, v: string) {
      if (f.fbq) return; const n: any = f.fbq = function (...a: unknown[]) { n.callMethod ? n.callMethod(...a) : n.queue.push(a); };
      if (!f._fbq) f._fbq = n; n.push = n; n.loaded = true; n.version = "2.0"; n.queue = [];
      const t = b.createElement(e) as HTMLScriptElement; t.async = true; t.src = v; b.head.appendChild(t);
    })(window, document, "script", "https://connect.facebook.net/en_US/fbevents.js");
    /* eslint-enable */
    window.fbq!("init", id);
    window.fbq!("track", "PageView");
  }, [info?.pixel_id]);

  const trialHref = `/signup${ref ? `?ref=${ref}` : ""}`;
  const days = useMemo(() => {
    const by: Record<string, string[]> = {};
    for (const s of slots) { const d = s.slice(0, 10); (by[d] ||= []).push(s); }
    return Object.entries(by).slice(0, 6);
  }, [slots]);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const r = await api<{ trial_url: string; demo_at: string | null }>("/public/growth/leads", { body: {
        name: f.name, phone: f.phone, restaurant: f.restaurant || null, city: f.city || null, outlets: Number(f.outlets) || null,
        uses_now: f.uses_now || null, demo_at: f.demo_at || null, consent: f.consent, ref: ref || null, utm, website: f.website || null } });
      setDone(r);
      window.fbq?.("track", "Lead");
    } catch (x) { setErr(x instanceof Error ? x.message : "Couldn't send. Try again."); }
    finally { setBusy(false); }
  };
  const t = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
  const d = (iso: string) => new Date(iso + (iso.length === 10 ? "T12:00:00" : "")).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });

  return (
    <div className="lp">
      <header className="lp-top">
        <a href="/start" className="lp-logo" aria-label="Ease My Bill"><EmbLogo height={34} /></a>
        <nav><a href="#pricing">Pricing</a><a href="/setup" className="lp-signin">Sign in</a><a href={trialHref} className="lp-btn sm">Start free</a></nav>
      </header>

      <section className="lp-hero">
        {invitedBy && <p className="lp-invite">🎁 Invited by <strong>{invitedBy}</strong>: start with their link and you both get a month free.</p>}
        <h1>The restaurant POS with <span>WhatsApp</span> built in.</h1>
        <p className="lp-sub">Billing, bills on WhatsApp, offers by QR, supplier bills from a photo, stock and recipe costs, Tally for your CA.
          One app, on the phone you already have. Made in Jodhpur, run every day at Shakewallah.</p>
        <div className="lp-cta">
          <a href={trialHref} className="lp-btn">Start {info?.trial_days ?? 14}-day free trial</a>
          <a href="#demo" className="lp-btn ghost">Book a free demo</a>
        </div>
        <p className="lp-note">No card needed · Set up in 10 minutes · We move your menu for you</p>
        <div className="lp-phone" aria-hidden>
          <div className="bubble in">kharcha milk 300 6L</div>
          <div className="bubble out">✅ ₹300 · Milk · 6 L · Petty cash<br />📦 Milk stock +6 L</div>
          <div className="bubble in">sales today</div>
          <div className="bubble out">📈 <b>₹18,420</b> · 142 bills<br />UPI 61% · Cash 32% · Card 7%<br />Top: Cold Coffee (38)</div>
        </div>
      </section>

      <section className="lp-features">
        <h2>Everything a café or restaurant needs</h2>
        <div className="lp-grid">{FEATURES.map(([i, h, p]) => <article key={h}><span className="lp-ico">{i}</span><h3>{h}</h3><p>{p}</p></article>)}</div>
      </section>

      <section className="lp-pricing" id="pricing">
        <h2>Simple pricing</h2>
        <div className="lp-plans">
          <article className="lp-plan">
            <h3>Everything</h3>
            <p className="lp-price">{rs(info?.bundle_paise ?? 99900)}<small>/month per outlet</small></p>
            <ul><li>Everything included, no add-on modules</li><li>POS, KOT, kitchen screens, stock and recipes</li><li>Bill scanner, 150 scans a month</li><li>{(info?.msgs_per_outlet ?? 500).toLocaleString("en-IN")} WhatsApp bills a month per outlet</li>
              <li>Offers, QR and reports</li><li>Tally and GST exports</li><li>Help on WhatsApp</li></ul>
            <a href={trialHref} className="lp-btn ghost">Try free for {info?.trial_days ?? 14} days</a>
          </article>
          <article className="lp-plan hot">
            <span className="lp-tag">Only extras</span>
            <h3>Top-ups, when you need them</h3>
            <ul><li>Extra WhatsApp messages: 1,000 for ₹199</li><li>Extra bill scans: 25 for ₹100</li>
              <li>Top-ups never expire</li><li>No setup fee, cancel any time</li></ul>
            <a href={trialHref} className="lp-btn">Start free trial</a>
          </article>
        </div>
      </section>

      <section className="lp-form" id="demo">
        <h2>Book a free demo</h2>
        <p className="lp-sub">We'll show it on your own menu, in person in Jodhpur or on a call.</p>
        {done ? (
          <div className="lp-done">
            <h3>Thank you! 🙏</h3>
            <p>{done.demo_at ? <>Your demo is booked for <strong>{d(done.demo_at)}, {t(done.demo_at)}</strong>. </> : null}
              {f.consent ? "We've sent you a message on WhatsApp." : "We'll call you soon."}</p>
            <a href={done.trial_url} className="lp-btn">Start your free trial now</a>
          </div>
        ) : (
          <form onSubmit={submit}>
            <div className="lp-row">
              <label>Your name<input required minLength={2} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} autoComplete="name" /></label>
              <label>Mobile (WhatsApp)<input required inputMode="tel" pattern="[0-9 +]{10,15}" value={f.phone} onChange={(e) => setF({ ...f, phone: e.target.value })} autoComplete="tel" placeholder="98290 00000" /></label>
            </div>
            <div className="lp-row">
              <label>Restaurant / café<input value={f.restaurant} onChange={(e) => setF({ ...f, restaurant: e.target.value })} /></label>
              <label>City<input value={f.city} onChange={(e) => setF({ ...f, city: e.target.value })} /></label>
            </div>
            <div className="lp-row">
              <label>Outlets<select value={f.outlets} onChange={(e) => setF({ ...f, outlets: e.target.value })}>{["1", "2", "3", "4", "5", "10"].map((n) => <option key={n} value={n}>{n === "10" ? "6 or more" : n}</option>)}</select></label>
              <label>Billing today with<select value={f.uses_now} onChange={(e) => setF({ ...f, uses_now: e.target.value })}>
                <option value="">Choose…</option>{["Petpooja", "Another POS", "Billing machine", "Paper / Excel", "Just starting"].map((x) => <option key={x}>{x}</option>)}</select></label>
            </div>
            {days.length > 0 && <fieldset className="lp-slots"><legend>Pick a demo time (optional)</legend>
              {days.map(([day, list]) => <div key={day} className="lp-day"><span>{d(day)}</span>
                {list.map((s) => <button type="button" key={s} className={f.demo_at === s ? "on" : ""} onClick={() => setF({ ...f, demo_at: f.demo_at === s ? "" : s })}>{t(s)}</button>)}</div>)}
            </fieldset>}
            <input className="lp-hp" tabIndex={-1} autoComplete="off" value={f.website} onChange={(e) => setF({ ...f, website: e.target.value })} aria-hidden />
            <label className="lp-check"><input type="checkbox" checked={f.consent} onChange={(e) => setF({ ...f, consent: e.target.checked })} />
              Send me details and reminders on WhatsApp (a few messages, reply STOP any time)</label>
            {err && <p className="lp-err">{err}</p>}
            <button className="lp-btn wide" disabled={busy}>{busy ? "Sending…" : f.demo_at ? "Book my demo" : "Send"}</button>
          </form>
        )}
      </section>

      <section className="lp-faq">
        <h2>Questions</h2>
        {FAQ.map(([q, a]) => <details key={q}><summary>{q}</summary><p>{a}</p></details>)}
      </section>

      <footer className="lp-foot">
        <EmbLogo height={26} />
        <p>Made in Jodhpur for Indian restaurants and cafés. WhatsApp or call: <a href={supportWa("Hi, I'd like to know more about Ease My Bill")}>{SUPPORT_LABEL}</a></p>
        <p className="lp-small"><a href="/setup">Sign in</a> · <a href={trialHref}>Start free trial</a></p>
        <p className="lp-small"><a href="/pricing">Pricing</a> · <a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> · <a href="/refunds">Refunds &amp; cancellation</a> · <a href="/delivery">Delivery</a> · <a href="/contact">Contact us</a></p>
      </footer>
      <a className="lp-float" href={trialHref}>Start free</a>
    </div>
  );
}
