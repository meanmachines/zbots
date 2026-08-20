"use strict";

// Minimal line-icon set (24x24 viewBox, stroke-based). No emoji anywhere in
// this app -- every icon here is a small hand-written SVG so the UI never
// depends on a font/icon-library CDN either.

const ICONS = {
  chat: '<path d="M4 5h16v11H8l-4 4V5Z"/>',
  bots: '<rect x="5" y="8" width="14" height="11" rx="2"/><path d="M9 8V5a3 3 0 0 1 6 0v3"/><circle cx="9.5" cy="13" r="1"/><circle cx="14.5" cy="13" r="1"/><path d="M9 17h6"/>',
  sessions: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h8M8 17h5"/>',
  models: '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>',
  mcp: '<rect x="3" y="4" width="8" height="8" rx="1.5"/><rect x="13" y="12" width="8" height="8" rx="1.5"/><path d="M7 12v0a4 4 0 0 0 4 4h2M17 12v0a4 4 0 0 0-4-4h-2"/>',
  config: '<circle cx="12" cy="12" r="3"/><path d="M19.4 13a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V19a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H5a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H11a1.7 1.7 0 0 0 1-1.5V5a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V11a1.7 1.7 0 0 0 1.5 1H19a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  cron: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l3 2M9 2h6"/>',
  skills: '<path d="M12 2 3 7v6c0 5 4 8 9 9 5-1 9-4 9-9V7l-9-5Z"/><path d="m9 12 2 2 4-4"/>',
  plugins: '<path d="M9 3v4M15 3v4M6 7h12l-1 5H7L6 7Z"/><path d="M7 12v3a5 5 0 0 0 10 0v-3M12 20v-3"/>',
  files: '<path d="M6 3h9l5 5v13H6V3Z"/><path d="M15 3v5h5"/>',
  env: '<path d="m5 4 3 8-3 8M19 4h-9M19 20h-9M13 12h6"/>',
  webhooks: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8.2 7.2 15.8 16.8M15.8 7.2 8.2 16.8M8.5 6h7"/>',
  logs: '<path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h8M8 17h4"/>',
  system: '<rect x="4" y="5" width="16" height="11" rx="1.5"/><path d="M9 20h6M12 16v4"/>',

  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  "eye-off": '<path d="M3 3l18 18M10.6 5.2A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.4 4.4M6.6 6.6C4 8.3 2 11 2 12c0 0 3.6 7 10 7a9.8 9.8 0 0 0 3.4-.6"/><path d="M9.5 9.9A3 3 0 0 0 12 15a3 3 0 0 0 2.1-.9"/>',
  group: '<circle cx="8" cy="9" r="3"/><circle cx="17" cy="10" r="2.5"/><path d="M3 20v-1a5 5 0 0 1 5-5h0a5 5 0 0 1 5 5v1M14.5 19v-.8a4.2 4.2 0 0 1 4-4.2h0"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  close: '<path d="M5 5l14 14M19 5 5 19"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  send: '<path d="M4 12 20 4l-6 16-3-7-7-1Z"/>',
  edit: '<path d="M4 20h4l11-11-4-4L4 16v4Z"/>',
  duplicate: '<rect x="9" y="9" width="11" height="11" rx="1.5"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  play: '<path d="M6 4l14 8-14 8V4Z"/>',
  pause: '<path d="M7 4h4v16H7zM13 4h4v16h-4z"/>',
  check: '<path d="M4 12l6 6L20 6"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  back: '<path d="M15 5 8 12l7 7"/>',
};

function icon(name, size) {
  size = size || 18;
  const body = ICONS[name] || ICONS.close;
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
}
