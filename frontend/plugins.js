"use strict";

async function loadPlugins() {
  const list = document.getElementById("plugins-list");
  try {
    const plugins = (await apiGet("/plugins")) || [];
    list.innerHTML = "";
    if (!plugins.length) {
      list.innerHTML = '<div class="empty-state">No plugins installed.</div>';
      return;
    }
    const grid = document.createElement("div");
    grid.className = "card-grid";
    plugins.forEach((p) => {
      const card = document.createElement("div");
      card.className = "item-card";
      card.innerHTML = `
        <div class="item-card-title">${escapeHtml(p.label || p.name)}</div>
        <div class="item-card-sub">${escapeHtml(p.description || "")}</div>
        <div style="font-size:0.72rem; color:var(--text-muted);">v${escapeHtml(p.version || "-")} -- ${escapeHtml(p.source || "")}</div>
      `;
      grid.appendChild(card);
    });
    list.appendChild(grid);
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
  }
}

renderShell("plugins");
loadPlugins();
