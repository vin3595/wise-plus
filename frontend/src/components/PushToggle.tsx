import { useEffect, useState } from "react";
import { api } from "../api";
import { Button } from "../ui";

/** Phone notifications: QR orders, alerts, day close and tonight's summary, even when the app is closed. */
const supported = () => typeof window !== "undefined" && !(window as { embDesktop?: unknown }).embDesktop && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function keyBytes(b64: string): Uint8Array {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export async function turnOnPush(): Promise<void> {
  const perm = await Notification.requestPermission();
  if (perm !== "granted") throw new Error("Notifications are blocked. Allow them for this site in your phone's settings.");
  const reg = await navigator.serviceWorker.ready;
  const { key } = await api<{ key: string }>("/push/key");
  const sub = (await reg.pushManager.getSubscription())
    || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) as BufferSource });
  const j = sub.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } };
  await api("/push/subscribe", { body: { endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth } });
}

export default function PushToggle({ compact = false }: { compact?: boolean }) {
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (!supported()) { setOn(null); return; }
    navigator.serviceWorker.ready.then((r) => r.pushManager.getSubscription()).then((s) => setOn(!!s && Notification.permission === "granted")).catch(() => setOn(false));
  }, []);
  if (!supported() || on === null) return null;
  if (on && compact) return null;
  const go = async () => {
    setBusy(true); setMsg(null);
    try { await turnOnPush(); setOn(true); await api("/push/test", { body: {} }).catch(() => {}); setMsg("On ✓ You'll get QR orders, alerts and the day's summary here."); }
    catch (e) { setMsg(e instanceof Error ? e.message : "Couldn't turn notifications on"); }
    finally { setBusy(false); }
  };
  const off = async () => {
    setBusy(true);
    try {
      const r = await navigator.serviceWorker.ready; const s = await r.pushManager.getSubscription();
      if (s) { await api("/push/unsubscribe", { body: { endpoint: s.endpoint } }).catch(() => {}); await s.unsubscribe(); }
      setOn(false); setMsg("Notifications are off on this phone.");
    } finally { setBusy(false); }
  };
  return (
    <div className="banner info" role="status">
      {on ? <>🔔 Phone notifications are on. <button className="link" disabled={busy} onClick={off}>Turn off</button></>
        : <>🔔 Get QR orders, alerts and tonight's summary as phone notifications. <Button disabled={busy} onClick={go}>{busy ? "Turning on…" : "Turn on"}</Button></>}
      {msg && <div className="small">{msg}</div>}
    </div>
  );
}
