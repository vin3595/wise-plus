import { useEffect, useState } from "react";
import { adminTokenStore, api } from "../api";
import { EmbLogo } from "../components/Brand";
import { Button, Card, ErrorText, Field, Input, useAction } from "../ui";
import { AllRestaurants, YourRazorpay } from "./Billing";
import Leads from "./Leads";

// Ease My Bill admin, at /admin. Its own sign-in (WISE_PLATFORM_ADMIN_PHONES), separate from every restaurant,
// your own included: restaurants, leads and the Razorpay account restaurants pay you through.

type Tab = "restaurants" | "leads" | "razorpay" | "whatsapp";

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
  const tabs: [Tab, string][] = [["restaurants", "Restaurants"], ["leads", "Leads"], ["razorpay", "Your Razorpay"], ["whatsapp", "Your WhatsApp"]];
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
      {tab === "whatsapp" && <YourWhatsApp />}
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
  const [f, setF] = useState({ token: "", phone_number_id: "1186830631186425", waba_id: "2509028906240984", app_id: "1428110315703396", app_secret: "" });
  const [done, setDone] = useState<{ display_number: string; name: string | null; verified: string | null } | null>(null);
  const act = useAction();
  const load = () => api<Info>("/platform/whatsapp-sender").then(setK).catch(() => setK(null));
  useEffect(() => { load(); }, []);
  if (!k) return <p className="muted">Loading…</p>;
  return (
    <Card title="Your WhatsApp number (Ease My Bill's sender)">
      <p className="small">Sending from: <strong>{k.display_number ? `+${k.display_number}` : "not set"}</strong> · phone number ID {k.phone_number_id || "—"}</p>
      {done ? <p className="ok-text">Saved and checked with Meta ✓ +{done.display_number}{done.name ? ` (${done.name})` : ""}. It switches over when the server restarts — tell Claude "saved".</p> : <>
        <p className="muted small">Meta Business Suite › Settings › System users › EaseMyBillAPI › Generate token → Copy. App secret: developers.facebook.com › Ease My Bill › App settings › Basic.</p>
        <div className="row wrap">
          <Field label="Permanent token"><Input type="password" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value.trim() })} autoComplete="off" placeholder="EAA…" /></Field>
          <Field label="App secret (optional now)"><Input type="password" value={f.app_secret} onChange={(e) => setF({ ...f, app_secret: e.target.value.trim() })} autoComplete="off" /></Field>
          <Field label="Phone number ID"><Input value={f.phone_number_id} onChange={(e) => setF({ ...f, phone_number_id: e.target.value.trim() })} inputMode="numeric" /></Field>
          <Field label="WhatsApp account ID"><Input value={f.waba_id} onChange={(e) => setF({ ...f, waba_id: e.target.value.trim() })} inputMode="numeric" /></Field>
        </div>
        <ErrorText error={act.error} />
        <Button disabled={act.busy || !f.token.startsWith("EAA")} onClick={() => act.run(async () => {
          setDone(await api("/platform/whatsapp-sender", { body: f })); setF({ ...f, token: "", app_secret: "" }); load();
        })}>{act.busy ? "Checking with Meta…" : "Check and save"}</Button>
      </>}
    </Card>
  );
}
