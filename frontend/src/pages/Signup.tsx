import { useState, type ChangeEvent } from "react";
import { Link, useLocation } from "react-router-dom";
import { api } from "../api";
import { useSession } from "../session";
import type { TokenOut } from "../types";
import { Button, ErrorText, Field, FieldGroup, Input, useAction } from "../ui";
import { EmbLogo, SupportLine } from "../components/Brand";
import { GoogleButton, googleProfile, useGoogleLogin } from "../components/GoogleButton";

export default function Signup() {
  const { signedIn } = useSession();
  const [googleCred, setGoogleCred] = useState<string | null>((useLocation().state as { credential?: string } | null)?.credential || null);
  const g = useGoogleLogin(signedIn, setGoogleCred);
  const qp = new URLSearchParams(location.search);
  const ref = (qp.get("ref") || "").toUpperCase() || null;      // wise-plus: someone's referral link
  const [form, setForm] = useState({ restaurant_name: "", owner_name: "", email: "", phone: qp.get("phone") || "" });
  const modules = ["pos", "stock", "bills"];          // one plan: everything is on
  const [made, setMade] = useState<TokenOut | null>(null);
  const [via, setVia] = useState<"phone" | "email">("phone");
  const [sent, setSent] = useState<{ sent_to: string; dev_otp: string | null } | null>(null);
  const [otp, setOtp] = useState("");
  const { busy, error, run } = useAction();
  const set = (k: keyof typeof form) => (e: ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  if (made) {
    return (
      <div className="auth-page"><div className="auth-card">
        <div className="auth-brand"><EmbLogo height={52} /></div>
        <h1>You're all set</h1>
        <p className="muted">Your login ID is</p>
        <p style={{ fontSize: "2.4rem", fontWeight: 700, letterSpacing: "0.2em", margin: "0.2em 0" }}>{made.tenant.login_id}</p>
        <p className="muted small">Note it down. You and your staff type it once on each phone or tablet, then sign in with your Gmail or mobile number.</p>
        <Button onClick={() => signedIn(made)}>Continue</Button>
      </div></div>
    );
  }

  if (sent) {
    return (
      <div className="auth-page"><div className="auth-card">
        <div className="auth-brand"><EmbLogo height={52} /></div>
        <h1>{via === "email" ? "Check your email" : "Verify your mobile"}</h1>
        <p className="muted">We sent a 6-digit code to {sent.sent_to}.{via === "email" ? " Check your inbox (and spam)." : " Check WhatsApp."}</p>
        {sent.dev_otp && <p className="dev-note">Development mode code: <strong>{sent.dev_otp}</strong></p>}
        <form onSubmit={(e) => {
          e.preventDefault();
          run(async () => setMade(await api<TokenOut>("/auth/signup/verify", { body: { ...form, modules, otp, ref, via } })));
        }}>
          <Field label="Code">
            <Input value={otp} onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
              inputMode="numeric" autoComplete="one-time-code" autoFocus />
          </Field>
          <ErrorText error={error} />
          <Button disabled={busy || otp.length !== 6}>Start now</Button>
          <button type="button" className="link" disabled={busy} onClick={() => { setOtp("");
            run(async () => setSent(await api("/auth/signup/start", { body: { ...form, modules, via } }))); }}>Send a new code</button>
          {" · "}
          <button type="button" className="link" onClick={() => setSent(null)}>Edit details</button>
          {via === "phone" && <> · <button type="button" className="link" disabled={busy} onClick={() => { setVia("email");
            run(async () => setSent(await api("/auth/signup/start", { body: { ...form, modules, via: "email" } }))); }}>Email me the code instead</button></>}
        </form>
      </div></div>
    );
  }

  if (googleCred) {
    const who = googleProfile(googleCred);
    return (
      <div className="auth-page"><div className="auth-card">
        <div className="auth-brand"><EmbLogo height={52} /></div>
        <h1>Set up your business</h1>
        <p className="muted">Signing up as <b>{who.email}</b>. Free for 30 days, then ₹999/month per outlet.</p>
        <form onSubmit={(e) => {
          e.preventDefault();
          run(async () => setMade(await api<TokenOut>("/auth/signup/google", { body: {
            credential: googleCred, restaurant_name: form.restaurant_name, owner_name: form.owner_name || who.name || null, phone: form.phone, modules, ref } })));
        }}>
          <Field label="Business name"><Input value={form.restaurant_name} onChange={set("restaurant_name")} autoFocus required /></Field>
          <Field label="Mobile number" hint="Bills, summaries and alerts come here on WhatsApp"><Input inputMode="tel" value={form.phone} onChange={set("phone")} required /></Field>
          <ErrorText error={error && (error.includes("didn't work") ? "Google sign-in timed out. Tap Google again." : error)} />
          <Button disabled={busy}>Create my account</Button>
        </form>
        {error && error.includes("didn't work") && <GoogleButton text="continue_with" onCredential={setGoogleCred} />}
        <p className="muted center small"><button className="link" onClick={() => setGoogleCred(null)}>Use a different way</button></p>
      </div></div>
    );
  }

  return (
    <div className="auth-page"><div className="auth-card">
      <div className="auth-brand"><EmbLogo height={52} /></div>
      <h1>Start free</h1>
      <p className="muted">Billing, stock, supplier bill scans and WhatsApp, all in one plan: <b>₹999/month per outlet</b>. Free for 30 days, ready in a minute.</p>
      <form onSubmit={(e) => {
        e.preventDefault();
        run(async () => setSent(await api("/auth/signup/start", { body: { ...form, modules, via } })));
      }}>
        <Field label="Business name"><Input value={form.restaurant_name} onChange={set("restaurant_name")} required /></Field>
        <Field label="Your name"><Input value={form.owner_name} onChange={set("owner_name")} required /></Field>
        <Field label="Gmail"><Input type="email" value={form.email} onChange={set("email")} required /></Field>
        <Field label="Mobile number"><Input inputMode="tel" value={form.phone} onChange={set("phone")} required /></Field>
        <ErrorText error={error} />
        <FieldGroup label="Send the code to">
          <div className="row" style={{ gap: 8 }}>
            <Button type="button" variant={via === "phone" ? undefined : "ghost"} onClick={() => setVia("phone")}>Mobile</Button>
            <Button type="button" variant={via === "email" ? undefined : "ghost"} onClick={() => setVia("email")}>Email</Button>
          </div>
        </FieldGroup>
        <Button disabled={busy}>Send verification code</Button>
      </form>
      <GoogleButton text="signup_with" onCredential={(c) => g.go(c)} />
      {g.picker}
      <ErrorText error={g.error} />
      <p className="muted center">Already set up? <Link to="/setup">Sign in</Link></p>
      <SupportLine />
    </div></div>
  );
}

