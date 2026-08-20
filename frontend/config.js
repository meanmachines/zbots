"use strict";

async function loadConfig() {
  try {
    const res = await apiGet("/config/raw");
    document.getElementById("config-editor").value = res.yaml || "";
    document.getElementById("config-path").textContent = res.path || "";
  } catch (e) {
    toast("Failed to load config: " + e.message);
  }
}

document.getElementById("save-config-btn").addEventListener("click", async () => {
  if (!confirm("Save changes to config.yaml? A malformed edit can break the gateway.")) return;
  const yamlText = document.getElementById("config-editor").value;
  try {
    await apiSend("PUT", "/config/raw", { yaml_text: yamlText });
    toast("Config saved");
  } catch (e) {
    toast("Failed: " + e.message);
  }
});

renderShell("config");
document.getElementById("save-config-btn").innerHTML = icon("check", 15) + "<span>Save</span>";
loadConfig();
