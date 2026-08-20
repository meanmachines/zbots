"use strict";

// Shared helpers for every admin page (Bots' app.js has its own copy of
// these predating this file -- kept in sync manually, not worth a bundler
// for this small an app).

const API = "/bots-api";

async function api(path, opts) {
  const res = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}
const apiGet = (path) => api(path);
const apiSend = (method, path, body) => api(path, { method, body: body !== undefined ? JSON.stringify(body) : undefined });

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("open");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("open"), 3200);
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString();
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
