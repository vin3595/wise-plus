// Files shared into Ease My Bill from Android's share sheet (the service worker keeps them in a cache for us).
const SHARE = "emb-share";

export async function sharedFiles(): Promise<File[]> {
  if (!("caches" in window)) return [];
  const c = await caches.open(SHARE);
  const keys = [...(await c.keys())].sort((a: Request, b: Request) => a.url.localeCompare(b.url, undefined, { numeric: true }));
  const out: File[] = [];
  for (const k of keys) {
    const r = await c.match(k);
    if (!r) continue;
    const blob = await r.blob();
    out.push(new File([blob], decodeURIComponent(r.headers.get("X-Name") || "shared"), { type: blob.type || r.headers.get("Content-Type") || "" }));
  }
  return out;
}

export async function clearShared(): Promise<void> {
  if ("caches" in window) await caches.delete(SHARE);
}
