"use strict";

// Shared helpers for every page (Bots' app.js loads this file too -- the old
// per-page copies of these helpers have been removed so behavior stays in one
// place).

const API = "/bots-api";

// Standalone deployments can set BOTS_UI_API_KEY in the backend and inject it
// into the frontend as window.BOTS_UI_KEY (see the standalone entrypoint).
// When present, every API call carries it as a Bearer token.
function apiHeaders(extra) {
  const headers = { "Content-Type": "application/json", ...(extra || {}) };
  if (window.BOTS_UI_KEY) headers["Authorization"] = "Bearer " + window.BOTS_UI_KEY;
  return headers;
}

async function api(path, opts) {
  const res = await fetch(API + path, { headers: apiHeaders(opts && opts.headers), ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

const apiGet = (path) => api(path);
const apiSend = (method, path, body) =>
  api(path, { method, body: body !== undefined ? JSON.stringify(body) : undefined });

function toast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("open");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("open"), 3200);
}

// ---------------------------------------------------------------------
// Push notifications -- real Web Push for routine/delegated-task
// deliveries (see backend/push.py's own module docstring for why: works
// even when this tab isn't open or focused, unlike a same-tab
// Notification() call). Deliberately opt-in (a button click, not an
// auto-prompt on page load) -- an unsolicited permission prompt on first
// visit is a real anti-pattern browsers increasingly penalize (Chrome's
// own abuse heuristics can auto-block an origin that gets dismissed too
// often), and this app has no way to know if THIS is a first visit.
// ---------------------------------------------------------------------

function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

// Web Push's own applicationServerKey wants a Uint8Array, not the
// base64url string the backend hands back -- standard conversion, same
// shape every Web Push guide/MDN's own example uses.
function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function getPushSubscription() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/bots/");
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

async function isPushEnabled() {
  return !!(await getPushSubscription());
}

async function enablePushNotifications() {
  if (!pushSupported()) {
    toast("This browser doesn't support push notifications");
    return false;
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    toast("Notification permission was not granted");
    return false;
  }
  const reg = await navigator.serviceWorker.register("/bots/sw.js", { scope: "/bots/" });
  await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    const { key } = await apiGet("/push/vapid-public-key");
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
  }
  await apiSend("POST", "/push/subscribe", { subscription: sub.toJSON() });
  toast("Reminder notifications enabled");
  return true;
}

async function disablePushNotifications() {
  const sub = await getPushSubscription();
  if (!sub) return;
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  try {
    await apiSend("POST", "/push/unsubscribe", { endpoint });
  } catch (_) {
    // Best-effort -- the browser-side unsubscribe above already
    // succeeded (no more notifications will arrive locally either way);
    // a failure telling the backend to forget the subscription just
    // means one stale entry pruned on its next failed send instead.
  }
  toast("Reminder notifications disabled");
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString();
}

function fmtDateTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString();
}

function dayLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((startOfDay(today) - startOfDay(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function fmtBytes(n) {
  if (n == null) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(1)} ${units[i]}`;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  // Fallback for older/non-secure contexts.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
  return Promise.resolve();
}

function downloadTextFile(filename, text, mime) {
  const blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
