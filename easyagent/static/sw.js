/* EasyAgent shell. Chats are never cached. */
const CACHE = "easyagent-shell-0.3.0";
const SHELL = ["/", "/manifest.webmanifest", "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/face.svg", "/static/mascot.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api")) return;
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok && (url.pathname === "/" || url.pathname.startsWith("/ui/") || url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest")) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request)),
  );
});
