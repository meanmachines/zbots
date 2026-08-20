"use strict";

async function loadSystem() {
  const el = document.getElementById("system-content");
  let res;
  try {
    res = await apiGet("/system");
  } catch (e) {
    el.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
    return;
  }
  const s = res.status || {};
  const stats = res.stats || {};
  el.innerHTML = "";

  const grid = document.createElement("div");
  grid.className = "card-grid";

  const cards = [
    { title: "Gateway", rows: [["State", s.gateway_state], ["Version", s.version], ["Active sessions", s.active_sessions], ["Active agents", s.active_agents]] },
    { title: "Host", rows: [["OS", stats.os_release || stats.os], ["Uptime", stats.uptime_seconds ? Math.round(stats.uptime_seconds / 3600) + " h" : "-"], ["Load avg", (stats.load_avg || []).map((n) => n.toFixed(2)).join(", ")]] },
    { title: "CPU / Memory", rows: [["CPU", (stats.cpu_percent ?? "-") + "%"], ["Memory used", fmtBytes(stats.memory && stats.memory.used) + " / " + fmtBytes(stats.memory && stats.memory.total)], ["Memory %", (stats.memory && stats.memory.percent != null ? stats.memory.percent + "%" : "-")]] },
    { title: "Disk", rows: [["Used", fmtBytes(stats.disk && stats.disk.used) + " / " + fmtBytes(stats.disk && stats.disk.total)], ["Free", fmtBytes(stats.disk && stats.disk.free)], ["Used %", (stats.disk && stats.disk.percent != null ? stats.disk.percent + "%" : "-")]] },
  ];

  cards.forEach((c) => {
    const card = document.createElement("div");
    card.className = "item-card";
    let html = `<div class="item-card-title">${escapeHtml(c.title)}</div>`;
    c.rows.forEach(([label, value]) => {
      html += `<div style="display:flex; justify-content:space-between; font-size:0.82rem; padding:0.25rem 0; border-bottom:1px solid var(--border);"><span style="color:var(--text-muted);">${escapeHtml(label)}</span><span>${escapeHtml(value ?? "-")}</span></div>`;
    });
    card.innerHTML = html;
    grid.appendChild(card);
  });

  el.appendChild(grid);
}

document.getElementById("restart-gateway-btn").addEventListener("click", async () => {
  if (!confirm("Restart the Hermes gateway? Active sessions will be interrupted.")) return;
  try {
    await apiSend("POST", "/system/restart-gateway");
    toast("Restart requested");
  } catch (e) {
    toast("Failed: " + e.message);
  }
});

renderShell("system");
document.getElementById("restart-gateway-btn").innerHTML = icon("system", 15) + "<span>Restart gateway</span>";
loadSystem();
