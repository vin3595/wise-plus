import { useEffect, useState } from "react";
import { adminTokenStore, api } from "../api";
import { EmbLogo } from "../components/Brand";
import { Button, Card, ErrorText, Field, Input, useAction } from "../ui";
import { AllRestaurants, YourRazorpay } from "./Billing";
import Leads from "./Leads";

// Ease My Bill admin, at /admin. Its own sign-in (WISE_PLATFORM_ADMIN_PHONES), separate from every restaurant,
// your own included: restaurants, leads and the Razorpay account restaurants pay you through.

type Tab = "restaurants" | "leads" | "razorpay" | "whatsapp" | "codes";

export default function Admin() {
  const [phone, setPhone] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);
  const [tab, setTab] = useState<Tab>(() => (location.hash.slice(1).startsWith("link=") ? "whatsapp" : (location.hash.slice(1) as Tab)) || "restaurants");
  useEffect(() => {
    document.title = "Ease My Bill admin";
    const link = new URLSearchParams(location.hash.slice(1)).get("link");   // one-time sign-in link from the server
    if (link) {
      history.replaceState(null, "", "/admin");
      api<{ token: string; phone: string }>("/admin/login/link", { body: { code: link } })
        .then((r) => { adminTokenStore.set(r.token); setPhone(r.phone); }).catch(() => undefined).finally(() => setChecked(true));
      return;
    }
    if (!adminTokenStore.get()) { setChecked(true); return; }
    api<{ phone: string }>("/admin/me").then((r) => setPhone(r.phone)).catch(() => adminTokenStore.clear()).finally(() => setChecked(true));
  }, []);
  useEffect(() => { history.replaceState(null, "", `#${tab}`); }, [tab]);
  if (!checked) return <div className="splash">Loading…</div>;
  if (!phone) return <AdminLogin onIn={setPhone} />;
  const tabs: [Tab, string][] = [["restaurants", "Restaurants"], ["leads", "Leads"], ["razorpay", "Your Razorpay"], ["whatsapp", "Your WhatsApp"], ["codes", "Login codes"]];
  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "16px" }}>
      <header className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <span className="row" style={{ alignItems: "center", gap: 10 }}><EmbLogo height={26} /><strong>Admin</strong></span>
        <span className="small muted">+91 {phone} · <button className="link" onClick={() => { adminTokenStore.clear(); setPhone(null); }}>Sign out</button></span>
      </header>
      <nav className="row" role="tablist" style={{ gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
        {tabs.map(([k, label]) => <Button key={k} variant={tab === k ? undefined : "ghost"} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{label}</Button>)}
      </nav>
      {tab === "restaurants" && <AllRestaurants />}
      {tab === "leads" && <Leads />}
      {tab === "razorpay" && <YourRazorpay />}
      {tab === "whatsapp" && <><WhatsAppFast2SMS /><YourWhatsApp /></>}
      {tab === "codes" && <LoginCodes />}
    </div>
  );
}

function AdminLogin({ onIn }: { onIn: (phone: string) => void }) {
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const act = useAction();
  return (
    <div style={{ maxWidth: 420, margin: "10vh auto", padding: 16 }}>
      <p style={{ textAlign: "center" }}><EmbLogo height={32} /></p>
      <Card title="Ease My Bill admin">
        <p className="muted small">Only for the people who run Ease My Bill. Restaurants sign in at the normal page.</p>
        <Field label="Mobile number"><Input value={phone} inputMode="numeric" autoComplete="tel" onChange={(e) => setPhone(e.target.value.replace(/\D/g, "").slice(-10))} disabled={sent} /></Field>
        {sent && <Field label="Code"><Input value={code} inputMode="numeric" autoComplete="one-time-code" onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 8))} autoFocus /></Field>}
        {sent && <p className="muted small">If this number runs Ease My Bill, a code is on its way on WhatsApp.</p>}
        <ErrorText error={act.error} />
        <div className="row">
          {!sent ? <Button disabled={act.busy || phone.length !== 10} onClick={() => act.run(async () => { await api("/admin/login/start", { body: { phone } }); setSent(true); })}>Send code</Button>
            : <><Button disabled={act.busy || code.length < 4} onClick={() => act.run(async () => {
                const r = await api<{ token: string; phone: string }>("/admin/login/verify", { body: { phone, code } });
                adminTokenStore.set(r.token); onIn(r.phone);
              })}>Sign in</Button><Button variant="ghost" onClick={() => { setSent(false); setCode(""); }}>Change number</Button></>}
        </div>
      </Card>
    </div>
  );
}

/** Ease My Bill's own WhatsApp number: the sender for bills, sign-in codes and support. */
function YourWhatsApp() {
  type Info = { phone_number_id: string | null; waba_id: string | null; app_id: string | null; display_number: string | null;
    has_token: boolean; has_app_secret: boolean; webhook_url: string; verify_token: string | null };
  const [k, setK] = useState<Info | null>(null);
  const [f, setF] = useState({ token: "", phone_number_id: "1186830631186425", waba_id: "2509028906240984", app_id: "", app_secret: "" });
  const [done, setDone] = useState<{ display_number: string; name: string | null; verified: string | null } | null>(null);
  const act = useAction();
  const load = () => api<Info>("/platform/whatsapp-sender").then((x) => { setK(x); setF((o) => ({ ...o, app_id: o.app_id || x.app_id || "" })); }).catch(() => setK(null));
  useEffect(() => { load(); }, []);
  if (!k) return <p className="muted">Loading…</p>;
  return (
    <Card title="Your WhatsApp number (Ease My Bill's sender)">
      <p className="small">Sending from: <strong>{k.display_number ? `+${k.display_number}` : "not set"}</strong> · phone number ID {k.phone_number_id || "—"}</p>
      {done ? <p className="ok-text">Saved and checked with Meta ✓ +{done.display_number}{done.name ? ` (${done.name})` : ""}. It's used within a minute.</p> : <>
        <p className="muted small">Meta Business Suite › Settings › System users › EaseMyBillAPI › Generate token → Copy. App secret: developers.facebook.com › Ease My Bill › App settings › Basic.</p>
        <div className="row wrap">
          <Field label={k.has_token ? "Permanent token (saved — leave blank)" : "Permanent token"}><Input type="password" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value.trim() })} autoComplete="off" placeholder="EAA…" /></Field>
          <Field label={k.has_app_secret ? "App secret (saved)" : "App secret"}><Input type="password" value={f.app_secret} onChange={(e) => setF({ ...f, app_secret: e.target.value.trim() })} autoComplete="off" /></Field>
          <Field label="Phone number ID"><Input value={f.phone_number_id} onChange={(e) => setF({ ...f, phone_number_id: e.target.value.trim() })} inputMode="numeric" /></Field>
          <Field label="WhatsApp account ID"><Input value={f.waba_id} onChange={(e) => setF({ ...f, waba_id: e.target.value.trim() })} inputMode="numeric" /></Field>
          <Field label="App ID"><Input value={f.app_id} onChange={(e) => setF({ ...f, app_id: e.target.value.trim() })} inputMode="numeric" /></Field>
        </div>
        <ErrorText error={act.error} />
        <Button disabled={act.busy || !(f.token.startsWith("EAA") || (k.has_token && f.app_secret.length > 10))} onClick={() => act.run(async () => {
          setDone(await api("/platform/whatsapp-sender", { body: f })); setF({ ...f, token: "", app_secret: "" }); load();
        })}>{act.busy ? "Checking with Meta…" : "Check and save"}</Button>
      </>}
    </Card>
  );
}

/** Send WhatsApp templates (codes, bills, summaries, offers) through Fast2SMS instead of Meta directly. */
function WhatsAppFast2SMS() {
  type Info = { on: boolean; ready: boolean; has_key: boolean; phone_number_id: string | null; templates: Record<string, string>; names: string[] };
  const [k, setK] = useState<Info | null>(null);
  const [f, setF] = useState<{ api_key: string; phone_number_id: string; templates: Record<string, string> }>({ api_key: "", phone_number_id: "", templates: {} });
  const [to, setTo] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const act = useAction();
  const load = () => api<Info>("/platform/whatsapp-fast2sms").then((x) => {
    setK(x); setF((o) => ({ api_key: "", phone_number_id: o.phone_number_id || x.phone_number_id || "", templates: { ...x.templates, ...o.templates } }));
  }).catch(() => setK(null));
  useEffect(() => { load(); }, []);
  if (!k) return <p className="muted">Loading…</p>;
  const save = (body: object, done: string) => act.run(async () => { setK(await api<Info>("/platform/whatsapp-fast2sms", { body })); setMsg(done); setF((o) => ({ ...o, api_key: "" })); });
  return (
    <Card title="Send through Fast2SMS">
      <p className="small">{k.on ? (k.ready ? "On ✓ Codes, bills, summaries and offers go out through Fast2SMS." : "On, but not ready: fill in the details below.")
        : "Off: messages go to Meta directly."}</p>
      <ol className="small muted" style={{ paddingLeft: 18 }}>
        <li>fast2sms.com › WhatsApp › connect your number (+91 97991 19994).</li>
        <li>Copy the <em>Phone Number ID</em> it shows, and the API key from Dev API.</li>
        <li>WhatsApp › Templates: copy each approved template's <em>Message ID</em> into the boxes below.</li>
      </ol>
      <div className="row wrap">
        <Field label={k.has_key ? "Fast2SMS API key (saved — paste to change)" : "Fast2SMS API key"}><Input type="password" value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value.trim() })} autoComplete="off" /></Field>
        <Field label="Phone Number ID (Fast2SMS)"><Input value={f.phone_number_id} onChange={(e) => setF({ ...f, phone_number_id: e.target.value.replace(/\D/g, "") })} inputMode="numeric" /></Field>
      </div>
      <div className="row wrap">
        {k.names.map((n) => <Field key={n} label={`Message ID: ${n}`}><Input value={f.templates[n] || ""} inputMode="numeric"
          onChange={(e) => setF({ ...f, templates: { ...f.templates, [n]: e.target.value.replace(/\D/g, "") } })} /></Field>)}
      </div>
      <ErrorText error={act.error} />
      {msg && <p className="ok-text">{msg}</p>}
      <div className="row wrap" style={{ gap: 8 }}>
        <Button disabled={act.busy} onClick={() => save({ ...(f.api_key ? { api_key: f.api_key } : {}), phone_number_id: f.phone_number_id, templates: f.templates }, "Saved ✓")}>Save</Button>
        {k.on ? <Button variant="ghost" disabled={act.busy} onClick={() => save({ on: false }, "Off: back to Meta directly.")}>Turn off</Button>
          : <Button variant="ghost" disabled={act.busy || !k.ready} onClick={() => save({ on: true }, "On ✓ WhatsApp now goes through Fast2SMS.")}>Turn on</Button>}
      </div>
      <div className="row wrap" style={{ marginTop: 12, alignItems: "flex-end" }}>
        <Field label="Send a test to"><Input value={to} inputMode="numeric" placeholder="10-digit mobile" onChange={(e) => setTo(e.target.value.replace(/\D/g, "").slice(-10))} /></Field>
        <Button variant="ghost" disabled={act.busy || to.length !== 10 || !k.ready} onClick={() => act.run(async () => {
          const r = await api<{ request_id: string | null }>("/platform/whatsapp-fast2sms/test", { body: { to } });
          setMsg(`Test sent ✓ (request ${r.request_id || "—"}). Check WhatsApp on ${to}.`);
        })}>Send test</Button>
      </div>
    </Card>
  );
}

/** How sign-in codes reach people: a mobile number gets WhatsApp or SMS, an email address gets an email from your Gmail. */
function LoginCodes() {
  type Info = { phone: "whatsapp" | "sms"; has_sms_key: boolean; mail_user: string | null; has_mail_password: boolean; whatsapp_ready: boolean; google_client_id: string | null };
  const [k, setK] = useState<Info | null>(null);
  const [f, setF] = useState({ sms_key: "", mail_user: "", mail_password: "", google_client_id: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const act = useAction();
  const load = () => api<Info>("/platform/login-codes").then((x) => { setK(x); setF((o) => ({ ...o, mail_user: o.mail_user || x.mail_user || "easemybills@gmail.com" })); }).catch(() => setK(null));
  useEffect(() => { load(); }, []);
  if (!k) return <p className="muted">Loading…</p>;
  const save = (body: Record<string, string>, done: string) => act.run(async () => {
    const r = await api<Info & { test_email_to: string | null }>("/platform/login-codes", { body });
    setK(r); setMsg(r.test_email_to ? `Saved ✓ A test email went to ${r.test_email_to}.` : done);
    setF((o) => ({ ...o, sms_key: "", mail_password: "", google_client_id: "" }));
  });
  return (<>
    <Card title="Codes to a mobile number">
      <p className="small">{k.whatsapp_ready ? "On ✓ Codes to a mobile number go on WhatsApp (through Fast2SMS)." : "WhatsApp isn't set up yet: see Your WhatsApp."}</p>
    </Card>
    <Card title="Sign in with Google">
      <p className="small">{k.google_client_id ? <>On ✓ The Google button shows on sign-in and sign-up.</> : "Off: add a Google Client ID to show the Google button."}</p>
      <ol className="small muted" style={{ paddingLeft: 18 }}>
        <li>Open <a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noreferrer">console.cloud.google.com › Google Auth Platform › Clients</a> (sign in with easemybills@gmail.com). If asked, set up the app: name <em>Ease My Bill</em>, audience <em>External</em>.</li>
        <li>Create client › Web application › Authorised JavaScript origins: <code>https://www.easemybill.com</code> and <code>https://easemybill.com</code> › Create.</li>
        <li>Copy the <em>Client ID</em> (ends in .apps.googleusercontent.com) and paste it here.</li>
      </ol>
      <div className="row wrap">
        <Field label="Google Client ID"><Input value={f.google_client_id} onChange={(e) => setF({ ...f, google_client_id: e.target.value.trim() })} autoComplete="off" placeholder={k.google_client_id || "…apps.googleusercontent.com"} /></Field>
      </div>
      <Button disabled={act.busy || !f.google_client_id.endsWith(".apps.googleusercontent.com")} onClick={() => save({ google_client_id: f.google_client_id }, "Saved ✓ The Google button is on.")}>Save</Button>
    </Card>
    <Card title="Codes to an email address (Gmail)">
      <p className="small">{k.mail_user && k.has_mail_password ? <>On ✓ Codes are emailed from <strong>{k.mail_user}</strong>.</> : "Off: people who type an email address can't get a code yet."}</p>
      <ol className="small muted" style={{ paddingLeft: 18 }}>
        <li>Sign in to that Gmail on your phone or laptop. 2-Step Verification must be on (myaccount.google.com › Security).</li>
        <li>Open <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">myaccount.google.com/apppasswords</a>, type the name <em>Ease My Bill</em> and tap Create.</li>
        <li>Copy the 16-letter password Google shows and paste it here.</li>
      </ol>
      <div className="row wrap">
        <Field label="Gmail address"><Input type="email" value={f.mail_user} onChange={(e) => setF({ ...f, mail_user: e.target.value.trim() })} autoComplete="off" /></Field>
        <Field label={k.has_mail_password ? "App password (saved — paste to change)" : "App password"}><Input type="password" value={f.mail_password} onChange={(e) => setF({ ...f, mail_password: e.target.value })} autoComplete="off" placeholder="abcd efgh ijkl mnop" /></Field>
      </div>
      <Button disabled={act.busy || !f.mail_user.includes("@") || f.mail_password.replace(/\s/g, "").length < 16} onClick={() => save({ mail_user: f.mail_user, mail_password: f.mail_password }, "Saved ✓")}>
        {act.busy ? "Checking with Gmail…" : "Check and save"}</Button>
    </Card>
    <ErrorText error={act.error} />
    {msg && <p className="ok-text">{msg}</p>}
  </>);
}
