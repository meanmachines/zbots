"use strict";

let currentPath = "";

async function loadFiles(path) {
  currentPath = path || "";
  document.getElementById("files-path").textContent = currentPath || "/";
  const body = document.getElementById("files-body");
  body.innerHTML = "<tr><td colspan='4'>Loading...</td></tr>";
  let res;
  try {
    res = await apiGet(`/files${currentPath ? "?path=" + encodeURIComponent(currentPath) : ""}`);
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4">Failed to load: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  body.innerHTML = "";
  if (res.parent !== null && res.parent !== undefined) {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.innerHTML = `<td colspan="4">.. (up)</td>`;
    tr.addEventListener("click", () => loadFiles(res.parent));
    body.appendChild(tr);
  }
  (res.entries || []).forEach((entry) => body.appendChild(fileRow(entry)));
}

function fileRow(entry) {
  const tr = document.createElement("tr");
  const nameTd = document.createElement("td");
  nameTd.textContent = (entry.is_directory ? "[dir] " : "") + entry.name;
  if (entry.is_directory) {
    nameTd.style.cursor = "pointer";
    nameTd.addEventListener("click", () => loadFiles(entry.path));
  }
  const sizeTd = document.createElement("td");
  sizeTd.textContent = entry.is_directory ? "-" : fmtBytes(entry.size);
  const mtimeTd = document.createElement("td");
  mtimeTd.textContent = fmtTime(entry.mtime);
  mtimeTd.style.fontSize = "0.8rem";
  mtimeTd.style.color = "var(--text-muted)";
  const actionTd = document.createElement("td");
  const delBtn = document.createElement("button");
  delBtn.className = "row-actions";
  delBtn.innerHTML = `<button>${icon("trash", 14)}</button>`;
  delBtn.querySelector("button").addEventListener("click", async () => {
    if (!confirm(`Delete "${entry.name}"?`)) return;
    try {
      await apiSend("DELETE", "/files", { path: entry.path, recursive: entry.is_directory });
      toast("Deleted");
      await loadFiles(currentPath);
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  actionTd.appendChild(delBtn);
  tr.append(nameTd, sizeTd, mtimeTd, actionTd);
  return tr;
}

renderShell("files");
loadFiles("");
