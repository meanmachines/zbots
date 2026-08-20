"use strict";

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

let providers = [];
let currentModel = {};

async function refreshProviders() {
  const list = document.getElementById("providers-list");
  try {
    const res = await apiGet("/providers");
    providers = res.providers || [];
    currentModel = res.current || {};
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load providers: ${e.message}</div>`;
    return;
  }
  renderProviders();
}

function renderProviders() {
  const list = document.getElementById("providers-list");
  list.innerHTML = "";
  if (!providers.length) {
    list.innerHTML = '<div class="empty-state">No providers configured yet.</div>';
    return;
  }
  const grid = document.createElement("div");
  grid.className = "card-grid";
  providers.forEach((p) => grid.appendChild(providerCard(p)));
  list.appendChild(grid);
}

function providerCard(p) {
  const card = document.createElement("div");
  card.className = "item-card";

  const title = document.createElement("div");
  title.className = "item-card-title";
  title.style.display = "flex";
  title.style.alignItems = "center";
  title.style.gap = "0.4rem";
  title.textContent = p.name;
  if (p.is_current) {
    const pill = document.createElement("span");
    pill.className = "pill on";
    pill.textContent = "Active";
    title.appendChild(pill);
  }
  card.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "item-card-sub";
  sub.textContent = p.base_url + (p.has_api_key ? " -- API key set" : " -- no API key");
  card.appendChild(sub);

  const modelsWrap = document.createElement("div");
  (p.models.length ? p.models : ["(no models discovered)"]).forEach((m) => {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.alignItems = "center";
    row.style.justifyContent = "space-between";
    row.style.padding = "0.3rem 0";
    row.style.fontSize = "0.82rem";

    const name = document.createElement("span");
    name.textContent = m;
    row.appendChild(name);

    const isActiveModel = currentModel.provider === p.id && currentModel.model === m;
    if (isActiveModel) {
      const pill = document.createElement("span");
      pill.className = "pill on";
      pill.textContent = "In use";
      row.appendChild(pill);
    } else {
      const setBtn = document.createElement("button");
      setBtn.className = "btn";
      setBtn.style.padding = "0.15rem 0.5rem";
      setBtn.style.fontSize = "0.72rem";
      setBtn.textContent = "Set active";
      setBtn.addEventListener("click", async () => {
        try {
          await apiSend("POST", "/models/activate", { provider: p.id, model: m });
          toast(`${m} is now active`);
          await refreshProviders();
        } catch (e) {
          toast("Failed: " + e.message);
        }
      });
      row.appendChild(setBtn);
    }
    modelsWrap.appendChild(row);
  });
  card.appendChild(modelsWrap);

  const actions = document.createElement("div");
  actions.className = "item-card-actions";

  const delBtn = document.createElement("button");
  delBtn.className = "btn danger btn-icon-label";
  delBtn.innerHTML = icon("trash", 15) + "<span>Delete</span>";
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Delete provider "${p.name}"?`)) return;
    try {
      await apiSend("DELETE", `/providers/${p.id}`);
      toast("Provider deleted");
      await refreshProviders();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  actions.appendChild(delBtn);
  card.appendChild(actions);

  return card;
}

// ---------------------------------------------------------------------
// Add provider modal
// ---------------------------------------------------------------------

let discoveredModels = null;

function openProviderModal() {
  document.getElementById("provider-form").reset();
  document.getElementById("provider-discover").checked = true;
  document.getElementById("provider-validate-result").textContent = "";
  discoveredModels = null;
  document.getElementById("provider-modal-backdrop").classList.add("open");
}

document.getElementById("add-provider-btn").addEventListener("click", openProviderModal);
document.getElementById("provider-modal-cancel").addEventListener("click", () => {
  document.getElementById("provider-modal-backdrop").classList.remove("open");
});
document.getElementById("provider-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "provider-modal-backdrop") e.target.classList.remove("open");
});

function providerFormValues() {
  return {
    id: document.getElementById("provider-id").value.trim().toLowerCase(),
    name: document.getElementById("provider-name").value.trim() || document.getElementById("provider-id").value.trim(),
    base_url: document.getElementById("provider-base-url").value.trim(),
    model: document.getElementById("provider-model").value.trim(),
    api_key: document.getElementById("provider-api-key").value || undefined,
    discover_models: document.getElementById("provider-discover").checked,
    make_default: document.getElementById("provider-make-default").checked,
  };
}

document.getElementById("provider-validate-btn").addEventListener("click", async () => {
  const v = providerFormValues();
  const resultEl = document.getElementById("provider-validate-result");
  resultEl.textContent = "Testing...";
  resultEl.style.color = "var(--text-muted)";
  try {
    const res = await apiSend("POST", "/providers/validate", { name: v.name, base_url: v.base_url, model: v.model, api_key: v.api_key });
    if (res.ok) {
      resultEl.style.color = "var(--success)";
      discoveredModels = res.models && res.models.length ? res.models : null;
      resultEl.textContent = `Reachable. ${discoveredModels ? discoveredModels.length + " model(s) found -- all will be added." : ""}`;
    } else {
      resultEl.style.color = "var(--danger)";
      resultEl.textContent = res.message || "Validation failed.";
    }
  } catch (e) {
    resultEl.style.color = "var(--danger)";
    resultEl.textContent = "Failed: " + e.message;
  }
});

document.getElementById("provider-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = providerFormValues();
  // Validate isn't required before saving, but if it ran and discovered
  // models, use that list -- otherwise the endpoint only ever gets the one
  // primary model, silently ignoring "discover" checkbox regardless of its
  // state (Hermes' own save endpoint doesn't re-probe the endpoint itself).
  if (v.discover_models && discoveredModels) {
    v.models = discoveredModels;
  }
  try {
    await apiSend("POST", "/providers", v);
    toast(`Provider "${v.name}" saved`);
    document.getElementById("provider-modal-backdrop").classList.remove("open");
    await refreshProviders();
  } catch (err) {
    toast("Failed: " + err.message);
  }
});

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

function boot() {
  renderShell("models");
  document.getElementById("add-provider-btn").innerHTML = icon("plus", 15) + "<span>Add provider</span>";
  refreshProviders();
}

boot();
