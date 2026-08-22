"use strict";

async function loadServers() {
  const list = document.getElementById("mcp-list");
  try {
    const res = await apiGet("/mcp/servers");
    const servers = res.servers || [];
    list.innerHTML = "";
    if (!servers.length) {
      list.innerHTML = '<div class="empty-state">No MCP servers configured.</div>';
      return;
    }
    const grid = document.createElement("div");
    grid.className = "card-grid";
    servers.forEach((s) => grid.appendChild(serverCard(s)));
    list.appendChild(grid);
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
  }
}

function serverCard(s) {
  const card = document.createElement("div");
  card.className = "item-card";

  const title = document.createElement("div");
  title.className = "item-card-title";
  title.style.display = "flex";
  title.style.alignItems = "center";
  title.style.gap = "0.4rem";
  title.textContent = s.name;
  const pill = document.createElement("span");
  pill.className = "pill " + (s.enabled ? "on" : "off");
  pill.textContent = s.enabled ? "Enabled" : "Disabled";
  title.appendChild(pill);
  card.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "item-card-sub";
  sub.textContent = `${s.transport}${s.url ? " -- " + s.url : ""}${s.command ? " -- " + s.command : ""}${s.auth ? " -- auth: " + s.auth : ""}`;
  card.appendChild(sub);

  const actions = document.createElement("div");
  actions.className = "item-card-actions";

  const testBtn = document.createElement("button");
  testBtn.className = "btn btn-icon-label";
  testBtn.innerHTML = icon("check", 14) + "<span>Test</span>";
  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    try {
      const res = await apiSend("POST", `/mcp/servers/${encodeURIComponent(s.name)}/test`);
      toast(res.ok || res.success ? "Connection OK" : (res.message || res.error || "Test failed"));
    } catch (e) {
      toast("Test failed: " + e.message);
    } finally {
      testBtn.disabled = false;
    }
  });

  const toggleBtn = document.createElement("button");
  toggleBtn.className = "btn btn-icon-label";
  toggleBtn.innerHTML = icon(s.enabled ? "pause" : "play", 14) + `<span>${s.enabled ? "Disable" : "Enable"}</span>`;
  toggleBtn.addEventListener("click", async () => {
    try {
      await apiSend("PUT", `/mcp/servers/${encodeURIComponent(s.name)}/enabled`, { enabled: !s.enabled });
      toast(`${s.name} ${s.enabled ? "disabled" : "enabled"}`);
      await loadServers();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });

  const delBtn = document.createElement("button");
  delBtn.className = "btn danger btn-icon-label";
  delBtn.innerHTML = icon("trash", 14) + "<span>Remove</span>";
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Remove MCP server "${s.name}"?`)) return;
    try {
      await apiSend("DELETE", `/mcp/servers/${encodeURIComponent(s.name)}`);
      toast("Removed");
      await loadServers();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });

  actions.append(testBtn, toggleBtn, delBtn);
  card.appendChild(actions);
  return card;
}

document.getElementById("mcp-transport").addEventListener("change", (e) => {
  const isHttp = e.target.value === "http";
  document.getElementById("mcp-url-field").style.display = isHttp ? "" : "none";
  document.getElementById("mcp-command-field").style.display = isHttp ? "none" : "";
});
document.getElementById("mcp-auth").addEventListener("change", (e) => {
  document.getElementById("mcp-token-field").style.display = e.target.value === "header" ? "" : "none";
});

document.getElementById("add-mcp-btn").addEventListener("click", () => {
  document.getElementById("mcp-form").reset();
  document.getElementById("mcp-url-field").style.display = "";
  document.getElementById("mcp-command-field").style.display = "none";
  document.getElementById("mcp-token-field").style.display = "none";
  document.getElementById("mcp-modal-backdrop").classList.add("open");
});
document.getElementById("mcp-modal-cancel").addEventListener("click", () => {
  document.getElementById("mcp-modal-backdrop").classList.remove("open");
});
document.getElementById("mcp-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "mcp-modal-backdrop") e.target.classList.remove("open");
});

document.getElementById("mcp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const transport = document.getElementById("mcp-transport").value;
  const auth = document.getElementById("mcp-auth").value;
  const body = {
    name: document.getElementById("mcp-name").value.trim(),
    url: transport === "http" ? document.getElementById("mcp-url").value.trim() : null,
    command: transport === "stdio" ? document.getElementById("mcp-command").value.trim() : null,
    auth: auth || null,
    bearer_token: auth === "header" ? document.getElementById("mcp-token").value || null : null,
  };
  try {
    await apiSend("POST", "/mcp/servers", body);
    toast(`${body.name} saved`);
    document.getElementById("mcp-modal-backdrop").classList.remove("open");
    await loadServers();
  } catch (err) {
    toast("Failed: " + err.message);
  }
});

// ---------------------------------------------------------------------
// Catalog -- Nous-approved integrations, browse-and-click like the real
// Hermes desktop app's "Add integration" button. Reuses GET /api/mcp/catalog
// and POST /api/mcp/catalog/install (proxied through zBots' own backend),
// the exact endpoints that power the desktop client, plus the same
// dashboard OAuth flow (POST .../auth -> open authorization_url -> poll
// GET /api/mcp/oauth/flows/{id} for status) for entries that need it.
// ---------------------------------------------------------------------

let pendingCatalogEntry = null;

async function loadCatalog() {
  const list = document.getElementById("catalog-list");
  list.className = "loading-hint";
  list.textContent = "Loading...";
  try {
    const res = await apiGet("/mcp/catalog");
    const entries = res.entries || [];
    list.className = "";
    list.innerHTML = "";
    if (!entries.length) {
      list.innerHTML = '<div class="empty-state">No catalog entries available.</div>';
      return;
    }
    const grid = document.createElement("div");
    grid.className = "card-grid";
    entries.forEach((e) => grid.appendChild(catalogCard(e)));
    list.appendChild(grid);
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load catalog: ${e.message}</div>`;
  }
}

function catalogCard(entry) {
  const card = document.createElement("div");
  card.className = "item-card";

  const title = document.createElement("div");
  title.className = "item-card-title";
  title.style.display = "flex";
  title.style.alignItems = "center";
  title.style.gap = "0.4rem";
  title.textContent = entry.name;
  if (entry.enabled) {
    const pill = document.createElement("span");
    pill.className = "pill on";
    pill.textContent = "Connected";
    title.appendChild(pill);
  } else if (entry.installed) {
    const pill = document.createElement("span");
    pill.className = "pill off";
    pill.textContent = "Installed";
    title.appendChild(pill);
  }
  card.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "item-card-sub";
  sub.textContent = entry.description || "";
  card.appendChild(sub);

  const actions = document.createElement("div");
  actions.className = "item-card-actions";

  if (entry.enabled) {
    const disableBtn = document.createElement("button");
    disableBtn.className = "btn btn-icon-label";
    disableBtn.innerHTML = icon("pause", 14) + "<span>Disable</span>";
    disableBtn.addEventListener("click", async () => {
      try {
        await apiSend("PUT", `/mcp/servers/${encodeURIComponent(entry.name)}/enabled`, { enabled: false });
        toast(`${entry.name} disabled`);
        await loadCatalog();
        await loadServers();
      } catch (e) {
        toast("Failed: " + e.message);
      }
    });
    actions.appendChild(disableBtn);
  } else {
    const connectBtn = document.createElement("button");
    connectBtn.className = "btn primary btn-icon-label";
    const label = entry.auth_type === "oauth" ? "Connect" : "Install";
    connectBtn.innerHTML = icon("plus", 14) + `<span>${label}</span>`;
    connectBtn.addEventListener("click", () => startCatalogInstall(entry, connectBtn));
    actions.appendChild(connectBtn);
  }

  card.appendChild(actions);
  return card;
}

async function startCatalogInstall(entry, btn) {
  if (entry.required_env && entry.required_env.length) {
    openCatalogEnvModal(entry);
    return;
  }
  btn.disabled = true;
  try {
    await installCatalogEntry(entry.name, {});
    if (entry.auth_type === "oauth") {
      await runCatalogOAuth(entry.name);
    } else {
      toast(`${entry.name} installed`);
    }
    await loadCatalog();
    await loadServers();
  } catch (e) {
    toast("Failed: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function installCatalogEntry(name, env) {
  const res = await apiSend("POST", "/mcp/catalog/install", { name, env, enable: true });
  if (res.background) {
    toast(`${name} is installing in the background -- refresh the catalog in a bit`);
  }
  return res;
}

async function runCatalogOAuth(name) {
  const flow = await apiSend("POST", `/mcp/servers/${encodeURIComponent(name)}/auth`);
  if (flow.error) throw new Error(flow.error);
  if (flow.authorization_url) {
    window.open(flow.authorization_url, "_blank", "noopener,noreferrer");
  } else {
    toast(`${name}: waiting on authorization...`);
  }
  await pollOAuthFlow(flow.flow_id, name);
}

async function pollOAuthFlow(flowId, name) {
  if (!flowId) return;
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    let status;
    try {
      status = await apiGet(`/mcp/oauth/flows/${encodeURIComponent(flowId)}`);
    } catch (e) {
      break; // flow expired/gone server-side -- stop polling quietly
    }
    if (status.status === "approved") {
      toast(`${name} connected`);
      return;
    }
    if (status.status === "error") {
      toast(`${name}: ${status.error || "authorization failed"}`);
      return;
    }
  }
  toast(`${name}: still waiting on authorization -- check back in the catalog`);
}

function openCatalogEnvModal(entry) {
  pendingCatalogEntry = entry;
  document.getElementById("catalog-env-title").textContent = `Set up ${entry.name}`;
  document.getElementById("catalog-env-sub").textContent = entry.description || "";
  const fields = document.getElementById("catalog-env-fields");
  fields.innerHTML = "";
  entry.required_env.forEach((envVar) => {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = envVar.prompt || envVar.name;
    label.setAttribute("for", `catalog-env-${envVar.name}`);
    const input = document.createElement("input");
    input.type = "password";
    input.id = `catalog-env-${envVar.name}`;
    if (envVar.required) input.required = true;
    field.append(label, input);
    fields.appendChild(field);
  });
  document.getElementById("catalog-env-modal-backdrop").classList.add("open");
}

document.getElementById("catalog-env-cancel").addEventListener("click", () => {
  document.getElementById("catalog-env-modal-backdrop").classList.remove("open");
  pendingCatalogEntry = null;
});
document.getElementById("catalog-env-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "catalog-env-modal-backdrop") {
    e.target.classList.remove("open");
    pendingCatalogEntry = null;
  }
});

document.getElementById("catalog-env-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!pendingCatalogEntry) return;
  const entry = pendingCatalogEntry;
  const env = {};
  entry.required_env.forEach((envVar) => {
    const input = document.getElementById(`catalog-env-${envVar.name}`);
    if (input && input.value) env[envVar.name] = input.value;
  });
  document.getElementById("catalog-env-modal-backdrop").classList.remove("open");
  try {
    await installCatalogEntry(entry.name, env);
    if (entry.auth_type === "oauth") {
      await runCatalogOAuth(entry.name);
    } else {
      toast(`${entry.name} installed`);
    }
    await loadCatalog();
    await loadServers();
  } catch (err) {
    toast("Failed: " + err.message);
  }
  pendingCatalogEntry = null;
});

document.getElementById("browse-catalog-btn").addEventListener("click", () => {
  document.getElementById("catalog-modal-backdrop").classList.add("open");
  loadCatalog();
});
document.getElementById("catalog-modal-close").addEventListener("click", () => {
  document.getElementById("catalog-modal-backdrop").classList.remove("open");
});
document.getElementById("catalog-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "catalog-modal-backdrop") e.target.classList.remove("open");
});

renderShell("mcp");
document.getElementById("add-mcp-btn").innerHTML = icon("plus", 15) + "<span>Add server</span>";
document.getElementById("browse-catalog-btn").innerHTML = icon("system", 15) + "<span>Browse catalog</span>";
loadServers();
