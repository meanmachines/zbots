"use strict";

async function loadWebhooks() {
  const list = document.getElementById("webhooks-list");
  const sub = document.getElementById("webhooks-sub");
  let res;
  try {
    res = await apiGet("/webhooks");
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
    return;
  }

  if (!res.enabled) {
    sub.textContent = "The webhook platform is currently disabled.";
    list.innerHTML = "";
    const enableBtn = document.createElement("button");
    enableBtn.className = "btn primary";
    enableBtn.textContent = "Enable webhooks";
    enableBtn.addEventListener("click", async () => {
      try {
        const r = await apiSend("POST", "/webhooks/enable");
        toast(r.needs_restart ? "Enabled -- restart the gateway to apply" : "Webhooks enabled");
        await loadWebhooks();
      } catch (e) {
        toast("Failed: " + e.message);
      }
    });
    list.appendChild(enableBtn);
    return;
  }

  sub.textContent = `Base URL: ${res.base_url || "-"}`;
  const subs = res.subscriptions || [];
  list.innerHTML = "";
  if (!subs.length) {
    list.innerHTML = '<div class="empty-state">No webhooks configured yet.</div>';
    return;
  }
  const grid = document.createElement("div");
  grid.className = "card-grid";
  subs.forEach((w) => grid.appendChild(webhookCard(w)));
  list.appendChild(grid);
}

function webhookCard(w) {
  const name = w.name || w.id || "unnamed";
  const card = document.createElement("div");
  card.className = "item-card";
  const title = document.createElement("div");
  title.className = "item-card-title";
  title.textContent = name;
  const sub = document.createElement("div");
  sub.className = "item-card-sub";
  sub.textContent = w.description || w.url || "";
  card.append(title, sub);

  const actions = document.createElement("div");
  actions.className = "item-card-actions";
  const delBtn = document.createElement("button");
  delBtn.className = "btn danger btn-icon-label";
  delBtn.innerHTML = icon("trash", 14) + "<span>Delete</span>";
  delBtn.addEventListener("click", async () => {
    if (!confirm(`Delete webhook "${name}"?`)) return;
    try {
      await apiSend("DELETE", `/webhooks/${encodeURIComponent(name)}`);
      toast("Deleted");
      await loadWebhooks();
    } catch (e) {
      toast("Failed: " + e.message);
    }
  });
  actions.appendChild(delBtn);
  card.appendChild(actions);
  return card;
}

document.getElementById("add-webhook-btn").addEventListener("click", () => {
  document.getElementById("webhook-form").reset();
  document.getElementById("webhook-modal-backdrop").classList.add("open");
});
document.getElementById("webhook-modal-cancel").addEventListener("click", () => {
  document.getElementById("webhook-modal-backdrop").classList.remove("open");
});
document.getElementById("webhook-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "webhook-modal-backdrop") e.target.classList.remove("open");
});

document.getElementById("webhook-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: document.getElementById("webhook-name").value.trim(),
    description: document.getElementById("webhook-description").value.trim() || undefined,
    prompt: document.getElementById("webhook-prompt").value.trim() || undefined,
  };
  try {
    await apiSend("POST", "/webhooks", body);
    toast(`${body.name} created`);
    document.getElementById("webhook-modal-backdrop").classList.remove("open");
    await loadWebhooks();
  } catch (err) {
    toast("Failed: " + err.message);
  }
});

renderShell("webhooks");
document.getElementById("add-webhook-btn").innerHTML = icon("plus", 15) + "<span>New webhook</span>";
loadWebhooks();
