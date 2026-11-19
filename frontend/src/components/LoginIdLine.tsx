import { useSession } from "../session";

/** "Shakewallah · Login ID: shakewallah" — staff type it once on each phone or tablet. */
export default function LoginIdLine({ big = false }: { big?: boolean }) {
  const { me } = useSession();
  const id = me?.tenant.login_id;
  if (!id) return null;
  return (
    <p className={big ? "" : "small muted"} style={{ margin: 0 }}>
      {me?.tenant.name} · Login ID: <strong style={{ letterSpacing: big ? "0.1em" : undefined, fontSize: big ? "1.3em" : undefined }}>{id}</strong>
      {big && <span className="small muted" style={{ display: "block" }}>Staff type this once on each phone or tablet, then sign in with their PIN or mobile.</span>}
    </p>
  );
}
