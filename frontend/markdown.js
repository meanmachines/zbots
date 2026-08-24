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

// Real gap found live: a bot walking someone through an OAuth setup (or
// any "here's a link" moment) writes the bare URL as plain text, not
// markdown link syntax -- this renderer only ever linkified
// [label](url), so that URL rendered as dead, unstyled text the user
// couldn't click at all, only select-and-copy-paste. Matches a bare
// http(s) URL not already wrapped in [label](url) (that conversion runs
// first, in mdInline, and protects its own output before this ever
// sees the text). Trailing punctuation likely belonging to the
// SENTENCE, not the URL (a period ending the sentence, a comma before
// "and", a closing paren that was never opened) is peeled off after
// matching, same as most chat apps' own autolinkers do.
const BARE_URL_RE = /https?:\/\/[^\s<>"')\]]+/g;
const URL_TRAILING_PUNCT_RE = /[.,;:!?]+$/;

// OAuth/consent-flow links get a distinct "Connect" button instead of
// blending in as one more inline link -- real feedback live: walking
// someone through connecting Gmail/Calendar buries the one link that
// actually matters (the real authorization URL) in a wall of setup
// prose, when it's the single most important thing to click. Matched
// by the actual OAuth AUTHORIZATION endpoint host, not by guessing at
// path keywords -- these are the real, stable authorize endpoints every
// OAuth 2.0 provider exposes, confirmed against hermes-agent's own
// google-workspace skill (the mechanism that generates these links
// today).
const OAUTH_LINK_RE = /^https:\/\/(accounts\.google\.com\/o\/oauth2|www\.linkedin\.com\/oauth|login\.microsoftonline\.com\/[^/]+\/oauth2|slack\.com\/oauth|github\.com\/login\/oauth\/authorize)\b/i;

// url arrives already HTML-escaped -- both callers (the markdown-link
// regex and linkifyBareUrls) run on `out` AFTER mdInline's own leading
// mdEscape(text) pass, so it's already safe to drop into an attribute
// as-is. Real bug found live, pre-existing before this file even grew a
// bare-URL pass: escaping it again here double-encoded any query-string
// "&" into "&amp;amp;" -- which a real OAuth authorize URL (the one
// case this file most needs to get right) almost always has.
function linkHtml(url, label) {
  if (OAUTH_LINK_RE.test(url)) {
    return `<a class="connect-btn" href="${url}" target="_blank" rel="noopener noreferrer">${icon("connectors", 14)}<span>Connect</span></a>`;
  }
  return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

// Runs over the STRING (not the DOM) after markdown-syntax links have
// already been protected as placeholders, so a bare URL match can never
// land inside an href already produced, and this can never re-process
// a URL that was already part of [label](url).
function linkifyBareUrls(text, linkSpans) {
  return text.replace(BARE_URL_RE, (m) => {
    const trailingMatch = m.match(URL_TRAILING_PUNCT_RE);
    const trailing = trailingMatch ? trailingMatch[0] : "";
    const url = trailing ? m.slice(0, -trailing.length) : m;
    if (!url) return m;
    linkSpans.push(linkHtml(url, url));
    return `\u0001${linkSpans.length - 1}\u0001${trailing}`;
  });
}

function mdInline(text) {
  let out = mdEscape(text);

  // Protect inline code spans so formatting regexes can't touch them.
  const codeSpans = [];
  out = out.replace(/`([^`]+)`/g, (_, code) => {
    codeSpans.push(code);
    return `\u0000${codeSpans.length - 1}\u0000`;
  });

  // Links -- only http(s), opened in a new tab with no referrer. Both
  // markdown-syntax links and bare URLs (see linkifyBareUrls below) are
  // protected behind the same NUL-fence placeholder scheme as code
  // spans, so the bare-URL pass can never land inside an href this pass
  // already produced.
  const linkSpans = [];
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) => {
    linkSpans.push(linkHtml(url, label));
    return `\u0001${linkSpans.length - 1}\u0001`;
  });
  out = linkifyBareUrls(out, linkSpans);

  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");

  out = out.replace(/\u0000(\d+)\u0000/g, (_, idx) => `<code>${codeSpans[Number(idx)]}</code>`);
  out = out.replace(/\u0001(\d+)\u0001/g, (_, idx) => linkSpans[Number(idx)]);
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
