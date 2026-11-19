import { useState } from "react";
import { useSession } from "../session";

/** "Login ID: 482913 · Copy" — staff type it once on each phone or tablet. */
export default function LoginIdLine({ big = false }: { big?: boolean }) {
  const { me } = useSession();
  const [copied, setCopied] = useState(false);
  const id = me?.tenant.login_id;
  if (!id) return null;
  const copy = async () => { try { await navigator.clipboard.writeText(id); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* no clipboard */ } };
  return (
    <p className={big ? "" : "small muted"} style={{ margin: 0 }}>
      {me?.tenant.name} · Login ID: <strong style={{ letterSpacing: big ? "0.1em" : undefined, fontSize: big ? "1.3em" : undefined }}>{id}</strong>{" "}
      <button className="link small" onClick={copy}>{copied ? "Copied ✓" : "Copy"}</button>
      {big && <span className="small muted" style={{ display: "block" }}>Staff type this once on each phone or tablet, then sign in with their PIN or mobile.</span>}
    </p>
  );
}
