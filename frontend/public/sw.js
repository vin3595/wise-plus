// App-shell service worker: the app opens even with no internet.
// API calls always go to the network (offline billing data sync arrives in Phase 2).
const CACHE = "emb-shell-v4";
const SHARE = "emb-share";   // files shared into the app from the Android share sheet, until /share picks them up   // new name: phones drop the old Wise icons
const SHELL = ["/", "/index.html", "/icon.svg", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== SHARE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === "POST" && url.pathname === "/share-target") {
    event.respondWith((async () => {
      const form = await event.request.formData();
      const files = form.getAll("files").filter((f) => f && typeof f !== "string");
      await caches.delete(SHARE);
      const c = await caches.open(SHARE);
      await Promise.all(files.slice(0, 30).map((f, i) => c.put(`/shared/${i}`, new Response(f, {
        headers: { "Content-Type": f.type || "application/octet-stream", "X-Name": encodeURIComponent(f.name || `shared-${i}`) } }))));
      return Response.redirect(`/share?n=${Math.min(files.length, 30)}`, 303);
    })());
    return;
  }
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() => caches.match("/index.html")));
    return;
  }
  event.respondWith(
    caches.match(event.request).then(
      (hit) =>
        hit ||
        fetch(event.request).then((res) => {
          if (res.ok && url.origin === location.origin) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(event.request, copy));
          }
          return res;
        })
    )
  );
});
