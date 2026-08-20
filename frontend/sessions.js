"use strict";

async function loadSessions() {
  const body = document.getElementById("sessions-body");
  const empty = document.getElementById("sessions-empty");
  try {
    const res = await apiGet("/sessions?limit=100");
    const rows = res.sessions || [];
    body.innerHTML = "";
    empty.style.display = rows.length ? "none" : "block";
    rows.forEach((s) => body.appendChild(sessionRow(s)));
  } catch (e) {
    toast("Failed to load sessions: " + e.message);
  }
}

function sessionRow(s) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td>${escapeHtml(s.title || "(untitled)")}</td>
    <td>${escapeHtml(s.profile || "default")}</td>
    <td>${escapeHtml(s.model || "-")}</td>
    <td>${s.message_count ?? 0}</td>
    <td>${fmtTime(s.last_activity_at || s.started_at)}</td>
  `;
  const actionsTd = document.createElement("td");
  const row = document.createElement("div");
  row.className = "row-actions";
  const delBtn = document.createElement("button");
  delBtn.innerHTML = icon("trash", 14);
  delBtn.title = "Delete session";
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Delete session "${s.title || s.id}"?`)) return;
    try {
      await apiSend("DELETE", `/sessions/${s.id}?profile=${encodeURIComponent(s.profile || "default")}`);
      toast("Session deleted");
      await loadSessions();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  row.appendChild(delBtn);
  actionsTd.appendChild(row);
  tr.appendChild(actionsTd);
  return tr;
}

renderShell("sessions");
loadSessions();
