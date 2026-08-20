"use strict";

let allSkills = [];

async function loadSkills() {
  const body = document.getElementById("skills-body");
  try {
    allSkills = (await apiGet("/skills")) || [];
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4">Failed to load: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  renderSkills();
}

function renderSkills() {
  const query = document.getElementById("skills-search").value.trim().toLowerCase();
  const body = document.getElementById("skills-body");
  body.innerHTML = "";
  const filtered = allSkills.filter(
    (s) => !query || s.name.toLowerCase().includes(query) || (s.description || "").toLowerCase().includes(query)
  );
  filtered.forEach((s) => body.appendChild(skillRow(s)));
}

function skillRow(s) {
  const tr = document.createElement("tr");
  const nameTd = document.createElement("td");
  nameTd.textContent = s.name;
  const catTd = document.createElement("td");
  catTd.innerHTML = `<span class="pill">${escapeHtml(s.category || "-")}</span>`;
  const descTd = document.createElement("td");
  descTd.textContent = s.description || "";
  descTd.style.color = "var(--text-muted)";
  descTd.style.fontSize = "0.8rem";
  const actionTd = document.createElement("td");
  const toggle = document.createElement("button");
  toggle.className = "btn btn-icon-label";
  toggle.innerHTML = icon(s.enabled ? "pause" : "play", 14) + `<span>${s.enabled ? "Disable" : "Enable"}</span>`;
  toggle.addEventListener("click", async () => {
    try {
      await apiSend("PUT", "/skills/toggle", { name: s.name, enabled: !s.enabled });
      s.enabled = !s.enabled;
      renderSkills();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  actionTd.appendChild(toggle);
  tr.append(nameTd, catTd, descTd, actionTd);
  return tr;
}

document.getElementById("skills-search").addEventListener("input", renderSkills);

renderShell("skills");
loadSkills();
