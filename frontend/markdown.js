"use strict";

// Tiny safe Markdown renderer for chat messages. No external dependency and
// no raw innerHTML of user/bot text: every span is escaped before any tag is
// emitted. Supports the subset a chat UI actually needs -- fenced code blocks,
// inline code, bold/italic/strike, http(s) links, headings, lists, blockquotes
// and paragraph breaks.

function mdEscape(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function mdInline(text) {
  let out = mdEscape(text);

  // Protect inline code spans so formatting regexes can't touch them.
  const codeSpans = [];
  out = out.replace(/`([^`]+)`/g, (_, code) => {
    codeSpans.push(code);
    return `\u0000${codeSpans.length - 1}\u0000`;
  });

  // Links -- only http(s), opened in a new tab with no referrer.
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) => {
    return `<a href="${mdEscape(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });

  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");

  out = out.replace(/\u0000(\d+)\u0000/g, (_, idx) => `<code>${codeSpans[Number(idx)]}</code>`);
  return out;
}

function mdBlockHtml(block) {
  switch (block.type) {
    case "pre":
      return `<pre><code${block.lang ? ` class="language-${mdEscape(block.lang)}"` : ""}>${mdEscape(block.text)}</code></pre>`;
    case "h":
      return `<h${block.level}>${mdInline(block.text)}</h${block.level}>`;
    case "quote":
      return `<blockquote>${mdInline(block.text)}</blockquote>`;
    case "ul":
      return `<ul>${block.items.map((t) => `<li>${mdInline(t)}</li>`).join("")}</ul>`;
    case "ol":
      return `<ol>${block.items.map((t) => `<li>${mdInline(t)}</li>`).join("")}</ol>`;
    case "p":
    default:
      return `<p>${mdInline(block.text).replace(/\n/g, "<br>")}</p>`;
  }
}

function renderMarkdown(src) {
  if (!src) return "";
  const lines = String(src).replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let paragraph = [];
  let list = null; // {type: "ul"|"ol", items: []}

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ type: "p", text: paragraph.join("\n") });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      blocks.push(list);
      list = null;
    }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```([\w+-]*)\s*$/);
    if (fence) {
      flushParagraph();
      flushList();
      const lang = fence[1];
      const code = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        code.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      blocks.push({ type: "pre", lang, text: code.join("\n") });
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ type: "h", level: heading[1].length, text: heading[2] });
      i++;
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      flushList();
      blocks.push({ type: "quote", text: quote[1] });
      i++;
      continue;
    }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) {
      flushParagraph();
      if (!list || list.type !== "ul") {
        flushList();
        list = { type: "ul", items: [] };
      }
      list.items.push(ul[1]);
      i++;
      continue;
    }

    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) {
      flushParagraph();
      if (!list || list.type !== "ol") {
        flushList();
        list = { type: "ol", items: [] };
      }
      list.items.push(ol[1]);
      i++;
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      flushList();
      i++;
      continue;
    }

    paragraph.push(line);
    i++;
  }
  flushParagraph();
  flushList();

  return blocks.map(mdBlockHtml).join("");
}
