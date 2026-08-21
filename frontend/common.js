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
  const res = await fetch(API + path, { ...opts, headers: apiHeaders(opts && opts.headers) });
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
