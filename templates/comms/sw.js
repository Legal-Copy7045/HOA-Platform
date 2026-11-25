// Service worker for the installable app (Milestone 4).
// It caches the site's static files only (styles, scripts, icons). Pages,
// downloads and anything else that can hold personal data are never cached
// or touched: those requests go straight to the network as usual.
"use strict";
const CONFIG = {{ config|safe }};

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CONFIG.cache)
      .then(function (cache) { return cache.addAll(CONFIG.precache); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CONFIG.cache; })
          .map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(CONFIG.staticPrefix)) return;
  event.respondWith(
    caches.match(request).then(function (hit) {
      if (hit) return hit;
      return fetch(request).then(function (response) {
        if (response.ok && response.type === "basic") {
          const copy = response.clone();
          caches.open(CONFIG.cache).then(function (cache) { cache.put(request, copy); });
        }
        return response;
      });
    })
  );
});

// A push carries a title and the path of a signed-in page, nothing more.
function safePath(value) {
  return typeof value === "string" && value.charAt(0) === "/" && value.charAt(1) !== "/" &&
    value.indexOf("\\") === -1 ? value : "/";
}

self.addEventListener("push", function (event) {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const title = typeof data.title === "string" && data.title ? data.title.slice(0, 80) : CONFIG.siteName;
  event.waitUntil(self.registration.showNotification(title, {
    body: "Sign in to " + CONFIG.siteName + " to read it.",
    icon: CONFIG.icon,
    data: { url: safePath(data.url) }
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const target = new URL(safePath(event.notification.data && event.notification.data.url), self.location.origin);
  event.waitUntil(self.clients.openWindow(target.href));
});
