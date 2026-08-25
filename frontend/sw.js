"use strict";

// Service worker for real Web Push notifications (routine/delegated-task
// deliveries -- see backend/push.py's own module docstring for why this
// needs to be real Web Push, not a same-tab Notification() call: it has
// to fire even when this tab isn't open or focused).
//
// Scope is /bots/ (wherever this file is served from -- a service worker's
// default scope is its own directory and everything below it), which is
// the whole app's own root, so this is fine as-is.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = { title: "zBots", body: "" };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (_) {
    // Real bug this guards against: a push payload that isn't valid JSON
    // (shouldn't happen -- backend/push.py always sends JSON -- but a
    // malformed/corrupted delivery must never throw inside this handler,
    // which would silently drop the notification with no visible sign
    // anything went wrong).
    if (event.data) data.body = event.data.text();
  }
  // No icon/badge -- zBots ships no image assets at all (every icon in
  // the app itself is CSS-rendered, see icons.js), so this intentionally
  // omits both rather than reference a file that doesn't exist; the OS
  // falls back to its own default notification icon.
  const url = data.url || "/bots/";
  event.waitUntil(
    self.registration.showNotification(data.title || "zBots", {
      body: data.body || "",
      data: { url },
      tag: url, // a second notification for the same bot's chat replaces the first, not stacks
    })
  );
});

// Clicking the notification focuses an already-open zBots tab if one
// exists, instead of always opening a new one.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/bots/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes("/bots/") && "focus" in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
