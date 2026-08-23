"use strict";

// Chat-platform connectors (Telegram, Discord, WhatsApp, Slack, ...) --
// thin client over hermes-agent's own real messaging-platform API (see
// backend/main.py's /connectors routes). The catalog, field metadata
// (prompt/help/required/is_password), and state machine all come straight
// from that API; this page renders whatever it returns rather than
// hardcoding a platform list, so a newly installed plugin platform shows
// up here with zero frontend changes.

const STATE_LABELS = {
  disabled: "Disabled",
  not_configured: "Needs setup",
  pending_restart: "Saved -- restart pending",
  gateway_stopped: "Gateway not running",
  startup_failed: "Failed to start",
};

function statePillClass(state) {
  if (state === "disabled") return "off";
  if (!state || state === "not_configured" || state === "pending_restart" || state === "gateway_stopped" || state === "startup_failed") {
    return "warn";
  }
  return "on";
}

async function loadConnectors() {
  const list = document.getElementById("connectors-list");
  const notice = document.getElementById("connectors-gateway-notice");
  try {
    const res = await apiGet("/connectors");
    const platforms = res.platforms || [];
    notice.style.display = "none";
    if (platforms.length && platforms.every((p) => !p.gateway_running)) {
      notice.style.display = "";
      notice.className = "empty-state";
      notice.textContent =
        "The messaging gateway isn't running, so connectors can be configured here but won't actually connect yet. Ask an admin to start it (" +
        (res.gateway_start_command || "hermes gateway run") + ").";
    }
    list.innerHTML = "";
    if (!platforms.length) {
      list.innerHTML = '<div class="empty-state">No connector platforms available.</div>';
      return;
    }
    const grid = document.createElement("div");
    grid.className = "card-grid";
    platforms.forEach((p) => grid.appendChild(connectorCard(p)));
    list.appendChild(grid);
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
  }
}

function connectorCard(p) {
  const card = document.createElement("div");
  card.className = "item-card";

  const title = document.createElement("div");
  title.className = "item-card-title";
  title.style.display = "flex";
  title.style.alignItems = "center";
  title.style.gap = "0.4rem";
  title.textContent = p.name;
  const pill = document.createElement("span");
  pill.className = "pill " + statePillClass(p.state);
  pill.textContent = STATE_LABELS[p.state] || (p.state ? p.state[0].toUpperCase() + p.state.slice(1) : "Connected");
  title.appendChild(pill);
  card.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "item-card-sub";
  sub.textContent = p.description || "";
  card.appendChild(sub);

  if (p.error_message) {
    const err = document.createElement("div");
    err.style.cssText = "font-size:0.78rem; color:var(--danger); margin-top:0.3rem;";
    err.textContent = p.error_message;
    card.appendChild(err);
  }

  const fields = document.createElement("div");
  fields.style.marginTop = "0.6rem";
  const inputs = {};
  (p.env_vars || []).forEach((ev) => {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = ev.prompt || ev.key;
    if (ev.required) label.textContent += " *";
    field.appendChild(label);
    const input = document.createElement("input");
    input.type = ev.is_password ? "password" : "text";
    input.placeholder = ev.is_set ? `Set (${ev.redacted_value || "hidden"}) -- leave blank to keep` : ev.description || "";
    inputs[ev.key] = input;
    field.appendChild(input);
    if (ev.help || ev.url) {
      const hint = document.createElement("div");
      hint.style.cssText = "font-size:0.72rem; color:var(--text-muted); margin-top:0.2rem;";
      hint.textContent = ev.help || "";
      if (ev.url) {
        const a = document.createElement("a");
        a.href = ev.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = (ev.help ? " " : "") + "Get one here";
        a.style.color = "var(--primary)";
        hint.appendChild(a);
      }
      field.appendChild(hint);
    }
    fields.appendChild(field);
  });
  card.appendChild(fields);

  const actions = document.createElement("div");
  actions.className = "item-card-actions";

  const saveBtn = document.createElement("button");
  saveBtn.className = "btn btn-icon-label";
  saveBtn.innerHTML = icon("check", 14) + "<span>Save</span>";
  saveBtn.addEventListener("click", async () => {
    const env = {};
    for (const [key, input] of Object.entries(inputs)) {
      if (input.value.trim()) env[key] = input.value.trim();
    }
    saveBtn.disabled = true;
    try {
      const result = await apiSend("PUT", `/connectors/${encodeURIComponent(p.id)}`, { env });
      toast(`${p.name} saved`);
      if (result && result.confirm_required) {
        toast(result.confirm_message || "Confirmation required -- see the desktop app for advanced changes.");
      }
      await loadConnectors();
    } catch (e) {
      toast("Save failed: " + e.message);
    } finally {
      saveBtn.disabled = false;
    }
  });

  const toggleBtn = document.createElement("button");
  toggleBtn.className = "btn btn-icon-label";
  toggleBtn.innerHTML = icon(p.enabled ? "pause" : "play", 14) + `<span>${p.enabled ? "Disable" : "Enable"}</span>`;
  toggleBtn.addEventListener("click", async () => {
    toggleBtn.disabled = true;
    try {
      await apiSend("PUT", `/connectors/${encodeURIComponent(p.id)}`, { enabled: !p.enabled });
      toast(`${p.name} ${p.enabled ? "disabled" : "enabled"}`);
      await loadConnectors();
    } catch (e) {
      toast("Failed: " + e.message);
    } finally {
      toggleBtn.disabled = false;
    }
  });

  const testBtn = document.createElement("button");
  testBtn.className = "btn btn-icon-label";
  testBtn.innerHTML = icon("search", 14) + "<span>Test</span>";
  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    try {
      const res = await apiSend("POST", `/connectors/${encodeURIComponent(p.id)}/test`);
      toast(res.ok || res.success ? "Connection OK" : res.message || res.error || "Test failed");
    } catch (e) {
      toast("Test failed: " + e.message);
    } finally {
      testBtn.disabled = false;
    }
  });

  actions.append(saveBtn, toggleBtn, testBtn);
  card.appendChild(actions);
  return card;
}

renderShell("connectors");
loadConnectors();
