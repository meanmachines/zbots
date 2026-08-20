"use strict";

const API = "/bots-api";

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------

let roster = [];
let groups = [];
let showHidden = false;
let selected = null; // {kind: "bot"|"group", id: string}
let rosterPollTimer = null;
let messagesPollTimer = null;
let sending = false;

// ---------------------------------------------------------------------
// Tiny fetch helpers
// ---------------------------------------------------------------------

async function api(path, opts) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function apiGet(path) {
  return api(path);
}
function apiSend(method, path, body) {
  return api(path, { method, body: body !== undefined ? JSON.stringify(body) : undefined });
}

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("open");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("open"), 3200);
}

// ---------------------------------------------------------------------
// Avatars -- deterministic from bot name, no server round-trip needed
// unless the user picked an uploaded image. Kept intentionally simple
// (fixed shape/color tables, not freehand generated paths) so it's
// correct without a live render loop to check it against.
// ---------------------------------------------------------------------

function hashString(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

const AVATAR_COLORS = [
  "#f2f2f2", "#e5484d", "#f76b15", "#f5a623", "#46a758",
  "#12a594", "#0091ff", "#8e4ec6", "#e93d82", "#a0a0a0",
];

// Eight hand-picked, valid blob-ish SVG paths on a 100x100 viewBox.
const BLOB_PATHS = [
  "M50 8C68 8 90 24 90 50C90 74 70 92 48 92C24 92 8 72 8 50C8 26 30 8 50 8Z",
  "M52 6C74 10 94 28 88 52C82 76 58 94 36 88C14 82 4 58 10 38C16 18 32 3 52 6Z",
  "M48 10C66 4 92 18 92 44C92 68 76 90 50 90C26 90 6 70 8 46C10 24 30 16 48 10Z",
  "M46 4C64 2 88 16 92 38C96 60 82 88 56 92C32 96 8 78 6 54C4 30 26 6 46 4Z",
  "M50 12C70 12 88 30 86 50C84 72 64 88 44 86C22 84 8 64 12 44C16 24 32 12 50 12Z",
  "M40 6C60 0 90 12 94 36C98 60 82 92 54 92C28 92 4 72 6 46C8 24 22 12 40 6Z",
  "M54 8C74 14 92 34 88 56C84 78 60 92 40 88C18 84 4 62 10 40C16 20 36 2 54 8Z",
  "M44 4C66 2 90 20 90 44C90 68 72 92 48 90C26 88 6 66 8 44C10 24 24 6 44 4Z",
];

const GEOMETRIC_SHAPES = ["circle", "square", "triangle", "diamond", "hexagon", "pentagon", "star"];

function shapeMarkup(shape, color) {
  const c = color;
  switch (shape) {
    case "circle":
      return `<circle cx="50" cy="50" r="42" fill="${c}"/>`;
    case "square":
      return `<rect x="14" y="14" width="72" height="72" rx="10" fill="${c}"/>`;
    case "triangle":
      return `<polygon points="50,10 90,85 10,85" fill="${c}"/>`;
    case "diamond":
      return `<polygon points="50,6 94,50 50,94 6,50" fill="${c}"/>`;
    case "hexagon":
      return `<polygon points="50,6 89,28 89,72 50,94 11,72 11,28" fill="${c}"/>`;
    case "pentagon":
      return `<polygon points="50,6 94,38 77,90 23,90 6,38" fill="${c}"/>`;
    case "star":
      return `<polygon points="50,4 61,37 96,37 68,58 79,92 50,71 21,92 32,58 4,37 39,37" fill="${c}"/>`;
    default:
      return `<circle cx="50" cy="50" r="42" fill="${c}"/>`;
  }
}

function avatarSvg(name, avatar) {
  avatar = avatar || { type: "blob" };
  const seed = avatar.seed != null ? avatar.seed : hashString(name);
  const bgColor = AVATAR_COLORS[seed % AVATAR_COLORS.length];
  const darkBg = "#111111";
  if (avatar.type === "geometric") {
    const shape = GEOMETRIC_SHAPES[Math.floor(seed / 7) % GEOMETRIC_SHAPES.length];
    return `<svg viewBox="0 0 100 100"><rect width="100" height="100" fill="${darkBg}"/>${shapeMarkup(shape, bgColor)}</svg>`;
  }
  // default: blob
  const path = BLOB_PATHS[Math.floor(seed / 11) % BLOB_PATHS.length];
  return `<svg viewBox="0 0 100 100"><rect width="100" height="100" fill="${darkBg}"/><path d="${path}" fill="${bgColor}"/></svg>`;
}

function avatarNode(name, avatar) {
  const div = document.createElement("div");
  div.className = "avatar";
  if (avatar && avatar.type === "upload" && avatar.url) {
    const img = document.createElement("img");
    img.src = avatar.url;
    img.alt = name;
    div.appendChild(img);
  } else {
    div.innerHTML = avatarSvg(name, avatar);
  }
  return div;
}

// ---------------------------------------------------------------------
// Roster
// ---------------------------------------------------------------------

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

async function refreshRoster() {
  try {
    roster = await apiGet(`/roster?include_hidden=${showHidden}`);
  } catch (e) {
    toast("Failed to load bots: " + e.message);
    return;
  }
  renderRoster();
  if (selected && selected.kind === "bot") {
    const entry = roster.find((r) => r.name === selected.id);
    if (entry) renderChatHeader(entry);
  }
}

function renderRoster() {
  const query = document.getElementById("search-input").value.trim().toLowerCase();
  const list = document.getElementById("roster-list");
  list.innerHTML = "";

  const filtered = roster.filter((r) => {
    if (!query) return true;
    return r.name.toLowerCase().includes(query) || r.title.toLowerCase().includes(query);
  });

  const active = filtered.filter((r) => r.is_active);
  const rest = filtered.filter((r) => !r.is_active);

  if (active.length) {
    const label = document.createElement("div");
    label.className = "roster-section-label";
    label.textContent = "Active now";
    list.appendChild(label);
    active.forEach((r) => list.appendChild(rosterRow(r)));
  }

  const label2 = document.createElement("div");
  label2.className = "roster-section-label";
  label2.textContent = active.length ? "All bots" : "Bots";
  list.appendChild(label2);

  if (!rest.length && !active.length) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = query ? "No bots match your search." : "No bots yet -- create one to get started.";
    list.appendChild(empty);
  }
  rest.forEach((r) => list.appendChild(rosterRow(r)));

  if (groups.length) {
    const glabel = document.createElement("div");
    glabel.className = "roster-section-label";
    glabel.textContent = "Groups";
    list.appendChild(glabel);
    groups.forEach((g) => list.appendChild(groupRow(g)));
  }
}

function rosterRow(entry) {
  const row = document.createElement("div");
  row.className = "roster-row" + (selected && selected.kind === "bot" && selected.id === entry.name ? " selected" : "");
  row.appendChild(avatarNode(entry.name, entry.avatar));

  const body = document.createElement("div");
  body.className = "roster-row-body";
  const top = document.createElement("div");
  top.className = "roster-row-top";
  const title = document.createElement("div");
  title.className = "roster-row-title";
  title.textContent = entry.title;
  const time = document.createElement("div");
  time.className = "roster-row-time";
  time.textContent = timeAgo(entry.last_active);
  top.append(title, time);
  const preview = document.createElement("div");
  preview.className = "roster-row-preview";
  preview.textContent = entry.preview || entry.description || "No messages yet";
  body.append(top, preview);
  row.appendChild(body);

  if (entry.is_active) {
    const dot = document.createElement("div");
    dot.className = "active-dot";
    row.appendChild(dot);
  }

  row.addEventListener("click", () => selectBot(entry.name));
  row.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    openBotContextMenu(e.clientX, e.clientY, entry);
  });
  return row;
}

function groupRow(group) {
  const row = document.createElement("div");
  row.className = "roster-row" + (selected && selected.kind === "group" && selected.id === group.id ? " selected" : "");
  const av = document.createElement("div");
  av.className = "avatar";
  av.innerHTML = `<svg viewBox="0 0 100 100"><rect width="100" height="100" fill="#111"/><circle cx="38" cy="42" r="22" fill="#f2f2f2"/><circle cx="66" cy="55" r="18" fill="#9a9a9a"/></svg>`;
  row.appendChild(av);
  const body = document.createElement("div");
  body.className = "roster-row-body";
  const title = document.createElement("div");
  title.className = "roster-row-title";
  title.textContent = group.name;
  const preview = document.createElement("div");
  preview.className = "roster-row-preview";
  preview.textContent = group.members.join(", ");
  body.append(title, preview);
  row.appendChild(body);
  row.addEventListener("click", () => selectGroup(group.id));
  return row;
}

// ---------------------------------------------------------------------
// Context menu
// ---------------------------------------------------------------------

function closeContextMenu() {
  document.getElementById("context-menu").classList.remove("open");
}

function openBotContextMenu(x, y, entry) {
  const menu = document.getElementById("context-menu");
  menu.innerHTML = "";
  const items = [
    { label: entry.is_hidden ? "Unhide bot" : "Hide bot", action: () => toggleHide(entry) },
    { label: "Edit profile", action: () => openEditModal(entry) },
    { label: "Choose avatar", action: () => openAvatarModal(entry) },
    { label: "Duplicate", action: () => openDuplicateModal(entry) },
    { sep: true },
    { label: "Delete", danger: true, action: () => deleteBot(entry) },
  ];
  items.forEach((it) => {
    if (it.sep) {
      const sep = document.createElement("div");
      sep.className = "ctx-sep";
      menu.appendChild(sep);
      return;
    }
    const div = document.createElement("div");
    div.className = "ctx-item" + (it.danger ? " danger" : "");
    div.textContent = it.label;
    div.addEventListener("click", () => {
      closeContextMenu();
      it.action();
    });
    menu.appendChild(div);
  });
  menu.style.left = x + "px";
  menu.style.top = y + "px";
  menu.classList.add("open");
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#context-menu")) closeContextMenu();
});

// ---------------------------------------------------------------------
// Bot actions
// ---------------------------------------------------------------------

async function toggleHide(entry) {
  try {
    await apiSend("POST", `/bots/${entry.name}/${entry.is_hidden ? "unhide" : "hide"}`);
    toast(entry.is_hidden ? `${entry.title} unhidden` : `${entry.title} hidden`);
    await refreshRoster();
  } catch (e) {
    toast("Failed: " + e.message);
  }
}

async function deleteBot(entry) {
  if (!confirm(`Delete "${entry.title}" (${entry.name})? This cannot be undone.`)) return;
  try {
    await apiSend("DELETE", `/bots/${entry.name}`);
    if (selected && selected.kind === "bot" && selected.id === entry.name) {
      selected = null;
      showEmptyMain();
    }
    toast(`${entry.title} deleted`);
    await refreshRoster();
  } catch (e) {
    toast("Failed to delete: " + e.message);
  }
}

// ---------------------------------------------------------------------
// Create / Edit bot modal
// ---------------------------------------------------------------------

let modelCatalog = [];
let editingBot = null; // set when the modal is in "edit" mode

async function loadModelCatalog() {
  try {
    const res = await apiGet("/models");
    modelCatalog = res.models || [];
  } catch (_) {
    modelCatalog = [];
  }
  const providerSel = document.getElementById("bot-provider");
  const modelSel = document.getElementById("bot-model");
  const providers = [...new Set(modelCatalog.map((m) => m.provider))];
  providerSel.innerHTML = '<option value="">(inherit default)</option>';
  providers.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    providerSel.appendChild(opt);
  });
  const refreshModels = () => {
    const p = providerSel.value;
    modelSel.innerHTML = '<option value="">(inherit default)</option>';
    modelCatalog.filter((m) => !p || m.provider === p).forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.model;
      opt.textContent = m.model;
      modelSel.appendChild(opt);
    });
  };
  providerSel.onchange = refreshModels;
  refreshModels();

  const cloneSel = document.getElementById("bot-clone-from");
  cloneSel.innerHTML = '<option value="">— start fresh —</option>';
  roster.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r.name;
    opt.textContent = r.title;
    cloneSel.appendChild(opt);
  });
}

function openCreateModal() {
  editingBot = null;
  document.getElementById("bot-modal-title").textContent = "New bot";
  document.getElementById("bot-modal-submit").textContent = "Create bot";
  document.getElementById("bot-form").reset();
  document.getElementById("bot-name").disabled = false;
  loadModelCatalog();
  document.getElementById("bot-modal-backdrop").classList.add("open");
}

function openEditModal(entry) {
  editingBot = entry;
  document.getElementById("bot-modal-title").textContent = "Edit " + entry.title;
  document.getElementById("bot-modal-submit").textContent = "Save changes";
  document.getElementById("bot-form").reset();
  document.getElementById("bot-name").value = entry.name;
  document.getElementById("bot-name").disabled = true;
  document.getElementById("bot-title").value = entry.title;
  document.getElementById("bot-description").value = entry.description;
  loadModelCatalog().then(() => {
    document.getElementById("bot-provider").value = entry.provider || "";
    document.getElementById("bot-provider").onchange();
    document.getElementById("bot-model").value = entry.model || "";
  });
  document.getElementById("bot-modal-backdrop").classList.add("open");
}

document.getElementById("bot-modal-cancel").addEventListener("click", () => {
  document.getElementById("bot-modal-backdrop").classList.remove("open");
});

document.getElementById("bot-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("bot-name").value.trim().toLowerCase();
  const title = document.getElementById("bot-title").value.trim();
  const description = document.getElementById("bot-description").value.trim();
  const provider = document.getElementById("bot-provider").value;
  const model = document.getElementById("bot-model").value;
  const soul = document.getElementById("bot-soul").value;
  const cloneFrom = document.getElementById("bot-clone-from").value;
  const noSkills = document.getElementById("bot-no-skills").checked;

  try {
    if (editingBot) {
      await apiSend("PATCH", `/bots/${editingBot.name}`, {
        title: title || undefined,
        description,
        provider: provider || undefined,
        model: model || undefined,
        soul: soul || undefined,
      });
      toast(`${title || editingBot.name} updated`);
    } else {
      await apiSend("POST", "/bots", {
        name,
        title,
        description,
        clone_from: cloneFrom || undefined,
        provider: provider || undefined,
        model: model || undefined,
        soul: soul || undefined,
        no_skills: noSkills,
      });
      toast(`${title || name} created`);
    }
    document.getElementById("bot-modal-backdrop").classList.remove("open");
    await refreshRoster();
  } catch (e2) {
    toast("Failed: " + e2.message);
  }
});

// ---------------------------------------------------------------------
// Duplicate modal
// ---------------------------------------------------------------------

let duplicatingBot = null;

function openDuplicateModal(entry) {
  duplicatingBot = entry;
  document.getElementById("dup-name").value = "";
  document.getElementById("dup-modal-backdrop").classList.add("open");
}
document.getElementById("dup-modal-cancel").addEventListener("click", () => {
  document.getElementById("dup-modal-backdrop").classList.remove("open");
});
document.getElementById("dup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const newName = document.getElementById("dup-name").value.trim().toLowerCase();
  try {
    await apiSend("POST", `/bots/${duplicatingBot.name}/duplicate`, { new_name: newName });
    toast(`Duplicated as ${newName}`);
    document.getElementById("dup-modal-backdrop").classList.remove("open");
    await refreshRoster();
  } catch (e2) {
    toast("Failed: " + e2.message);
  }
});

// ---------------------------------------------------------------------
// Avatar modal
// ---------------------------------------------------------------------

let avatarTargetBot = null;

function openAvatarModal(entry) {
  avatarTargetBot = entry;
  const wrap = document.getElementById("avatar-options");
  wrap.innerHTML = "";
  const seedBase = hashString(entry.name);
  const choices = [];
  for (let i = 0; i < 4; i++) choices.push({ type: "blob", seed: seedBase + i * 101 });
  for (let i = 0; i < 4; i++) choices.push({ type: "geometric", seed: seedBase + i * 37 });
  choices.forEach((choice) => {
    const btn = document.createElement("button");
    btn.className = "icon-btn";
    btn.style.width = "48px";
    btn.style.height = "48px";
    btn.style.borderRadius = "50%";
    btn.style.padding = "0";
    btn.style.overflow = "hidden";
    btn.innerHTML = avatarSvg(entry.name, choice);
    btn.addEventListener("click", async () => {
      try {
        await apiSend("PUT", `/bots/${entry.name}/avatar`, choice);
        toast("Avatar updated");
        document.getElementById("avatar-modal-backdrop").classList.remove("open");
        await refreshRoster();
      } catch (e) {
        toast("Failed: " + e.message);
      }
    });
    wrap.appendChild(btn);
  });
  document.getElementById("avatar-modal-backdrop").classList.add("open");
}

document.getElementById("avatar-modal-cancel").addEventListener("click", () => {
  document.getElementById("avatar-modal-backdrop").classList.remove("open");
});

document.getElementById("avatar-upload").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file || !avatarTargetBot) return;
  const form = new FormData();
  form.append("file", file);
  try {
    await fetch(`${API}/bots/${avatarTargetBot.name}/avatar/upload`, { method: "POST", body: form }).then((r) => {
      if (!r.ok) throw new Error("upload failed");
    });
    toast("Avatar uploaded");
    document.getElementById("avatar-modal-backdrop").classList.remove("open");
    await refreshRoster();
  } catch (err) {
    toast("Upload failed: " + err.message);
  }
});

// ---------------------------------------------------------------------
// Chat view
// ---------------------------------------------------------------------

function showEmptyMain() {
  document.getElementById("main-empty").style.display = "flex";
  document.getElementById("chat-view").style.display = "none";
  clearInterval(messagesPollTimer);
}

function showChatView() {
  document.getElementById("main-empty").style.display = "none";
  document.getElementById("chat-view").style.display = "flex";
}

function renderChatHeader(entry) {
  const avatarSlot = document.getElementById("chat-header-avatar");
  avatarSlot.innerHTML = "";
  avatarSlot.appendChild(avatarNode(entry.name, entry.avatar));
  document.getElementById("chat-header-title").textContent = entry.title;
  const modelEl = document.getElementById("chat-header-model");
  modelEl.textContent = entry.model || "";
  modelEl.style.display = entry.model ? "" : "none";
  modelEl.title = entry.provider ? `Provider: ${entry.provider}` : "";
  document.getElementById("chat-header-desc").textContent = entry.description || entry.name;
  document.getElementById("composer-input").placeholder = `Message ${entry.title}`;
}

async function selectBot(name) {
  selected = { kind: "bot", id: name };
  document.body.classList.add("chat-open");
  document.getElementById("routines-pane").classList.remove("open");
  const entry = roster.find((r) => r.name === name);
  if (entry) renderChatHeader(entry);
  showChatView();
  renderRoster();
  await loadMessages();
  clearInterval(messagesPollTimer);
  messagesPollTimer = setInterval(loadMessages, 5000);
}

function backToRoster() {
  selected = null;
  document.body.classList.remove("chat-open");
  clearInterval(messagesPollTimer);
  renderRoster();
}

function renderMessages(rows, kind) {
  const pane = document.getElementById("messages-pane");
  pane.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "msg system";
    empty.textContent = "No messages yet -- say hi!";
    pane.appendChild(empty);
    return;
  }
  rows.forEach((row) => {
    const div = document.createElement("div");
    if (kind === "bot") {
      const isUser = row.role === "user";
      div.className = "msg " + (isUser ? "user" : "bot");
      div.textContent = extractText(row);
    } else {
      const isUser = row.from === "user";
      div.className = "msg " + (isUser ? "user" : "bot");
      if (!isUser) {
        const label = document.createElement("div");
        label.className = "msg-from";
        label.textContent = row.from;
        div.appendChild(label);
      }
      const textNode = document.createElement("div");
      textNode.textContent = row.text;
      div.appendChild(textNode);
    }
    pane.appendChild(div);
  });
  pane.scrollTop = pane.scrollHeight;
}

function extractText(row) {
  if (typeof row.content === "string") return row.content;
  if (Array.isArray(row.content)) {
    return row.content.map((c) => (typeof c === "string" ? c : c.text || "")).join("");
  }
  return row.text || "";
}

async function loadMessages() {
  if (!selected) return;
  try {
    if (selected.kind === "bot") {
      const rows = await apiGet(`/bots/${selected.id}/messages`);
      const chatRows = (rows || []).filter((r) => r.role === "user" || r.role === "assistant");
      renderMessages(chatRows, "bot");
    } else {
      const rows = await apiGet(`/groups/${selected.id}/messages`);
      renderMessages(rows || [], "group");
    }
  } catch (e) {
    // Quiet -- polling failures shouldn't spam toasts.
  }
}

document.getElementById("send-btn").addEventListener("click", sendComposerMessage);
document.getElementById("composer-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendComposerMessage();
  }
});

async function sendComposerMessage() {
  if (sending || !selected) return;
  const input = document.getElementById("composer-input");
  const text = input.value.trim();
  if (!text) return;
  sending = true;
  document.getElementById("send-btn").disabled = true;
  input.value = "";
  try {
    if (selected.kind === "bot") {
      await apiSend("POST", `/bots/${selected.id}/messages`, { text });
    } else {
      await apiSend("POST", `/groups/${selected.id}/messages`, { text, sender: "user" });
    }
    await loadMessages();
    await refreshRoster();
  } catch (e) {
    toast("Send failed: " + e.message);
    input.value = text;
  } finally {
    sending = false;
    document.getElementById("send-btn").disabled = false;
  }
}

// ---------------------------------------------------------------------
// Routines
// ---------------------------------------------------------------------

document.getElementById("routines-toggle-btn").addEventListener("click", async () => {
  const pane = document.getElementById("routines-pane");
  pane.classList.toggle("open");
  if (pane.classList.contains("open") && selected && selected.kind === "bot") {
    await loadRoutines(selected.id);
  }
});
document.getElementById("routines-close-btn").addEventListener("click", () => {
  document.getElementById("routines-pane").classList.remove("open");
});

async function loadRoutines(botName) {
  const list = document.getElementById("routines-list");
  list.innerHTML = "<div class='empty-hint'>Loading…</div>";
  try {
    const rows = await apiGet(`/bots/${botName}/routines`);
    list.innerHTML = "";
    if (!rows.length) {
      list.innerHTML = "<div class='empty-hint'>No routines yet.</div>";
      return;
    }
    rows.forEach((job) => list.appendChild(routineCard(job)));
  } catch (e) {
    list.innerHTML = "<div class='empty-hint'>Failed to load routines.</div>";
  }
}

function routineCard(job) {
  const card = document.createElement("div");
  card.className = "routine-card";
  const name = document.createElement("div");
  name.className = "routine-card-name";
  name.textContent = (job.name || "").replace(/^\[bot:[a-zA-Z0-9_-]+\]\s*/, "");
  const schedule = document.createElement("div");
  schedule.className = "routine-card-schedule";
  schedule.textContent = job.schedule || "";
  card.append(name, schedule);

  const actions = document.createElement("div");
  actions.className = "routine-card-actions";
  const pauseBtn = document.createElement("button");
  pauseBtn.textContent = job.enabled === false ? "Resume" : "Pause";
  pauseBtn.addEventListener("click", async () => {
    try {
      await apiSend("POST", `/routines/${job.id}/${job.enabled === false ? "resume" : "pause"}`);
      await loadRoutines(selected.id);
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  const delBtn = document.createElement("button");
  delBtn.textContent = "Delete";
  delBtn.addEventListener("click", async () => {
    if (!confirm("Delete this routine?")) return;
    try {
      await apiSend("DELETE", `/routines/${job.id}`);
      await loadRoutines(selected.id);
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  actions.append(pauseBtn, delBtn);
  card.appendChild(actions);
  return card;
}

document.getElementById("add-routine-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selected || selected.kind !== "bot") return;
  const routine = document.getElementById("routine-name").value.trim();
  const prompt = document.getElementById("routine-prompt").value.trim();
  const schedule = document.getElementById("routine-schedule").value.trim();
  try {
    await apiSend("POST", `/bots/${selected.id}/routines`, { routine, prompt, schedule });
    document.getElementById("add-routine-form").reset();
    toast("Routine added");
    await loadRoutines(selected.id);
  } catch (e2) {
    toast("Failed: " + e2.message);
  }
});

// ---------------------------------------------------------------------
// Groups
// ---------------------------------------------------------------------

async function refreshGroups() {
  try {
    groups = await apiGet("/groups");
  } catch (_) {
    groups = [];
  }
}

async function selectGroup(id) {
  selected = { kind: "group", id };
  document.body.classList.add("chat-open");
  document.getElementById("routines-pane").classList.remove("open");
  const group = groups.find((g) => g.id === id);
  document.getElementById("chat-header-avatar").innerHTML =
    `<svg viewBox="0 0 100 100"><rect width="100" height="100" fill="#111"/><circle cx="38" cy="42" r="22" fill="#f2f2f2"/><circle cx="66" cy="55" r="18" fill="#9a9a9a"/></svg>`;
  document.getElementById("chat-header-title").textContent = group ? group.name : "Group";
  document.getElementById("chat-header-model").style.display = "none";
  document.getElementById("chat-header-desc").textContent = group ? group.members.join(", ") : "";
  document.getElementById("composer-input").placeholder = "Message the group (@name to address one bot)";
  showChatView();
  renderRoster();
  await loadMessages();
  clearInterval(messagesPollTimer);
  messagesPollTimer = setInterval(loadMessages, 5000);
}

document.getElementById("groups-btn").addEventListener("click", openGroupsModal);
document.getElementById("groups-modal-close").addEventListener("click", () => {
  document.getElementById("groups-modal-backdrop").classList.remove("open");
});

async function openGroupsModal() {
  await refreshGroups();
  const existing = document.getElementById("groups-existing");
  existing.innerHTML = "";
  if (!groups.length) {
    existing.innerHTML = "<div class='empty-hint'>No groups yet.</div>";
  } else {
    groups.forEach((g) => {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.justifyContent = "space-between";
      row.style.alignItems = "center";
      row.style.padding = "0.4rem 0";
      const label = document.createElement("div");
      label.textContent = `${g.name} (${g.members.join(", ")})`;
      label.style.fontSize = "0.85rem";
      const del = document.createElement("button");
      del.className = "btn";
      del.textContent = "Delete";
      del.addEventListener("click", async () => {
        await apiSend("DELETE", `/groups/${g.id}`);
        await openGroupsModal();
        renderRoster();
      });
      row.append(label, del);
      existing.appendChild(row);
    });
  }

  const checksWrap = document.getElementById("group-member-checks");
  checksWrap.innerHTML = "";
  roster.forEach((r) => {
    const label = document.createElement("label");
    label.style.display = "flex";
    label.style.alignItems = "center";
    label.style.gap = "0.4rem";
    label.style.marginBottom = "0.3rem";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = r.name;
    label.append(cb, document.createTextNode(r.title));
    checksWrap.appendChild(label);
  });

  document.getElementById("groups-modal-backdrop").classList.add("open");
}

document.getElementById("create-group-btn").addEventListener("click", async () => {
  const name = document.getElementById("group-name").value.trim();
  const members = [...document.querySelectorAll("#group-member-checks input:checked")].map((el) => el.value);
  if (members.length < 2) {
    toast("Pick at least 2 members.");
    return;
  }
  try {
    await apiSend("POST", "/groups", { name, members });
    toast("Group created");
    document.getElementById("group-name").value = "";
    await openGroupsModal();
    renderRoster();
  } catch (e) {
    toast("Failed: " + e.message);
  }
});

// ---------------------------------------------------------------------
// Misc wiring
// ---------------------------------------------------------------------

document.getElementById("search-input").addEventListener("input", renderRoster);
document.getElementById("new-bot-btn").addEventListener("click", openCreateModal);
document.getElementById("show-hidden-btn").addEventListener("click", () => {
  showHidden = !showHidden;
  document.getElementById("show-hidden-btn").classList.toggle("active", showHidden);
  refreshRoster();
});

[
  ["bot-modal-backdrop", "bot-modal-cancel"],
  ["dup-modal-backdrop", null],
  ["avatar-modal-backdrop", null],
  ["groups-modal-backdrop", null],
].forEach(([backdropId]) => {
  document.getElementById(backdropId).addEventListener("click", (e) => {
    if (e.target.id === backdropId) e.target.classList.remove("open");
  });
});

document.getElementById("chat-back-btn").addEventListener("click", backToRoster);

document.getElementById("chat-menu-btn").addEventListener("click", (e) => {
  if (!selected || selected.kind !== "bot") return;
  const entry = roster.find((r) => r.name === selected.id);
  if (!entry) return;
  const rect = e.currentTarget.getBoundingClientRect();
  openBotContextMenu(rect.right - 170, rect.bottom + 6, entry);
});

function populateIcons() {
  document.getElementById("chat-back-btn").innerHTML = icon("back", 16);
  document.getElementById("show-hidden-btn").innerHTML = icon("eye", 16);
  document.getElementById("groups-btn").innerHTML = icon("group", 16);
  document.getElementById("new-bot-btn").innerHTML = icon("plus", 15) + "<span>New</span>";
  document.getElementById("routines-toggle-btn").innerHTML = icon("clock", 16);
  document.getElementById("chat-menu-btn").innerHTML = icon("more", 16);
  document.getElementById("routines-close-btn").innerHTML = icon("close", 15);
  document.getElementById("send-btn").innerHTML = icon("send", 15) + "<span>Send</span>";
  document.getElementById("main-empty-icon").innerHTML = icon("bots", 34);
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

async function boot() {
  renderShell("bots");
  populateIcons();
  await refreshGroups();
  await refreshRoster();
  clearInterval(rosterPollTimer);
  rosterPollTimer = setInterval(refreshRoster, 8000);
}

boot();
