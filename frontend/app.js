"use strict";

// Shared API/toast helpers live in common.js (loaded before this file).

let roster = [];
let groups = [];
let showHidden = false;
let selected = null; // {kind: "bot"|"group", id: string}
let rosterPollTimer = null;
let messagesPollTimer = null;
let lastRenderedCount = -1; // -1 means "next render is a fresh thread open, don't animate"
// Raw row payload (JSON) behind the last actual render, so a poll whose
// server response is byte-identical to what's already on screen can skip
// renderMessages entirely instead of unconditionally wiping and rebuilding
// the whole pane every 5s regardless of whether anything changed. That
// unconditional rebuild -- not any particular card-insertion timing -- was
// the real source of the preview-card flicker: messages get rewritten
// synchronously (one paint, no visible gap), but cards go back in through
// an async step, so every no-op poll still opened a real window where the
// pane briefly had zero cards. Reported live as "cards are still
// flickering" even after two rounds of tuning that async step itself.
let lastMessagesSignature = null;

// path -> fileInfo (or null for "checked, not a previewable file") --
// renderMessages re-scans and re-appends preview cards on every poll
// (rows are rebuilt from scratch each time, same as everything else in
// this pane), so without this cache a file that's still there five
// messages later would get re-fetched over the wire every 5s for no
// reason.
//
// Session-permanent, deliberately -- this went through TWO real bugs
// before landing here, both found live. First: a plain permanent cache
// meant a bot overwriting the SAME path with new content ("change the
// color to purple") kept showing whatever was fetched the FIRST time,
// forever. Tried fixing that with a short TTL (4s) instead -- but every
// poll (5s, unconditional) then landed just past expiry, forcing a real
// network re-fetch for every card on every single poll; the instantly-
// rendered base message list beat the cards back into view by a beat,
// visible as cards vanishing and reappearing every 5 seconds. Widening
// the TTL to 15s only pushed the same gap out to once every 15s instead
// of removing it -- still a periodic flicker, just slower, and still
// reported live as "chat is still refreshing all the time." The actual
// fix for staleness isn't a client-side cache heuristic at all -- it's
// the RESPONSE_STYLE guardrail in persona.py that has bots save a
// revision under a NEW filename instead of overwriting, so a genuinely
// different version gets a genuinely different (never-cached) path in
// the first place. This cache goes back to being permanent for a
// session on that basis -- no periodic re-fetch, no periodic flicker,
// and the rare case of a bot still overwriting despite the instruction
// means a hard refresh picks up the change, not a silent forever-stale
// preview.
let previewCache = new Map();

function getCachedPreview(path) {
  return previewCache.has(path) ? previewCache.get(path) : undefined;
}

function setCachedPreview(path, fileInfo) {
  previewCache.set(path, fileInfo);
}

// Workspace panel -- path -> {content: string|null, ts: number}. content is
// only ever pre-filled for write_file (the call's own arguments already
// carry the full text -- see findWorkspaceFilesInHistory below); read_file's
// content lives in the tool RESULT, not the call, so it's left null and
// fetched on click via GET /bots/{name}/workspace/file, same bound the
// backend enforces (see main.py's own _touched_paths_from_messages comment).
// Reset on every selectBot() -- this is a per-chat view, not cross-bot state.
let workspaceFiles = new Map();

// Keyed by "kind:id" so a reply in flight for one bot/group doesn't lock
// the composer for every other chat -- multi-bot is the whole point of
// this app, you should be able to message bot B while bot A is thinking.
let sendingKeys = new Set();

function chatKey(sel) {
  return sel ? `${sel.kind}:${sel.id}` : null;
}

function updateComposerState() {
  const btn = document.getElementById("send-btn");
  // A bot actively streaming still accepts a new send -- it goes to
  // /steer instead of a normal message (see sendComposerMessage's own
  // comment) -- so the button stays enabled for that case. Groups have no
  // single "run" to steer, so they keep the original blocked-while-
  // sending behavior.
  const isSteerable = selected && selected.kind === "bot";
  if (btn) btn.disabled = !isSteerable && sendingKeys.has(chatKey(selected));
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
  row.className = "roster-row" + (selected && selected.kind === "bot" && selected.id === entry.name ? " selected" : "") + (entry.gateway_running === false ? " offline" : "");
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
  } else if (entry.gateway_running === false) {
    const dot = document.createElement("div");
    dot.className = "status-dot offline-dot";
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
  document.getElementById("bot-category").value = entry.category || "general";
  loadModelCatalog().then(() => {
    document.getElementById("bot-provider").value = entry.provider || "";
    document.getElementById("bot-provider").onchange();
    document.getElementById("bot-model").value = entry.model || "";
  });
  apiGet(`/bots/${entry.name}/soul`)
    .then((res) => {
      document.getElementById("bot-soul").value = res.content || "";
    })
    .catch(() => {});
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
  // Empty ("Auto-detect from description") is omitted on create, which
  // triggers _infer_bot_category server-side; on edit it's a no-op (no
  // re-inference), so only send it there when the user actually picked one.
  const category = document.getElementById("bot-category").value;

  try {
    if (editingBot) {
      await apiSend("PATCH", `/bots/${editingBot.name}`, {
        title: title || undefined,
        description,
        provider: provider || undefined,
        model: model || undefined,
        // Edit mode prefills this field with the bot's real current soul,
        // so unlike create, an empty value here is a deliberate clear, not
        // "user didn't set one" -- must still be sent, not dropped.
        soul,
        category: category || undefined,
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
        category: category || undefined,
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
  const offlineEl = document.getElementById("chat-header-offline");
  if (offlineEl) offlineEl.style.display = entry.gateway_running === false ? "" : "none";
  const modelEl = document.getElementById("chat-header-model");
  // A plain textContent assignment made this pill impossible to truncate on
  // a narrow phone screen: with no wrapping element, the bare text became
  // an anonymous flex item that ignored max-width and just overflowed
  // straight through the header's action icons (confirmed live -- a long
  // model name like "nvidia/Qwen3.6-35B-A3B-NVFP4" rendered on top of the
  // 4 action buttons, both unreadable). A real inner span gives
  // text-overflow: ellipsis something it can actually apply to.
  modelEl.innerHTML = "";
  const modelText = document.createElement("span");
  modelText.className = "chat-header-model-text";
  modelText.textContent = entry.model || "Set model";
  modelEl.appendChild(modelText);
  modelEl.style.display = "";
  modelEl.title = entry.provider ? `Provider: ${entry.provider} -- click to change model` : "Click to set a model";
  modelEl.onclick = (e) => openModelSwitcher(e, entry.name);
  document.getElementById("chat-header-desc").textContent = entry.description || entry.name;
  document.getElementById("composer-input").placeholder = `Message ${entry.title}`;
}

// ---------------------------------------------------------------------
// Model switcher -- click the model pill in the chat header to see and
// change which model/provider a bot is using, without going through the
// full Edit modal for something this quick.
// ---------------------------------------------------------------------

async function openModelSwitcher(e, botName) {
  e.stopPropagation();
  const pop = document.getElementById("model-switcher");
  const rect = e.currentTarget.getBoundingClientRect();
  pop.style.left = rect.left + "px";
  pop.style.top = rect.bottom + 6 + "px";
  pop.innerHTML = "<div class='popover-group-label'>Loading...</div>";
  pop.classList.add("open");

  let models = [];
  try {
    const res = await apiGet("/models");
    models = res.models || [];
  } catch (err) {
    if (pop.classList.contains("open")) {
      pop.innerHTML = "<div class='popover-group-label'>Failed to load models</div>";
    }
    return;
  }
  if (!pop.classList.contains("open")) return; // closed while the fetch was in flight

  const entry = roster.find((r) => r.name === botName);
  pop.innerHTML = "";
  if (!models.length) {
    pop.innerHTML = "<div class='popover-group-label'>No providers configured -- add one on the Models page.</div>";
    return;
  }

  const byProvider = new Map();
  models.forEach((m) => {
    if (!byProvider.has(m.provider)) byProvider.set(m.provider, []);
    byProvider.get(m.provider).push(m.model);
  });

  byProvider.forEach((modelNames, provider) => {
    const label = document.createElement("div");
    label.className = "popover-group-label";
    label.textContent = provider;
    pop.appendChild(label);
    modelNames.forEach((model) => {
      const isCurrent = entry && entry.provider === provider && entry.model === model;
      const item = document.createElement("div");
      item.className = "popover-item" + (isCurrent ? " current" : "");
      const nameSpan = document.createElement("span");
      nameSpan.textContent = model;
      item.appendChild(nameSpan);
      if (isCurrent) {
        const check = document.createElement("span");
        check.innerHTML = icon("check", 13);
        item.appendChild(check);
      }
      item.addEventListener("click", async () => {
        pop.classList.remove("open");
        if (isCurrent) return;
        try {
          await apiSend("PATCH", `/bots/${botName}`, { provider, model });
          toast(`Model set to ${model}`);
          await refreshRoster();
        } catch (err2) {
          toast("Failed: " + err2.message);
        }
      });
      pop.appendChild(item);
    });
  });
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#model-switcher") && !e.target.closest("#chat-header-model")) {
    document.getElementById("model-switcher").classList.remove("open");
  }
});

async function selectBot(name) {
  selected = { kind: "bot", id: name };
  lastRenderedCount = -1;
  lastMessagesSignature = null;
  workspaceFiles = new Map();
  showWorkspaceFilesList();
  document.body.classList.add("chat-open");
  document.getElementById("routines-pane").classList.remove("open");
  document.getElementById("workspace-pane").classList.remove("open");
  stopWorkspacePortsPolling();
  const entry = roster.find((r) => r.name === name);
  if (entry) renderChatHeader(entry);
  showChatView();
  renderRoster();
  // Fire-and-forget pre-warm: the worker starts spinning up while the
  // user is still reading history/typing, instead of only on first send.
  // Never awaited/never blocks the chat from opening -- a failure here
  // is invisible; the real send still ensures the worker itself.
  apiSend("POST", "/bots/" + name + "/wake").catch(() => {});
  await loadMessages();
  updateComposerState();
  if (sendingKeys.has(chatKey(selected))) showTypingIndicator();
  clearInterval(messagesPollTimer);
  messagesPollTimer = setInterval(() => loadMessages(true), 5000);
}

function backToRoster() {
  selected = null;
  document.body.classList.remove("chat-open");
  clearInterval(messagesPollTimer);
  stopWorkspacePortsPolling();
  renderRoster();
}

function messageTimestamp(row) {
  return row.timestamp || row.ts || null;
}

function messageText(row, kind) {
  return kind === "bot" ? extractText(row) : row.text || "";
}

// Bumped once per render call, synchronously -- the async preview-card
// insertion step (see insertPreviewCardsAsync) captures its own value at
// start and checks it before every DOM mutation, so a render superseded
// by a newer one (the 5s poll firing again, or switching chats) always
// notices and stops touching a pane it no longer owns. Real bug found
// live without this: interleaving awaited network fetches directly into
// the per-row render loop meant one call to renderMessages could take
// multiple seconds; the poll timer firing again mid-way started a SECOND
// overlapping render that wiped the pane out from under the first,
// producing exactly the "messages constantly coming and going" flicker
// reported live. The fix is this file's general shape, not just the
// counter: the base message list below is built synchronously start to
// finish with no awaits at all, so it can never be caught mid-render by
// a second call the way the old version could.
let renderGeneration = 0;

function renderMessages(rows, kind, previewPaths) {
  const pane = document.getElementById("messages-pane");
  const myGeneration = ++renderGeneration;
  // Only messages beyond what the previous render already showed get the
  // pop-in animation -- rows are rebuilt from scratch on every poll/send,
  // and re-animating the whole history each time would be noisy, not
  // "subtle." -1 means this is a fresh thread open: show it instantly.
  const animateFrom = lastRenderedCount === -1 ? Infinity : lastRenderedCount;
  pane.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "msg system";
    empty.textContent = "No messages yet -- say hi!";
    pane.appendChild(empty);
    lastRenderedCount = 0;
    return;
  }
  // Anchor nodes recorded as each row is built, so preview cards can be
  // positioned afterward via insertBefore against a stable reference
  // instead of relying on "whatever the pane's last child happens to be"
  // (only safe when nothing else can insert in between, which async
  // fetches can no longer guarantee here).
  const anchors = []; // {node, ts}
  let lastDayKey = null;
  rows.forEach((row, i) => {
    const ts = messageTimestamp(row);
    const dayKey = ts ? new Date(ts * 1000).toDateString() : null;
    if (ts && dayKey !== lastDayKey) {
      const sep = document.createElement("div");
      sep.className = "msg-day-sep";
      sep.textContent = dayLabel(ts);
      pane.appendChild(sep);
      lastDayKey = dayKey;
    } else if (!ts) {
      lastDayKey = null;
    }

    const isUser = kind === "bot" ? row.role === "user" : row.from === "user";
    const div = document.createElement("div");
    div.className = "msg " + (isUser ? "user" : "bot");
    if (kind === "group" && !isUser) {
      const label = document.createElement("div");
      label.className = "msg-from";
      label.textContent = row.from;
      div.appendChild(label);
    }
    const text = messageText(row, kind);
    const body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = renderMarkdown(text);
    div.appendChild(body);

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    if (ts) {
      const time = document.createElement("span");
      time.className = "msg-time";
      time.textContent = fmtDateTime(ts);
      meta.appendChild(time);
    }
    const copyBtn = document.createElement("button");
    copyBtn.className = "msg-copy icon-btn";
    copyBtn.title = "Copy message";
    copyBtn.innerHTML = icon("copy", 12);
    copyBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      copyText(text)
        .then(() => toast("Copied"))
        .catch(() => toast("Copy failed"));
    });
    meta.appendChild(copyBtn);
    div.appendChild(meta);

    if (i >= animateFrom) div.classList.add("msg-pop");
    pane.appendChild(div);
    anchors.push({ node: div, ts: ts || 0 });
  });
  lastRenderedCount = rows.length;
  pane.scrollTop = pane.scrollHeight;

  // The only async part left, deliberately decoupled from everything
  // above -- guarded by myGeneration so a render superseded by a newer
  // poll before this finishes fetching can never insert into (or scroll)
  // a pane it no longer owns.
  if (previewPaths && previewPaths.size) {
    insertPreviewCardsAsync(pane, anchors, previewPaths, myGeneration);
  }
}

// previewPaths is a Map<path, timestamp> -- processed oldest first so
// each card is positioned right after the anchor (a rendered message, or
// a previously-inserted card) it chronologically belongs after, instead
// of every card landing in one batch at the very end regardless of when
// the file was actually generated. Real bug found live: a file generated
// early in a long conversation rendered BELOW messages sent long after
// it -- newest-message-last (the one thing a chat pane must always get
// right) was broken.
//
// Deliberately fire-and-forget from renderMessages' point of view (not
// awaited there) -- this can take a real network round trip per
// uncached path, and renderMessages itself must stay fast and
// synchronous (see its own comment for why). Every mutation here checks
// myGeneration first specifically so that being unawaited is safe: if a
// newer render has started in the meantime, this generation is stale and
// silently stops touching a pane it no longer owns, rather than
// inserting into (or scrolling) whatever the pane has since become.
async function insertPreviewCardsAsync(pane, anchors, previewPaths, myGeneration) {
  const pending = Array.from(previewPaths.entries()).sort((a, b) => a[1] - b[1]);
  for (const [path, ts] of pending) {
    const card = await buildPreviewCard(path);
    if (renderGeneration !== myGeneration) return; // superseded -- stop here
    if (!card) continue;
    let anchorNode = null;
    for (const a of anchors) {
      if (a.ts <= ts) anchorNode = a.node;
      else break;
    }
    if (anchorNode) {
      pane.insertBefore(card, anchorNode.nextSibling);
    } else {
      pane.insertBefore(card, pane.firstChild);
    }
    // Anchors are processed in ascending ts order and pending is sorted
    // the same way, so appending (not re-sorting) keeps this list valid
    // for the next iteration -- this card's ts is always >= every anchor
    // already in the list.
    anchors.push({ node: card, ts });
  }
  if (renderGeneration === myGeneration) pane.scrollTop = pane.scrollHeight;
}

function showTypingIndicator() {
  const pane = document.getElementById("messages-pane");
  const el = document.createElement("div");
  el.id = "typing-indicator";
  el.className = "msg bot typing-dots msg-pop";
  el.innerHTML = "<span></span><span></span><span></span>";
  pane.appendChild(el);
  pane.scrollTop = pane.scrollHeight;
}

function hideTypingIndicator() {
  document.getElementById("typing-indicator")?.remove();
}

// Friendly one-line labels for a live tool.started/tool.progress event --
// what the desktop app's own equivalent indicator shows while a tool
// runs ("Creating routine...", "Messaging default...") instead of either
// showing nothing or a raw internal tool name. bot-supervisor's own
// tools and hermes' own action-based "mega-tools" (cronjob, memory) get
// specific, context-aware labels; anything else falls back to a
// humanized version of its real name rather than being hidden outright,
// so a newly installed MCP server's tools still show SOMETHING sensible
// with zero changes needed here.
function toolStatusLabel(toolName, preview, args) {
  if (toolName === "_thinking") return "Thinking";
  // tool_search's bridge tools (tool_describe/tool_call) are the deferred-
  // listing mechanism itself, not something meaningful on their own --
  // resolve to the REAL tool they're describing/calling when possible.
  if (toolName === "tool_describe" || toolName === "tool_call") {
    const real = (args && (args.name || (args.arguments && args.arguments.name))) || preview;
    if (real) return toolStatusLabel(real.replace(/^mcp__.*?__/, ""), null, args && args.arguments);
    return "Looking into it";
  }
  if (toolName === "tool_search") return "Looking into it";
  const bare = toolName.replace(/^mcp__.*?__/, "");
  const target = preview || (args && (args.name || args.target));
  switch (bare) {
    case "list_bots": return "Checking bots";
    case "get_bot_status": return target ? `Checking on ${target}` : "Checking bot status";
    case "message_bot": return target ? `Messaging ${target}` : "Messaging a bot";
    case "delegate_task": return target ? `Delegating to ${target}` : "Delegating a task";
    case "create_bot": return target ? `Creating bot "${target}"` : "Creating a bot";
    case "cronjob": {
      const action = (args && args.action) || "";
      if (action === "create") return "Creating routine";
      if (action === "list") return "Checking routines";
      if (action === "update" || action === "edit") return "Updating routine";
      if (action === "delete" || action === "remove") return "Removing routine";
      if (action === "run" || action === "trigger") return "Running routine";
      return "Managing routines";
    }
    case "memory": {
      const action = (args && args.action) || "";
      if (action === "save" || action === "add" || action === "write") return "Saving to memory";
      if (action === "search" || action === "recall" || action === "get") return "Checking memory";
      if (action === "delete" || action === "remove" || action === "forget") return "Updating memory";
      return "Using memory";
    }
    default:
      // Humanize an unmapped tool name -- "web_search" -> "Using web search".
      return "Using " + bare.replace(/_/g, " ");
  }
}

// Collapsible "thinking" panel -- accumulates tool-call/reasoning activity
// for one in-flight turn as a scrollable, collapsed-by-default log instead
// of ever rendering it as the visible reply. Real bug this replaces: the
// previous design rendered each assistant.delta live into what looked
// like the final answer bubble, then deleted that bubble whenever a tool
// call followed (see toolStatusLabel's own comment above) -- with several
// tool calls in one turn that's a repeated write-then-erase of a
// bubble-shaped thing, which reads as flickering (reported live). Fix:
// never show in-progress text as if it were the answer. While a turn
// runs, only two things are visible -- the existing typing indicator
// (nothing has happened yet) and this panel's own live summary line
// (something is happening, and what); its log is opt-in detail behind the
// native <details> toggle, same pattern as a desktop chat app's "thought
// for Xs" affordance. The real final answer always comes from
// assistant.completed's own authoritative `content` field (see
// engine.py's _process_sse_frame docstring -- the same field the
// non-streaming path already used), rendered exactly once, so it can
// never be shown and then discarded.
let thinkingLastLine = null;

function getThinkingPanel() {
  let wrap = document.getElementById("thinking-wrap");
  if (wrap) return wrap;
  const pane = document.getElementById("messages-pane");
  wrap = document.createElement("div");
  wrap.id = "thinking-wrap";
  wrap.className = "msg bot msg-pop";
  wrap.innerHTML =
    '<details class="thinking-panel" id="thinking-panel">' +
    '<summary id="thinking-summary">Thinking…</summary>' +
    '<div class="thinking-log" id="thinking-log"></div>' +
    "</details>";
  pane.appendChild(wrap);
  pane.scrollTop = pane.scrollHeight;
  thinkingLastLine = null;
  return wrap;
}

function setThinkingSummary(text) {
  getThinkingPanel();
  document.getElementById("thinking-summary").textContent = text;
}

function logThinkingStep(text) {
  getThinkingPanel();
  setThinkingSummary(text);
  if (text === thinkingLastLine) return; // dedupe consecutive repeats (e.g. several "Thinking" ticks in a row)
  thinkingLastLine = text;
  const log = document.getElementById("thinking-log");
  const line = document.createElement("div");
  line.className = "thinking-step";
  line.textContent = text;
  log.appendChild(line);
  const panel = document.getElementById("thinking-panel");
  if (panel.open) {
    const pane = document.getElementById("messages-pane");
    pane.scrollTop = pane.scrollHeight;
  }
}

// Called once the turn is over (success or error) -- swaps the live,
// pulsing summary line for a static one so a collapsed panel doesn't look
// like it's still working after the fact.
function finalizeThinkingPanel() {
  const summary = document.getElementById("thinking-summary");
  if (summary) summary.textContent = "Show steps";
  thinkingLastLine = null;
}

function appendFinalBotMessage(content) {
  const pane = document.getElementById("messages-pane");
  const div = document.createElement("div");
  div.className = "msg bot msg-pop";
  const body = document.createElement("div");
  body.className = "msg-body";
  body.innerHTML = renderMarkdown(content);
  div.appendChild(body);
  pane.appendChild(div);
  pane.scrollTop = pane.scrollHeight;
  return div;
}

// Live preview of a file a bot just built -- pages, images, icons, any
// generated output. Real gap found live: a bot asked to "build a landing
// page" (or generate an image) has plenty of ways to actually produce the
// file (its own terminal tool, a dedicated file-write/image-gen tool,
// whatever it has), but none of those show the RESULT -- the user has to
// already know the file exists and go find it on the Files page. Rather
// than hardcode one tool name (fragile -- ties this to whatever tool
// happens to be wired in today), this scans every tool call's own args
// for a string that looks like a previewable file path, tool-agnostic by
// construction: any tool that takes a file path argument works
// automatically, no allowlist to maintain. Real backend support already
// exists for the render step -- GET /files/read already returns a
// data_url (data:<mime>;base64,...) for exactly this purpose regardless
// of file type, so there's no new backend plumbing needed, just wiring
// the frontend up to call it and show what comes back.
//
// Also matches hermes-agent's own real MEDIA:<path> tag convention (see
// api_server.py's _resolve_media_to_data_urls) -- confirmed live that tag
// is resolved to an inline data URL ONLY in the live SSE event at
// generation time, never in what actually gets persisted to message
// history, so a reopened conversation (or the next 5s poll rebuilding the
// pane) shows the literal unresolved "MEDIA:/root/foo.png" text with no
// image at all. Stripping the prefix and running it through this same
// preview pipeline fixes that without touching any backend code.
//
// Not anchored to the whole string -- real bug found live: a terminal-
// style tool wraps the actual path inside a full shell command
// ("cat /root/page.html | head -c 100"), so an exact-match regex never
// fires for the single most common way a bot actually touches a file.
// Matches a path-shaped token (no whitespace/quotes/shell metacharacters)
// ending in a previewable extension anywhere in the string instead.
// Requires an actual path separator ("/") -- real bug found live scanning
// a bot's whole history: without this, casual prose mentioning a bare
// filename ("Logo.png") or a generic placeholder ("/path/to/image.png")
// matched just as eagerly as a real path, and a shared match budget
// across the whole conversation meant those false positives could crowd
// out the real, recent file before it was ever reached.
// ":" excluded from the flexible parts (not just the fixed "MEDIA:"
// prefix) specifically so this can never match into an http(s):// URL --
// real bug found live: with ":" allowed, matching still reached partway
// into remote image URLs (a weather-icon CDN mentioned in an unrelated
// conversation), consuming slots in the match budget for links
// renderMarkdown already displays correctly on its own and /files/read
// could never resolve anyway (it only ever reads a local path).
const PREVIEWABLE_PATH_RE = /(?:MEDIA:)?[^\s"'<>|;&:]*\/[^\s"'<>|;&:]+\.(?:html?|png|jpe?g|gif|svg|webp|ico)\b/gi;

function findPreviewPathsInArgs(args, out, depth, maxCandidates = 8) {
  if (depth > 4 || out.size > maxCandidates) return; // bounded -- args are attacker/model-controlled shape
  if (typeof args === "string") {
    for (const m of args.matchAll(PREVIEWABLE_PATH_RE)) {
      if (out.size > maxCandidates) break;
      const path = m[0].replace(/^MEDIA:/i, "");
      // Excludes remote URLs (http://.../foo.png) -- these are already
      // real, already-working markdown images (renderMarkdown handles
      // ![]()  syntax on its own) or plain links; /files/read only ever
      // resolves a LOCAL path, so a URL here would just be a guaranteed
      // 404 that ate a slot in the match budget for nothing.
      if (path.includes("://")) continue;
      out.add(path);
    }
    return;
  }
  if (Array.isArray(args)) {
    for (const v of args) findPreviewPathsInArgs(v, out, depth + 1, maxCandidates);
    return;
  }
  if (args && typeof args === "object") {
    for (const v of Object.values(args)) findPreviewPathsInArgs(v, out, depth + 1, maxCandidates);
  }
}

// Same detection, run over a bot's REAL persisted history instead of the
// live SSE stream -- real bug found live: the live-stream version alone
// only works for a turn that's actively happening right now; reopening an
// older conversation (or the next 5s poll rebuilding the panel, see
// renderMessages) lost the preview entirely, because it's never part of
// the persisted message content itself. Scans "tool" rows (the actual
// call/result JSON, wherever a real path lives) and "assistant" rows
// (covers a bot just TELLING the user where a file is, like "It's at
// /root/foo.html" or a MEDIA: tag, which is at least as reliable a signal
// as a raw tool argument and needs zero tool-shape knowledge at all). A
// much higher cap than the live-stream case (one whole conversation's
// worth of files, not just one turn's) -- cheap, since each unique path
// is a single cache-backed fetch, not a per-message cost.
// Returns path -> timestamp (the row it was first found in), not just a
// bare list -- real bug found live: rendering every detected preview card
// in one batch after the whole message loop meant a file generated early
// in a long conversation still landed at the very bottom, below messages
// sent long after it -- newest-message-last (the one thing a chat pane
// must always get right) was broken. renderMessages uses this timestamp
// to insert each card right after the message it actually belongs to,
// not always at the end.
function findPreviewPathsInHistory(rawRows) {
  const out = new Map();
  for (const row of rawRows) {
    if (row.role !== "tool" && row.role !== "assistant") continue;
    const found = new Set();
    findPreviewPathsInArgs(extractText(row), found, 0, 40);
    if (!found.size) continue;
    const ts = messageTimestamp(row) || 0;
    for (const path of found) {
      if (!out.has(path)) out.set(path, ts); // first (earliest) occurrence wins
    }
  }
  return out;
}

// Workspace panel's own history scan -- mirrors backend/main.py's
// _touched_paths_from_messages exactly (same tool_calls.function.arguments
// source, same write_file/read_file allowlist) so the frontend's file list
// and the backend's own bound for GET /bots/{name}/workspace/file can never
// drift apart. Unlike findPreviewPathsInHistory above (media-type files
// only, scanned from free-text args), this covers every file type a bot's
// real coding tools touch -- .py, .md, .json, anything -- which is the
// actual gap this panel exists to close (see this session's own findings:
// the existing preview-card path never fires for a plain code file, and
// only ever resolves within /opt/data regardless of file type).
function findWorkspaceFilesInHistory(rawRows) {
  const out = new Map();
  for (const row of rawRows) {
    if (row.role !== "assistant" || !Array.isArray(row.tool_calls)) continue;
    const ts = messageTimestamp(row) || 0;
    for (const call of row.tool_calls) {
      const fn = (call && call.function) || {};
      if (fn.name !== "write_file" && fn.name !== "read_file") continue;
      let args;
      try {
        args = JSON.parse(fn.arguments || "{}");
      } catch (_) {
        continue;
      }
      if (!args.path) continue;
      const existing = out.get(args.path);
      out.set(args.path, {
        content: fn.name === "write_file" ? args.content : (existing && existing.content) || null,
        ts: existing ? existing.ts : ts,
      });
    }
  }
  return out;
}

// Three mutually-exclusive views inside one panel: the file list, a single
// file's content, and (once something's actually listening) a live preview
// iframe -- exactly one visible at a time, same "swap one body element"
// shape #preview-pane already uses for image vs iframe.
function showWorkspaceTab(tab) {
  document.getElementById("workspace-files-list").style.display = tab === "files" ? "" : "none";
  document.getElementById("workspace-file-viewer").style.display = tab === "viewer" ? "flex" : "none";
  document.getElementById("workspace-preview").style.display = tab === "preview" ? "flex" : "none";
  document.getElementById("workspace-tab-files").classList.toggle("active", tab === "files" || tab === "viewer");
  document.getElementById("workspace-tab-preview").classList.toggle("active", tab === "preview");
}

function showWorkspaceFilesList() {
  showWorkspaceTab("files");
}

// Polled lightly while the panel is open -- see bot_processes.
// listening_ports's own docstring for why this needs polling at all
// (the process/terminal tools have no port concept to push an event for).
let workspacePortsPollTimer = null;
let workspaceLivePort = null;

async function pollWorkspacePorts() {
  if (!selected || selected.kind !== "bot") return;
  if (!document.getElementById("workspace-pane").classList.contains("open")) return;
  let ports;
  try {
    ports = await apiGet(`/bots/${selected.id}/workspace/ports`);
  } catch (_) {
    return; // quiet -- same convention as loadMessages's own poll-failure handling
  }
  const port = ports && ports.length ? ports[0].port : null;
  if (port === workspaceLivePort) return;
  workspaceLivePort = port;
  const tab = document.getElementById("workspace-tab-preview");
  if (port) {
    tab.style.display = "";
    document.getElementById("workspace-preview-port").textContent = `Port ${port}`;
    const src = `${API}/bots/${selected.id}/preview/${port}/`;
    document.getElementById("workspace-preview-frame").src = src;
    document.getElementById("workspace-preview-open").href = src;
  } else {
    tab.style.display = "none";
    document.getElementById("workspace-preview-frame").src = "about:blank";
    if (tab.classList.contains("active")) showWorkspaceTab("files");
  }
}

function startWorkspacePortsPolling() {
  stopWorkspacePortsPolling();
  pollWorkspacePorts();
  workspacePortsPollTimer = setInterval(pollWorkspacePorts, 7000);
}

function stopWorkspacePortsPolling() {
  clearInterval(workspacePortsPollTimer);
  workspacePortsPollTimer = null;
  workspaceLivePort = null;
  document.getElementById("workspace-tab-preview").style.display = "none";
}

// Rebuilt from scratch on every call, same convention as renderMessages --
// the underlying workspaceFiles Map is small (one conversation's worth of
// files, not a long scrollback) so a full rebuild is cheap and never risks
// drifting from what workspaceFiles actually holds.
function renderWorkspacePanel() {
  const list = document.getElementById("workspace-files-list");
  list.innerHTML = "";
  const paths = Array.from(workspaceFiles.keys()).sort((a, b) => (workspaceFiles.get(a).ts || 0) - (workspaceFiles.get(b).ts || 0));
  if (!paths.length) {
    const empty = document.createElement("div");
    empty.id = "workspace-files-empty";
    empty.textContent = "No files touched yet.";
    list.appendChild(empty);
    return;
  }
  for (const path of paths) {
    const row = document.createElement("div");
    row.className = "workspace-file-row";
    row.title = path;
    const name = document.createElement("span");
    name.className = "workspace-file-row-name";
    name.textContent = path;
    row.appendChild(name);
    row.addEventListener("click", () => openWorkspaceFile(path));
    list.appendChild(row);
  }
}

async function openWorkspaceFile(path) {
  const cached = workspaceFiles.get(path);
  showWorkspaceTab("viewer");
  document.getElementById("workspace-file-viewer-name").textContent = path;
  const body = document.getElementById("workspace-file-viewer-body");
  if (cached && cached.content != null) {
    body.textContent = cached.content;
    return;
  }
  body.textContent = "Loading…";
  try {
    const result = await apiGet(`/bots/${selected.id}/workspace/file?path=${encodeURIComponent(path)}`);
    if (cached) cached.content = result.content; // fill the cache in place so re-opening is instant
    body.textContent = result.content;
  } catch (e) {
    body.textContent = "Could not load this file.";
  }
}

// data: URIs work fine as an <iframe>/<img> src, but real bug found live:
// clicking "Open full size" (an <a target="_blank" href="data:...">) did
// nothing at all, silently -- Chrome (and other modern browsers) refuse
// to open a data: URI as a new top-level tab, a deliberate anti-phishing
// restriction (a data: page has no real address bar identity). A
// same-origin blob: URL isn't subject to that restriction and opens
// normally, so every consumer of a previewed file's bytes goes through
// one here instead of the raw data_url -- including the iframe/img
// themselves, for the same robustness (a blob avoids re-parsing a
// megabytes-long base64 string as a URL, which some browsers cap).
function dataUrlToBlobUrl(dataUrl) {
  const commaIdx = dataUrl.indexOf(",");
  const header = dataUrl.slice(0, commaIdx);
  const mimeMatch = header.match(/^data:([^;]+);base64$/);
  const mime = mimeMatch ? mimeMatch[1] : "application/octet-stream";
  const binary = atob(dataUrl.slice(commaIdx + 1));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return URL.createObjectURL(new Blob([bytes], { type: mime }));
}

// Revoked on every new preview and on close -- a blob URL otherwise pins
// its backing bytes in memory for the page's whole lifetime, and only
// one preview is ever open at a time so there's never a reason to keep
// more than the current one alive.
let currentPreviewBlobUrl = null;

function releaseCurrentPreviewBlobUrl() {
  if (currentPreviewBlobUrl) {
    URL.revokeObjectURL(currentPreviewBlobUrl);
    currentPreviewBlobUrl = null;
  }
}

// Opens the real file in the side panel (#preview-pane) -- real feedback
// live: rendering a page (or an image) inline at chat-bubble width made
// it unreadable, this needs its own real estate, same reasoning
// #routines-pane already established for "content that doesn't fit the
// message column." One panel, one body element swapped between an
// <iframe> (pages) and an <img> (images/icons) depending on what actually
// came back -- an <img> gives correct aspect-ratio sizing and native
// zoom/save behavior that forcing an image through an iframe would not.
function openPreviewPane(fileInfo, path) {
  const name = fileInfo.name || path;
  document.getElementById("preview-header-name").textContent = name;
  releaseCurrentPreviewBlobUrl();
  const blobUrl = dataUrlToBlobUrl(fileInfo.data_url);
  currentPreviewBlobUrl = blobUrl;

  const openLink = document.getElementById("preview-header-open");
  openLink.href = blobUrl;
  const downloadLink = document.getElementById("preview-header-download");
  downloadLink.href = blobUrl;
  downloadLink.download = name;

  const isImage = (fileInfo.mime_type || "").startsWith("image/");
  const frame = document.getElementById("preview-frame");
  const img = document.getElementById("preview-image");
  if (isImage) {
    frame.style.display = "none";
    frame.src = "about:blank";
    img.style.display = "";
    img.src = blobUrl;
    img.alt = name;
  } else {
    img.style.display = "none";
    img.src = "";
    frame.style.display = "";
    frame.src = blobUrl;
  }
  document.getElementById("preview-pane").classList.add("open");
}

document.getElementById("preview-close-btn").addEventListener("click", () => {
  document.getElementById("preview-pane").classList.remove("open");
  document.getElementById("preview-frame").src = "about:blank";
  document.getElementById("preview-image").src = "";
  releaseCurrentPreviewBlobUrl();
});

// Fetches + builds a preview card element without inserting it anywhere
// -- the caller decides where it goes (appended live during streaming,
// or positioned chronologically among history, see
// insertPreviewCardsAsync). Returns null for a path that turned out not
// to be a real previewable file (a regex guess that didn't pan out).
async function buildPreviewCard(path) {
  let fileInfo = getCachedPreview(path);
  if (fileInfo === undefined) {
    try {
      fileInfo = await apiGet(`/files/read?path=${encodeURIComponent(path)}`);
    } catch (_) {
      fileInfo = null; // the path was a guess (matched by regex, not confirmed) -- cache the miss too
    }
    setCachedPreview(path, fileInfo);
  }
  const mimeType = fileInfo && fileInfo.mime_type;
  const isPreviewable = mimeType && (mimeType.startsWith("text/html") || mimeType.startsWith("image/"));
  if (!fileInfo || !fileInfo.data_url || !isPreviewable) return null;
  const isImage = mimeType.startsWith("image/");

  const card = document.createElement("div");
  card.className = "msg bot msg-pop preview-card";
  card.title = "Click to preview";
  card.addEventListener("click", () => openPreviewPane(fileInfo, path));

  const header = document.createElement("div");
  header.className = "preview-card-header";
  const iconSpan = document.createElement("span");
  iconSpan.className = "preview-card-icon";
  iconSpan.innerHTML = icon(isImage ? "image" : "files", 16);
  header.appendChild(iconSpan);
  const name = document.createElement("span");
  name.className = "preview-card-name";
  name.textContent = fileInfo.name || path;
  header.appendChild(name);
  const hint = document.createElement("span");
  hint.className = "preview-card-hint";
  hint.textContent = isImage ? "Image" : "Preview";
  header.appendChild(hint);
  card.appendChild(header);
  return card;
}

// Live-streaming use only (see streamBotReply) -- one turn's own cards,
// appended in the order awaited, at whatever is currently the pane's
// last child. Safe there specifically because a streaming turn is a
// single linear sequence with no concurrent render of the same pane
// racing it. NOT used for history rendering -- see
// insertPreviewCardsAsync, which positions cards chronologically instead
// of just at the end, and guards against a superseded render.
async function appendPreviewCard(path) {
  const card = await buildPreviewCard(path);
  if (!card) return;
  const pane = document.getElementById("messages-pane");
  pane.appendChild(card);
  pane.scrollTop = pane.scrollHeight;
}

function appendOptimisticUserMessage(text) {
  const pane = document.getElementById("messages-pane");
  const div = document.createElement("div");
  div.id = "optimistic-user-msg";
  div.className = "msg user msg-pop";
  const body = document.createElement("div");
  body.className = "msg-body";
  body.innerHTML = renderMarkdown(text);
  div.appendChild(body);
  pane.appendChild(div);
  pane.scrollTop = pane.scrollHeight;
}

// Consumes the backend's SSE proxy (see stream_to_bot() in main.py) and
// appends each assistant.delta live instead of waiting for the full reply
// to generate server-side -- this is what actually fixes the "goes into
// wait processing on each query" complaint (the desktop app feels fast
// because it streams; this app previously didn't). Falls back
// transparently server-side for bots whose provider can't resolve through
// Hermes' /p/<profile>/ multiplex mirror (a real upstream Hermes bug, not
// fixable here) -- from this function's point of view that just looks
// like one big delta instead of many small ones, no special-casing needed
// on this end.
async function streamBotReply(botName, text, isStillActive) {
  // Real bug found live: without the isStillActive gate below, a delta
  // arriving after the user switched to a DIFFERENT chat would render
  // status/log updates against whatever #messages-pane currently shows --
  // i.e. bot A's reply visibly streaming into bot B's window.
  // The old blocking send never had this problem because it never touched
  // the DOM until the final, already-gated loadMessages() call. The fetch
  // itself is never aborted on a switch-away -- the reply still needs to
  // reach the server and land in bot A's real session; only the DOM
  // writes are suppressed once the user has moved on.
  const res = await fetch(`${API}/bots/${botName}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  const previewPathCandidates = new Set();
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const rawEvent = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let eventName = "message";
      let dataStr = "";
      for (const line of rawEvent.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
      }
      if (!dataStr) continue;
      let payload;
      try {
        payload = JSON.parse(dataStr);
      } catch (_) {
        continue;
      }
      if (!isStillActive()) continue;
      if (eventName === "assistant.delta" && payload.delta) {
        // Never rendered live (see getThinkingPanel's own comment) -- just
        // flips the ambient status to something honest while tokens are
        // actually arriving.
        hideTypingIndicator();
        setThinkingSummary("Responding…");
      } else if (eventName === "tool.started") {
        hideTypingIndicator();
        logThinkingStep(toolStatusLabel(payload.tool_name, payload.preview, payload.args));
        findPreviewPathsInArgs(payload.args, previewPathCandidates, 0);
        if (
          (payload.tool_name === "write_file" || payload.tool_name === "read_file") &&
          payload.args &&
          payload.args.path
        ) {
          const existing = workspaceFiles.get(payload.args.path);
          workspaceFiles.set(payload.args.path, {
            content: payload.tool_name === "write_file" ? payload.args.content : (existing && existing.content) || null,
            ts: existing ? existing.ts : Date.now() / 1000,
          });
          if (document.getElementById("workspace-pane").classList.contains("open")) renderWorkspacePanel();
        }
      } else if (eventName === "tool.progress" && payload.tool_name === "_thinking") {
        hideTypingIndicator();
        logThinkingStep("Thinking");
      } else if (eventName === "assistant.completed") {
        // The one and only source of the visible reply -- see this
        // function's own header comment for why deltas never render
        // directly. content is the real handler's full, final text
        // regardless of how many delta/tool cycles preceded it.
        hideTypingIndicator();
        finalizeThinkingPanel();
        if (typeof payload.content === "string" && payload.content.trim()) {
          appendFinalBotMessage(payload.content);
        }
        for (const path of previewPathCandidates) {
          await appendPreviewCard(path);
        }
      } else if (eventName === "run.completed" || eventName === "error") {
        hideTypingIndicator();
        finalizeThinkingPanel();
      }
    }
  }
  hideTypingIndicator();
  finalizeThinkingPanel();
}

function extractText(row) {
  if (typeof row.content === "string") return row.content;
  if (Array.isArray(row.content)) {
    return row.content.map((c) => (typeof c === "string" ? c : c.text || "")).join("");
  }
  return row.text || "";
}

// Marker prefix for a "user" turn that's actually an internal trigger (a
// scheduled routine's own instruction text, e.g. "This is your scheduled
// 5-minute check-in trigger...") rather than something the user typed.
// Real bug found live: a routine set up to have a bot proactively check in
// makes that trigger text land as a genuine, persisted user-role message
// (message_bot posts it the same way a real chat message arrives, and a
// session legitimately needs SOME inbound turn to answer) -- with no
// marker, the raw internal prompt rendered as if the user had typed it
// themselves. The trigger still exists in the real data (nothing about
// delivery changes) -- this only controls what's DISPLAYED, matching
// collapseToFinalTurns' own job of hiding real turns that aren't meant
// for the user to see.
const INTERNAL_TRIGGER_MARKER = "[internal-trigger]";

function isInternalTrigger(row) {
  return row.role === "user" && extractText(row).trimStart().startsWith(INTERNAL_TRIGGER_MARKER);
}

function collapseToFinalTurns(rows) {
  // Between two user messages (or from the start of the thread to the
  // end), the agent loop can take several assistant turns before its
  // real answer -- narrating a plan, calling a tool, narrating the
  // result, calling another tool, and so on. Each of those is a
  // genuine, complete assistant turn, indistinguishable in shape from a
  // real final answer, so without this every intermediate "let me
  // check..." renders as its own bubble. Keep only the LAST non-empty
  // assistant turn in each run -- that's always the one that actually
  // answers the user; a pure tool-call trigger has empty content and is
  // dropped outright, not just collapsed.
  const collapsed = [];
  let pendingAssistant = null;
  for (const row of rows) {
    if (isInternalTrigger(row)) {
      // Ends the previous turn (same as a real user row would) but never
      // renders itself -- the reply that follows shows up as if it just
      // arrived on its own, which is exactly right for a proactive
      // check-in nobody was supposed to "ask" for.
      if (pendingAssistant) collapsed.push(pendingAssistant);
      pendingAssistant = null;
    } else if (row.role === "user") {
      if (pendingAssistant) collapsed.push(pendingAssistant);
      pendingAssistant = null;
      collapsed.push(row);
    } else if (row.role === "assistant") {
      if (extractText(row).trim()) pendingAssistant = row;
    }
  }
  if (pendingAssistant) collapsed.push(pendingAssistant);
  return collapsed;
}

async function loadMessages(fromPoll = false) {
  if (!selected) return;
  // A background poll firing mid-send would rebuild the pane from
  // server state that doesn't have the in-flight message yet, wiping the
  // optimistic bubble until the real round trip finishes. Only the
  // deliberate post-send call (fromPoll=false) is allowed to run then.
  if (fromPoll && sendingKeys.has(chatKey(selected))) return;
  try {
    if (selected.kind === "bot") {
      const rows = await apiGet(`/bots/${selected.id}/messages`);
      // A poll whose response is byte-identical to what's already on
      // screen has nothing to do -- skip the render entirely rather than
      // wiping and rebuilding a pane that would come out looking exactly
      // the same. lastRenderedCount === -1 (fresh thread open) always
      // renders regardless, since there's nothing on screen yet to compare
      // against.
      const sig = JSON.stringify(rows);
      if (lastRenderedCount !== -1 && sig === lastMessagesSignature) return;
      lastMessagesSignature = sig;
      // Scanned from the UNFILTERED rows -- "tool" rows carry the actual
      // call/result content a preview path lives in, and get dropped by
      // the very next line's filter (collapseToFinalTurns never sees them
      // either), so this has to happen before that filter runs.
      const previewPaths = findPreviewPathsInHistory(rows || []);
      workspaceFiles = findWorkspaceFilesInHistory(rows || []);
      if (document.getElementById("workspace-pane").classList.contains("open")) renderWorkspacePanel();
      const chatRows = collapseToFinalTurns((rows || []).filter((r) => r.role === "user" || r.role === "assistant"));
      renderMessages(chatRows, "bot", previewPaths);
    } else {
      const rows = await apiGet(`/groups/${selected.id}/messages`);
      const sig = JSON.stringify(rows);
      if (lastRenderedCount !== -1 && sig === lastMessagesSignature) return;
      lastMessagesSignature = sig;
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
  // Snapshot which chat this send is actually for -- the user may switch
  // to a different bot before the reply comes back, and every UI effect
  // below (optimistic bubble, typing indicator, error toast, message
  // reload) must apply to THIS chat, not whatever happens to be selected
  // when the response lands.
  const target = selected;
  const key = chatKey(target);
  if (!target) return;
  const input = document.getElementById("composer-input");
  const text = input.value.trim();
  if (!text) return;

  // Real gap found live: sending while this exact bot's own reply was
  // still streaming used to be silently dropped entirely (the guard below
  // just returned early) -- no way to redirect a bot mid-work the way the
  // real hermes-agent desktop app's own steering does (confirmed live:
  // a real, pending mid-build steering message queued against a real
  // 21.5-hour session). A bot actively streaming gets routed to /steer
  // instead of blocked; once it goes quiet, this falls through to the
  // normal send path below exactly as before. Groups have no single "run"
  // to steer, so they keep the original blocked-while-sending behavior.
  if (target.kind === "bot" && sendingKeys.has(key)) {
    input.value = "";
    appendOptimisticUserMessage(text);
    try {
      const result = await apiSend("POST", `/bots/${target.id}/steer`, { text });
      if (!result.steered && chatKey(selected) === key) {
        // The run had already finished in the gap between this click and
        // the steer actually landing -- the backend's own fallback
        // already sent it as a normal message and has the real reply;
        // reload to show it like any other send would.
        await loadMessages();
      }
      await refreshRoster();
    } catch (e) {
      if (chatKey(selected) === key) {
        document.getElementById("optimistic-user-msg")?.remove();
        input.value = text;
      }
      toast(`Send to ${target.id} failed: ` + e.message);
    }
    return;
  }

  if (sendingKeys.has(key)) return;
  sendingKeys.add(key);
  const isStillActive = () => chatKey(selected) === key;
  input.value = "";
  updateComposerState();
  // Show the user's own message the instant they hit send, rather than
  // waiting on the full round trip (bot reply, possibly a corruption
  // retry on top of that) before it appears at all. Safe unconditionally
  // here -- target is always the active chat at this exact point, the
  // switch-away case only matters once we're past the await below.
  appendOptimisticUserMessage(text);
  showTypingIndicator();
  try {
    if (target.kind === "bot") {
      await streamBotReply(target.id, text, isStillActive);
    } else {
      await apiSend("POST", `/groups/${target.id}/messages`, { text, sender: "user" });
    }
    if (isStillActive()) await loadMessages();
    await refreshRoster();
  } catch (e) {
    if (isStillActive()) {
      hideTypingIndicator();
      document.getElementById("optimistic-user-msg")?.remove();
      document.getElementById("thinking-wrap")?.remove();
      input.value = text;
    }
    toast(`Send to ${target.id} failed: ` + e.message);
  } finally {
    sendingKeys.delete(key);
    updateComposerState();
  }
}

// ---------------------------------------------------------------------
// Routines
// ---------------------------------------------------------------------

document.getElementById("routines-toggle-btn").addEventListener("click", async () => {
  const pane = document.getElementById("routines-pane");
  pane.classList.toggle("open");
  if (pane.classList.contains("open") && selected && selected.kind === "bot") {
    populateRoutineTargets(selected.id);
    await loadRoutines(selected.id);
  }
});

document.getElementById("workspace-toggle-btn").addEventListener("click", () => {
  const pane = document.getElementById("workspace-pane");
  pane.classList.toggle("open");
  if (pane.classList.contains("open")) {
    renderWorkspacePanel();
    startWorkspacePortsPolling();
  } else {
    stopWorkspacePortsPolling();
  }
});
document.getElementById("workspace-close-btn").addEventListener("click", () => {
  document.getElementById("workspace-pane").classList.remove("open");
  stopWorkspacePortsPolling();
});
document.getElementById("workspace-file-viewer-back").addEventListener("click", showWorkspaceFilesList);
document.getElementById("workspace-tab-files").addEventListener("click", showWorkspaceFilesList);
document.getElementById("workspace-tab-preview").addEventListener("click", () => showWorkspaceTab("preview"));

// Cross-bot nudges (one bot's routine delivering into ANOTHER bot's chat)
// use Hermes' own real "bot-chat:<profile>" cron delivery lane -- see
// create_routine's own comment for why. Every other live bot is a valid
// target; the current one is left out since that's just the default
// "this bot's chat" option already in the list.
function populateRoutineTargets(currentBotId) {
  const sel = document.getElementById("routine-target");
  sel.innerHTML = '<option value="">Deliver to: this bot\'s chat</option>';
  roster
    .filter((r) => r.name !== currentBotId)
    .forEach((r) => {
      const opt = document.createElement("option");
      opt.value = r.name;
      opt.textContent = "Deliver to: " + r.title;
      sel.appendChild(opt);
    });
}
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

function _routineDeliverLabel(targetBot) {
  if (!targetBot) return "";
  const target = roster.find((r) => r.name === targetBot);
  return "-> " + (target ? target.title : targetBot);
}

function routineCard(job) {
  const card = document.createElement("div");
  card.className = "routine-card";
  const name = document.createElement("div");
  name.className = "routine-card-name";
  name.textContent = (job.name || "").replace(/^\[bot:[a-zA-Z0-9_-]+(?:->[a-zA-Z0-9_-]+)?\]\s*/, "");
  const schedule = document.createElement("div");
  schedule.className = "routine-card-schedule";
  // job.schedule is a structured object ({kind, run_at, display}, per
  // Hermes' own cron job shape) -- job.schedule_display is the ready-made
  // human string. Real bug found live: this used to read job.schedule
  // directly, rendering the literal "[object Object]" in every routine
  // card instead of e.g. "once in 1m".
  schedule.textContent = job.schedule_display || "";
  card.append(name, schedule);

  const target = _routineDeliverLabel(job.target_bot);
  if (target) {
    const targetEl = document.createElement("div");
    targetEl.className = "routine-card-target";
    targetEl.textContent = target;
    card.appendChild(targetEl);
  }

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
  const target_bot = document.getElementById("routine-target").value || undefined;
  try {
    await apiSend("POST", `/bots/${selected.id}/routines`, { routine, prompt, schedule, target_bot });
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
  lastRenderedCount = -1;
  lastMessagesSignature = null;
  document.body.classList.add("chat-open");
  document.getElementById("routines-pane").classList.remove("open");
  const group = groups.find((g) => g.id === id);
  document.getElementById("chat-header-avatar").innerHTML =
    `<svg viewBox="0 0 100 100"><rect width="100" height="100" fill="#111"/><circle cx="38" cy="42" r="22" fill="#f2f2f2"/><circle cx="66" cy="55" r="18" fill="#9a9a9a"/></svg>`;
  document.getElementById("chat-header-title").textContent = group ? group.name : "Group";
  const offlineEl = document.getElementById("chat-header-offline");
  if (offlineEl) offlineEl.style.display = "none";
  document.getElementById("chat-header-model").style.display = "none";
  document.getElementById("chat-header-desc").textContent = group ? group.members.join(", ") : "";
  document.getElementById("composer-input").placeholder = "Message the group (@name to address one bot)";
  showChatView();
  renderRoster();
  await loadMessages();
  updateComposerState();
  if (sendingKeys.has(chatKey(selected))) showTypingIndicator();
  clearInterval(messagesPollTimer);
  messagesPollTimer = setInterval(() => loadMessages(true), 5000);
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
      row.style.gap = "0.5rem";
      const label = document.createElement("div");
      label.textContent = `${g.name} (${g.members.join(", ")})`;
      label.style.fontSize = "0.85rem";
      label.style.flex = "1";
      const actions = document.createElement("div");
      actions.style.display = "flex";
      actions.style.gap = "0.4rem";
      const edit = document.createElement("button");
      edit.className = "btn";
      edit.textContent = "Edit";
      edit.addEventListener("click", () => {
        document.getElementById("groups-modal-backdrop").classList.remove("open");
        openGroupEditModal(g);
      });
      const del = document.createElement("button");
      del.className = "btn";
      del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete group "${g.name}"?`)) return;
        await apiSend("DELETE", `/groups/${g.id}`);
        await openGroupsModal();
        renderRoster();
      });
      actions.append(edit, del);
      row.append(label, actions);
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
// Group editing
// ---------------------------------------------------------------------

let editingGroup = null;

function fillGroupMemberChecks(wrap, selectedMembers) {
  wrap.innerHTML = "";
  roster.forEach((r) => {
    const label = document.createElement("label");
    label.style.display = "flex";
    label.style.alignItems = "center";
    label.style.gap = "0.4rem";
    label.style.marginBottom = "0.3rem";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = r.name;
    cb.checked = selectedMembers.includes(r.name);
    label.append(cb, document.createTextNode(r.title));
    wrap.appendChild(label);
  });
}

function openGroupEditModal(group) {
  editingGroup = group;
  document.getElementById("group-edit-name").value = group.name || "";
  fillGroupMemberChecks(document.getElementById("group-edit-member-checks"), group.members || []);
  document.getElementById("group-edit-modal-backdrop").classList.add("open");
}

document.getElementById("group-edit-cancel").addEventListener("click", () => {
  document.getElementById("group-edit-modal-backdrop").classList.remove("open");
});

document.getElementById("group-edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!editingGroup) return;
  const name = document.getElementById("group-edit-name").value.trim();
  const members = [...document.querySelectorAll("#group-edit-member-checks input:checked")].map((el) => el.value);
  if (members.length < 2) {
    toast("Pick at least 2 members.");
    return;
  }
  try {
    await apiSend("PATCH", `/groups/${editingGroup.id}`, { name, members });
    toast("Group updated");
    document.getElementById("group-edit-modal-backdrop").classList.remove("open");
    await refreshGroups();
    renderRoster();
    if (selected && selected.kind === "group" && selected.id === editingGroup.id) {
      const updated = groups.find((g) => g.id === editingGroup.id);
      if (updated) {
        document.getElementById("chat-header-title").textContent = updated.name;
        document.getElementById("chat-header-desc").textContent = updated.members.join(", ");
      }
    }
  } catch (err) {
    toast("Failed: " + err.message);
  }
});

document.getElementById("group-edit-delete").addEventListener("click", async () => {
  if (!editingGroup) return;
  if (!confirm(`Delete group "${editingGroup.name}"?`)) return;
  try {
    await apiSend("DELETE", `/groups/${editingGroup.id}`);
    document.getElementById("group-edit-modal-backdrop").classList.remove("open");
    if (selected && selected.kind === "group" && selected.id === editingGroup.id) {
      selected = null;
      showEmptyMain();
    }
    toast("Group deleted");
    await refreshGroups();
    renderRoster();
  } catch (err) {
    toast("Failed: " + err.message);
  }
});

function openGroupContextMenu(x, y, group) {
  const menu = document.getElementById("context-menu");
  menu.innerHTML = "";
  const items = [
    { label: "Edit group", action: () => openGroupEditModal(group) },
    { sep: true },
    { label: "Delete group", danger: true, action: async () => {
      if (!confirm(`Delete group "${group.name}"?`)) return;
      try {
        await apiSend("DELETE", `/groups/${group.id}`);
        if (selected && selected.kind === "group" && selected.id === group.id) {
          selected = null;
          showEmptyMain();
        }
        toast("Group deleted");
        await refreshGroups();
        renderRoster();
      } catch (e) {
        toast("Failed: " + e.message);
      }
    } },
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

async function refreshNotifyBtn() {
  const btn = document.getElementById("notify-btn");
  if (!pushSupported()) {
    btn.style.display = "none";
    return;
  }
  const enabled = await isPushEnabled();
  btn.innerHTML = icon(enabled ? "bell" : "bell-off", 16);
  btn.classList.toggle("active", enabled);
  btn.title = enabled ? "Reminder notifications on -- click to disable" : "Enable reminder notifications";
}

document.getElementById("notify-btn").addEventListener("click", async () => {
  if (await isPushEnabled()) {
    await disablePushNotifications();
  } else {
    await enablePushNotifications();
  }
  refreshNotifyBtn();
});

[
  ["bot-modal-backdrop", "bot-modal-cancel"],
  ["dup-modal-backdrop", null],
  ["avatar-modal-backdrop", null],
  ["groups-modal-backdrop", null],
  ["group-edit-modal-backdrop", null],
].forEach(([backdropId]) => {
  document.getElementById(backdropId).addEventListener("click", (e) => {
    if (e.target.id === backdropId) e.target.classList.remove("open");
  });
});

document.getElementById("chat-back-btn").addEventListener("click", backToRoster);

document.getElementById("chat-menu-btn").addEventListener("click", (e) => {
  if (!selected) return;
  const rect = e.currentTarget.getBoundingClientRect();
  if (selected.kind === "bot") {
    const entry = roster.find((r) => r.name === selected.id);
    if (!entry) return;
    openBotContextMenu(rect.right - 170, rect.bottom + 6, entry);
  } else {
    const group = groups.find((g) => g.id === selected.id);
    if (!group) return;
    openGroupContextMenu(rect.right - 170, rect.bottom + 6, group);
  }
});

// ---------------------------------------------------------------------
// Retry last / export conversation
// ---------------------------------------------------------------------

async function retryLastMessage() {
  if (!selected) return;
  let rows;
  try {
    rows = selected.kind === "bot"
      ? await apiGet(`/bots/${selected.id}/messages`)
      : await apiGet(`/groups/${selected.id}/messages`);
  } catch (e) {
    toast("Could not load messages to retry: " + e.message);
    return;
  }
  const lastUser = [...(rows || [])].reverse().find((r) => r.role === "user" || r.from === "user");
  if (!lastUser) {
    toast("No user message to retry.");
    return;
  }
  const text = selected.kind === "bot" ? extractText(lastUser) : lastUser.text || "";
  if (!text) return;
  const input = document.getElementById("composer-input");
  input.value = text;
  await sendComposerMessage();
}

document.getElementById("retry-btn").addEventListener("click", retryLastMessage);

async function exportConversation() {
  if (!selected) return;
  const name = selected.kind === "bot"
    ? (roster.find((r) => r.name === selected.id)?.title || selected.id)
    : (groups.find((g) => g.id === selected.id)?.name || "group");
  let rows;
  try {
    rows = selected.kind === "bot"
      ? await apiGet(`/bots/${selected.id}/messages`)
      : await apiGet(`/groups/${selected.id}/messages`);
  } catch (e) {
    toast("Export failed: " + e.message);
    return;
  }
  const lines = [`# ${name}`, ""];
  (rows || []).forEach((row) => {
    const who = selected.kind === "bot"
      ? (row.role === "user" ? "You" : name)
      : (row.from === "user" ? "You" : row.from);
    const text = selected.kind === "bot" ? extractText(row) : row.text || "";
    const ts = row.timestamp || row.ts;
    const stamp = ts ? new Date(ts * 1000).toISOString() : "";
    lines.push(`## ${who}${stamp ? ` (${stamp})` : ""}`, "", text, "");
  });
  const safeName = name.replace(/[^a-z0-9_-]+/gi, "_");
  downloadTextFile(`${safeName}.md`, lines.join("\n"), "text/markdown;charset=utf-8");
}

document.getElementById("export-btn").addEventListener("click", exportConversation);

function populateIcons() {
  document.getElementById("chat-back-btn").innerHTML = icon("back", 16);
  document.getElementById("show-hidden-btn").innerHTML = icon("eye", 16);
  document.getElementById("groups-btn").innerHTML = icon("group", 16);
  document.getElementById("new-bot-btn").innerHTML = icon("plus", 15) + "<span>New</span>";
  document.getElementById("retry-btn").innerHTML = icon("retry", 16);
  document.getElementById("export-btn").innerHTML = icon("download", 16);
  document.getElementById("routines-toggle-btn").innerHTML = icon("clock", 16);
  document.getElementById("workspace-toggle-btn").innerHTML = icon("files", 16);
  document.getElementById("chat-menu-btn").innerHTML = icon("more", 16);
  document.getElementById("routines-close-btn").innerHTML = icon("close", 15);
  document.getElementById("workspace-close-btn").innerHTML = icon("close", 15);
  document.getElementById("workspace-file-viewer-back").innerHTML = icon("back", 15);
  document.getElementById("preview-close-btn").innerHTML = icon("close", 15);
  document.getElementById("preview-header-download").innerHTML = icon("download", 15);
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
  refreshNotifyBtn(); // best-effort, never blocks the rest of boot on a slow/unsupported check
}

boot();
