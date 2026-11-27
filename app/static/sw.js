// Service worker for the installable app (PWA).
//
// Only the app shell (the page, scripts, styles and icons) is cached, so the
// app opens quickly and shows an "offline" message instead of a browser error.
// API responses are NEVER cached: they hold your private story and depend on
// who is logged in. Pages are fetched from the network first, so a new
// version of the app (or a login page) is always used when online.
const CACHE = "story-helper-v1";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/manifest.webmanifest",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth") ||
      url.pathname.startsWith("/login") || url.pathname.startsWith("/logout")) return; // never cached
  e.respondWith(
    fetch(req).then((res) => {
      // keep a copy of the shell for offline use; skip redirects (e.g. to a login page) and errors
      if (res.ok && !res.redirected && res.type === "basic" && SHELL.includes(url.pathname)) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req).then((hit) => hit ||
      new Response("<h1>Offline</h1><p>Story Helper needs a connection to your server.</p>",
        { status: 503, headers: { "Content-Type": "text/html" } })))
  );
});
