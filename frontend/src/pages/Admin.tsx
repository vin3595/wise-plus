import { useEffect, useState } from "react";
import { adminTokenStore, api } from "../api";
import { EmbLogo } from "../components/Brand";
import { Button, Card, ErrorText, Field, Input, useAction } from "../ui";
import { AllRestaurants, YourRazorpay } from "./Billing";
import Leads from "./Leads";

// Ease My Bill admin, at /admin. Its own sign-in (WISE_PLATFORM_ADMIN_PHONES), separate from every restaurant,
// your own included: restaurants, leads and the Razorpay account restaurants pay you through.

type Tab = "restaurants" | "leads" | "razorpay";

export default function Admin() {
  const [phone, setPhone] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);
  const [tab, setTab] = useState<Tab>(() => (location.hash.slice(1) as Tab) || "restaurants");
  useEffect(() => {
    document.title = "Ease My Bill admin";
    if (!adminTokenStore.get()) { setChecked(true); return; }
    api<{ phone: string }>("/admin/me").then((r) => setPhone(r.phone)).catch(() => adminTokenStore.clear()).finally(() => setChecked(true));
  }, []);
  useEffect(() => { history.replaceState(null, "", `#${tab}`); }, [tab]);
  if (!checked) return <div className="splash">Loading…</div>;
  if (!phone) return <AdminLogin onIn={setPhone} />;
  const tabs: [Tab, string][] = [["restaurants", "Restaurants"], ["leads", "Leads"], ["razorpay", "Your Razorpay"]];
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
