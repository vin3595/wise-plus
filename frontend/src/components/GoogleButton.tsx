import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";

// "Sign in with Google" (Google Identity Services). Shown only when the client ID is set in admin › Login codes.
// onCredential gets Google's ID token; the server checks it with Google.

declare global {
  interface Window { google?: any }   // eslint-disable-line @typescript-eslint/no-explicit-any
}

let clientId: Promise<string | null> | null = null;
let script: Promise<void> | null = null;

function loadScript(): Promise<void> {
  script ||= new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "https://accounts.google.com/gsi/client";
    s.async = true;
    s.onload = () => ok();
    s.onerror = () => { script = null; fail(new Error("Couldn't load Google sign-in")); };
    document.head.appendChild(s);
  });
  return script;
}

export function GoogleButton({ onCredential, text = "signin_with" }: { onCredential: (credential: string) => void; text?: "signin_with" | "signup_with" | "continue_with" }) {
  const box = useRef<HTMLDivElement>(null);
  const cb = useRef(onCredential);
  cb.current = onCredential;
  const [on, setOn] = useState(false);

  useEffect(() => {
    let live = true;
    clientId ||= api<{ client_id: string | null }>("/auth/google").then((r) => r.client_id).catch(() => null);
    clientId.then(async (id) => {
      if (!id || !live) return;
      setOn(true);
      await loadScript();
      if (!live || !box.current || !window.google) return;
      window.google.accounts.id.initialize({ client_id: id, callback: (r: { credential: string }) => cb.current(r.credential) });
      window.google.accounts.id.renderButton(box.current, { theme: "outline", size: "large", text, shape: "pill", width: 280 });
    }).catch(() => undefined);
    return () => { live = false; };
  }, [text]);

  if (!on) return null;
  return (
    <div style={{ margin: "12px 0" }}>
      <p className="muted center small" style={{ margin: "8px 0" }}>or</p>
      <div ref={box} style={{ display: "flex", justifyContent: "center", minHeight: 44 }} />
    </div>
  );
}

/** Name and Gmail from Google's sign-in token (only for showing; the server checks the token itself). */
export function googleProfile(cred: string): { email: string; name: string } {
  try {
    const p = JSON.parse(decodeURIComponent(escape(atob(cred.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")))));
    return { email: p.email || "", name: p.name || "" };
  } catch { return { email: "", name: "" }; }
}

/** Google sign-in: an existing business opens; a new Gmail goes straight to setting up its business;
 *  when one Gmail runs several businesses, asks which. */
export function useGoogleLogin(signedIn: (t: any) => Promise<void>, onNew?: (cred: string) => void) {   // eslint-disable-line @typescript-eslint/no-explicit-any
  const nav = useNavigate();
  const [choices, setChoices] = useState<{ cred: string; list: [string, string][] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const go = async (cred: string, login_id?: string) => {
    setError(null);
    try {
      await signedIn(await api("/auth/google", { body: { credential: cred, login_id } }));
      setChoices(null);
    } catch (e) {
      const msg = (e as Error).message || "";
      if (e instanceof ApiError && e.status === 404) { if (onNew) onNew(cred); else nav("/signup", { state: { credential: cred } }); return; }
      if (msg.startsWith("choose:")) {
        setChoices({ cred, list: msg.slice(7).split(",").map((x) => x.split("=") as [string, string]) });
      } else setError(msg);
    }
  };
  const picker = choices && (
    <div className="stack" style={{ gap: 8, margin: "12px 0" }}>
      <p className="muted small">This Gmail runs more than one business. Which one?</p>
      {choices.list.map(([id, name]) => <button key={id} className="btn ghost" onClick={() => go(choices.cred, id)}>{name} · {id}</button>)}
    </div>
  );
  return { go, picker, error };
}
