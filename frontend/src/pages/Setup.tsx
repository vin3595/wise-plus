import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { SupportLine } from "../components/Brand";
import { api } from "../api";
import { GoogleButton, useGoogleLogin } from "../components/GoogleButton";
import { useSession } from "../session";
import type { TokenOut } from "../types";
import { Button, ErrorText, Field, Input, useAction } from "../ui";

/** First screen on a new device: which restaurant is this? */
export default function Setup() {
  const { chooseRestaurant, signedIn } = useSession();
  const [id, setId] = useState("");
  const [byContact, setByContact] = useState(false);
  const g = useGoogleLogin(async (t) => { await signedIn(t); nav("/"); });
  const { busy, error, run } = useAction();
  const nav = useNavigate();

  return (
    <div className="auth-page">
      <div className="auth-card">
        <h1>Welcome</h1>
        {byContact ? <ContactSignIn onBack={() => setByContact(false)} /> : <>
        <p className="muted">Enter your business login ID to set up this device.</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            run(async () => {
              await chooseRestaurant(id.trim());
              nav("/login");
            });
          }}
        >
          <Field label="Restaurant login ID">
            <Input value={id} onChange={(e) => setId(e.target.value)} placeholder="6-digit login ID, e.g. 482913" autoFocus
              autoCapitalize="none" autoCorrect="off" required />
          </Field>
          <ErrorText error={error && (error.includes("not found") ? "No restaurant with that login ID" : error)} />
          <Button disabled={busy || !id.trim()}>Continue</Button>
        </form>
        <GoogleButton onCredential={(c) => g.go(c)} />
        {g.picker}
        <ErrorText error={g.error} />
        <p className="muted center small"><button className="link" onClick={() => setByContact(true)}>Sign in with your Gmail or mobile number instead</button></p>
        </>}
        <p className="muted center">New restaurant? <Link to="/signup">Create your account</Link></p>
        <SupportLine />
      </div>
    </div>
  );
}

/** Owner / manager sign-in without the login ID: the business is found from the Gmail or mobile number. */
function ContactSignIn({ onBack }: { onBack: () => void }) {
  const { signedIn } = useSession();
  const nav = useNavigate();
  const [identifier, setIdentifier] = useState("");
  const [otp, setOtp] = useState("");
  const [sent, setSent] = useState<{ sent_to: string; dev_otp: string | null } | null>(null);
  const { busy, error, run } = useAction();
  const send = () => run(async () => setSent(await api("/auth/otp/request", { body: { identifier } })));
  if (!sent) return (
    <form onSubmit={(e) => { e.preventDefault(); send(); }}>
      <Field label="Gmail or mobile number"><Input value={identifier} onChange={(e) => setIdentifier(e.target.value)} inputMode="email" autoFocus required /></Field>
      <p className="muted small">A code comes by email to a Gmail, or on WhatsApp to a mobile number.</p>
      <ErrorText error={error} />
      <Button disabled={busy}>Send code</Button>{" "}
      <button type="button" className="link" onClick={onBack}>Use the login ID</button>
    </form>
  );
  return (
    <form onSubmit={(e) => { e.preventDefault(); run(async () => { await signedIn(await api<TokenOut>("/auth/otp/verify", { body: { identifier, otp } })); nav("/"); }); }}>
      <p className="muted">If that account exists, we sent a 6-digit code to {sent.sent_to}.</p>
      {sent.dev_otp && <p className="dev-note">Development mode code: <strong>{sent.dev_otp}</strong></p>}
      <Field label="Code"><Input value={otp} onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" autoFocus /></Field>
      <ErrorText error={error} />
      <Button disabled={busy || otp.length !== 6}>Sign in</Button>{" "}
      <button type="button" className="link" disabled={busy} onClick={() => { setOtp(""); send(); }}>Send a new code</button>
      {" · "}<button type="button" className="link" onClick={() => { setSent(null); setOtp(""); }}>Change</button>
    </form>
  );
}
