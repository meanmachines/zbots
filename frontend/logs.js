"use strict";

async function loadLogs() {
  const view = document.getElementById("logs-view");
  try {
    const res = await apiGet("/logs?lines=300");
    const lines = res.lines || [];
    view.textContent = lines.join("");
    view.scrollTop = view.scrollHeight;
  } catch (e) {
    view.textContent = "Failed to load logs: " + e.message;
  }
}

document.getElementById("refresh-logs-btn").addEventListener("click", loadLogs);

renderShell("logs");
document.getElementById("refresh-logs-btn").innerHTML = icon("clock", 15) + "<span>Refresh</span>";
loadLogs();
