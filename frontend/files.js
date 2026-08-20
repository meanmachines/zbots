"use strict";

let currentPath = "";
let currentAbsPath = ""; // the real absolute directory path the API resolved "" (root) to

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
  currentAbsPath = res.path || currentPath;
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
  nameTd.style.cursor = "pointer";
  nameTd.addEventListener("click", () => {
    if (entry.is_directory) loadFiles(entry.path);
    else openFileViewer(entry);
  });
  const sizeTd = document.createElement("td");
  sizeTd.textContent = entry.is_directory ? "-" : fmtBytes(entry.size);
  const mtimeTd = document.createElement("td");
  mtimeTd.textContent = fmtTime(entry.mtime);
  mtimeTd.style.fontSize = "0.8rem";
  mtimeTd.style.color = "var(--text-muted)";
  const actionTd = document.createElement("td");
  const row = document.createElement("div");
  row.className = "row-actions";
  const delBtn = document.createElement("button");
  delBtn.innerHTML = icon("trash", 14);
  delBtn.title = "Delete";
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Delete "${entry.name}"?`)) return;
    try {
      await apiSend("DELETE", "/files", { path: entry.path, recursive: entry.is_directory });
      toast("Deleted");
      await loadFiles(currentPath);
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  row.appendChild(delBtn);
  actionTd.appendChild(row);
  tr.append(nameTd, sizeTd, mtimeTd, actionTd);
  return tr;
}

// ---------------------------------------------------------------------
// File content viewer
// ---------------------------------------------------------------------

function openFileViewer(entry) {
  document.getElementById("viewer-title").textContent = entry.name;
  const body = document.getElementById("viewer-body");
  body.textContent = "Loading...";
  document.getElementById("viewer-download").style.display = "none";
  document.getElementById("viewer-modal-backdrop").classList.add("open");

  apiGet(`/files/read?path=${encodeURIComponent(entry.path)}`)
    .then((res) => {
      const dl = document.getElementById("viewer-download");
      if (res.data_url) {
        dl.href = res.data_url;
        dl.download = entry.name;
        dl.style.display = "";
      }
      const isTextLike = /^text\/|json|xml|yaml|csv|javascript$/.test(res.mime_type || "");
      if (isTextLike && res.data_url) {
        try {
          const base64 = res.data_url.split(",")[1] || "";
          const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
          body.textContent = new TextDecoder("utf-8").decode(bytes);
          return;
        } catch (_) {
          // fall through to the binary message below
        }
      }
      body.textContent = `Binary file (${res.mime_type || "unknown type"}, ${fmtBytes(res.size)}) -- use Download to save it.`;
    })
    .catch((e) => {
      body.textContent = "Failed to load: " + e.message;
    });
}

document.getElementById("viewer-close").addEventListener("click", () => {
  document.getElementById("viewer-modal-backdrop").classList.remove("open");
});
document.getElementById("viewer-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "viewer-modal-backdrop") e.target.classList.remove("open");
});

// ---------------------------------------------------------------------
// New folder
// ---------------------------------------------------------------------

document.getElementById("new-folder-btn").addEventListener("click", () => {
  document.getElementById("new-folder-form").reset();
  document.getElementById("new-folder-modal-backdrop").classList.add("open");
});
document.getElementById("new-folder-cancel").addEventListener("click", () => {
  document.getElementById("new-folder-modal-backdrop").classList.remove("open");
});
document.getElementById("new-folder-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "new-folder-modal-backdrop") e.target.classList.remove("open");
});
document.getElementById("new-folder-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("new-folder-name").value.trim();
  if (!name) return;
  const path = `${currentAbsPath.replace(/\/$/, "")}/${name}`;
  try {
    await apiSend("POST", "/files/mkdir", { path });
    toast("Folder created");
    document.getElementById("new-folder-modal-backdrop").classList.remove("open");
    await loadFiles(currentPath);
  } catch (err) {
    toast("Failed: " + err.message);
  }
});

renderShell("files");
document.getElementById("new-folder-btn").innerHTML = icon("plus", 15) + "<span>New folder</span>";
loadFiles("");
