const CACHE = "goldcalc-v1";
const ASSETS = [
  "/",
  "/trend",
  "/calculator",
  "/zakat",
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-256.png",
  "/static/icons/icon-384.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-180.png"
  // Add your real CSS/JS bundles here for reliable offline
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.headers.get("accept")?.includes("text/html")) {
    // network first for pages
    e.respondWith(
      fetch(req).then(res => {
        caches.open(CACHE).then(c => c.put(req, res.clone()));
        return res;
      }).catch(() => caches.match(req))
    );
  } else {
    // cache first for static assets
    e.respondWith(
      caches.match(req).then(cached => cached || fetch(req).then(res => {
        caches.open(CACHE).then(c => c.put(req, res.clone()));
        return res;
      }))
    );
  }
});
