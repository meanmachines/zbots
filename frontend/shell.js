"use strict";

// Shared nav rail rendered into #shell-nav on every page. Each page calls
// renderShell("<page-id>") once on load.

const NAV_GROUPS = [
  {
    label: "Agent",
    items: [
      { id: "chat", label: "Chat (classic)", href: "/", icon: "chat" },
      { id: "bots", label: "Bots", href: "/bots/", icon: "bots" },
      { id: "sessions", label: "Sessions", href: "/bots/sessions.html", icon: "sessions" },
      { id: "cron", label: "Routines", href: "/bots/cron.html", icon: "cron" },
      { id: "skills", label: "Skills", href: "/bots/skills.html", icon: "skills" },
    ],
  },
  {
    label: "Configuration",
    items: [
      { id: "models", label: "Models", href: "/bots/models.html", icon: "models" },
      { id: "mcp", label: "MCP Servers", href: "/bots/mcp.html", icon: "mcp" },
      { id: "config", label: "Config", href: "/bots/config.html", icon: "config" },
      { id: "env", label: "Environment", href: "/bots/env.html", icon: "env" },
    ],
  },
  {
    label: "Platform",
    items: [
      { id: "plugins", label: "Plugins", href: "/bots/plugins.html", icon: "plugins" },
      { id: "webhooks", label: "Webhooks", href: "/bots/webhooks.html", icon: "webhooks" },
      { id: "files", label: "Files", href: "/bots/files.html", icon: "files" },
      { id: "logs", label: "Logs", href: "/bots/logs.html", icon: "logs" },
      { id: "system", label: "System", href: "/bots/system.html", icon: "system" },
    ],
  },
];

function renderShell(activeId) {
  const nav = document.getElementById("shell-nav");
  if (!nav) return;

  const brand = document.createElement("div");
  brand.id = "shell-nav-brand";
  brand.innerHTML = `${icon("system", 20)}<span>Hermes</span>`;
  nav.appendChild(brand);

  let activeLabel = "Hermes";
  NAV_GROUPS.forEach((group) => {
    const label = document.createElement("div");
    label.className = "shell-nav-group-label";
    label.textContent = group.label;
    nav.appendChild(label);

    group.items.forEach((item) => {
      const a = document.createElement("a");
      a.className = "shell-nav-link" + (item.id === activeId ? " active" : "");
      a.href = item.href;
      a.innerHTML = `${icon(item.icon, 17)}<span>${item.label}</span>`;
      nav.appendChild(a);
      if (item.id === activeId) activeLabel = item.label;
    });
  });

  // Mobile-only top bar (hidden on desktop by CSS) that toggles the nav
  // rail as a slide-in drawer, since there's no room for a 210px rail
  // alongside real content on a phone screen.
  const page = document.getElementById("shell-page");
  const topbar = document.createElement("div");
  topbar.id = "shell-mobile-topbar";
  topbar.innerHTML = `<button id="shell-nav-toggle" class="icon-btn" title="Menu"></button><span id="shell-mobile-title"></span>`;
  page.insertBefore(topbar, page.firstChild);
  document.getElementById("shell-nav-toggle").innerHTML = icon("menu", 18);
  document.getElementById("shell-mobile-title").textContent = activeLabel;

  const backdrop = document.createElement("div");
  backdrop.id = "shell-nav-backdrop";
  document.body.appendChild(backdrop);

  function closeNav() {
    nav.classList.remove("open");
    backdrop.classList.remove("open");
  }
  document.getElementById("shell-nav-toggle").addEventListener("click", () => {
    nav.classList.toggle("open");
    backdrop.classList.toggle("open");
  });
  backdrop.addEventListener("click", closeNav);
}
