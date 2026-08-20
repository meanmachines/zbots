"use strict";

async function loadJobs() {
  const list = document.getElementById("cron-list");
  try {
    const jobs = (await apiGet("/cron")) || [];
    list.innerHTML = "";
    if (!jobs.length) {
      list.innerHTML = '<div class="empty-state">No routines scheduled.</div>';
      return;
    }
    const table = document.createElement("table");
    table.className = "data-table";
    table.innerHTML = "<thead><tr><th>Name</th><th>Profile</th><th>Schedule</th><th>Status</th><th>Next run</th><th></th></tr></thead>";
    const tbody = document.createElement("tbody");
    jobs.forEach((j) => tbody.appendChild(jobRow(j)));
    table.appendChild(tbody);
    list.appendChild(table);
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
  }
}

function jobRow(j) {
  const tr = document.createElement("tr");
  const nameTd = document.createElement("td");
  nameTd.textContent = j.name || j.id;
  const profileTd = document.createElement("td");
  profileTd.textContent = j.profile || j.profile_name || "default";
  const scheduleTd = document.createElement("td");
  scheduleTd.textContent = j.schedule_display || (j.schedule && j.schedule.display) || "";
  scheduleTd.style.fontFamily = "ui-monospace, monospace";
  scheduleTd.style.fontSize = "0.8rem";
  const statusTd = document.createElement("td");
  statusTd.innerHTML = `<span class="pill ${j.enabled ? "on" : "off"}">${j.state || (j.enabled ? "scheduled" : "paused")}</span>`;
  const nextTd = document.createElement("td");
  nextTd.textContent = j.next_run_at ? new Date(j.next_run_at).toLocaleString() : "-";
  nextTd.style.fontSize = "0.8rem";
  nextTd.style.color = "var(--text-muted)";

  const actionTd = document.createElement("td");
  const row = document.createElement("div");
  row.className = "row-actions";

  const runBtn = document.createElement("button");
  runBtn.innerHTML = icon("play", 14);
  runBtn.title = "Run now";
  runBtn.addEventListener("click", async () => {
    try {
      await apiSend("POST", `/cron/${j.id}/run`);
      toast("Queued to run");
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });

  const toggleBtn = document.createElement("button");
  toggleBtn.innerHTML = icon(j.enabled ? "pause" : "play", 14);
  toggleBtn.title = j.enabled ? "Pause" : "Resume";
  toggleBtn.addEventListener("click", async () => {
    try {
      await apiSend("POST", `/cron/${j.id}/${j.enabled ? "pause" : "resume"}`);
      await loadJobs();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });

  const delBtn = document.createElement("button");
  delBtn.innerHTML = icon("trash", 14);
  delBtn.title = "Delete";
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Delete routine "${j.name}"?`)) return;
    try {
      await apiSend("DELETE", `/cron/${j.id}`);
      toast("Deleted");
      await loadJobs();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });

  row.append(runBtn, toggleBtn, delBtn);
  actionTd.appendChild(row);
  tr.append(nameTd, profileTd, scheduleTd, statusTd, nextTd, actionTd);
  return tr;
}

document.getElementById("add-cron-btn").addEventListener("click", () => {
  document.getElementById("cron-form").reset();
  document.getElementById("cron-modal-backdrop").classList.add("open");
});
document.getElementById("cron-modal-cancel").addEventListener("click", () => {
  document.getElementById("cron-modal-backdrop").classList.remove("open");
});
document.getElementById("cron-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "cron-modal-backdrop") e.target.classList.remove("open");
});

document.getElementById("cron-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: document.getElementById("cron-name").value.trim(),
    prompt: document.getElementById("cron-prompt").value.trim(),
    schedule: document.getElementById("cron-schedule").value.trim(),
  };
  try {
    await apiSend("POST", "/cron", body);
    toast("Routine created");
    document.getElementById("cron-modal-backdrop").classList.remove("open");
    await loadJobs();
  } catch (err) {
    toast("Failed: " + err.message);
  }
});

renderShell("cron");
document.getElementById("add-cron-btn").innerHTML = icon("plus", 15) + "<span>New routine</span>";
loadJobs();
