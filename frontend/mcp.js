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

renderShell("mcp");
document.getElementById("add-mcp-btn").innerHTML = icon("plus", 15) + "<span>Add server</span>";
loadServers();
