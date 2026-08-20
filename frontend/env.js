"use strict";

async function loadEnv() {
  const body = document.getElementById("env-body");
  let vars;
  try {
    vars = (await apiGet("/env")) || {};
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4">Failed to load: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  body.innerHTML = "";
  const entries = Object.entries(vars).sort((a, b) => {
    // Set variables first, then alphabetical.
    if (a[1].is_set !== b[1].is_set) return a[1].is_set ? -1 : 1;
    return a[0].localeCompare(b[0]);
  });
  entries.forEach(([key, meta]) => body.appendChild(envRow(key, meta)));
}

function envRow(key, meta) {
  const tr = document.createElement("tr");
  const keyTd = document.createElement("td");
  keyTd.textContent = key;
  keyTd.style.fontFamily = "ui-monospace, monospace";
  keyTd.style.fontSize = "0.8rem";

  const statusTd = document.createElement("td");
  const pill = document.createElement("span");
  pill.className = "pill " + (meta.is_set ? "on" : "off");
  pill.textContent = meta.is_set ? "Set" : "Not set";
  statusTd.appendChild(pill);

  const descTd = document.createElement("td");
  descTd.textContent = meta.description || "";
  descTd.style.color = "var(--text-muted)";
  descTd.style.fontSize = "0.8rem";

  const actionTd = document.createElement("td");
  if (meta.is_set) {
    const row = document.createElement("div");
    row.className = "row-actions";
    const delBtn = document.createElement("button");
    delBtn.innerHTML = icon("trash", 14);
    delBtn.title = "Remove";
    delBtn.addEventListener("click", async () => {
      if (!confirm(`Remove ${key}?`)) return;
      try {
        await apiSend("DELETE", `/env/${encodeURIComponent(key)}`);
        toast(`${key} removed`);
        await loadEnv();
      } catch (e) {
        toast("Failed: " + e.message);
      }
    });
    row.appendChild(delBtn);
    actionTd.appendChild(row);
  }

  tr.append(keyTd, statusTd, descTd, actionTd);
  return tr;
}

document.getElementById("env-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = document.getElementById("env-key").value.trim();
  const value = document.getElementById("env-value").value;
  try {
    await apiSend("PUT", "/env", { key, value });
    toast(`${key} saved`);
    document.getElementById("env-form").reset();
    await loadEnv();
  } catch (e) {
    toast("Failed: " + e.message);
  }
});

renderShell("env");
loadEnv();
