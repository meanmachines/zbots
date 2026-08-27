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
      return `<pre><code${block.lang ? ` class="language-${mdEscape(block.lang)}"` : ""}>${highlightCode(block.text, block.lang)}</code></pre>`;
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

// --------------------------------------------------------------------------
// Syntax highlighting for inline code -- fenced ```lang blocks in a bot's
// prose (mdBlockHtml's "pre" case above), and activity-card code bodies
// (write_file/read_file/terminal, see createActivityCard/
// fillReadFileActivityCardBody/openWorkspaceFile in app.js). Real gap found
// live: those all rendered as flat, uncolored monospace text -- readable,
// but nothing like the syntax-highlighted preview hermes-agent's own
// desktop app shows.
//
// Deliberately a small regex tokenizer, not a real parser or a vendored
// library: this app has zero external JS dependencies today (see
// index.html's own <script> list -- everything is served from /bots/,
// nothing from a CDN), and pulling in Prism/highlight.js would be the
// first one just to color a handful of languages a chat UI actually shows.
// Good enough to make code scannable at a glance; it can misjudge deeply
// nested or unusual syntax the way any regex highlighter can.
// --------------------------------------------------------------------------

function hlEscapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const HL_LANG_ALIASES = {
  py: "python",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "typescript",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  yml: "yaml",
  "c++": "cpp",
  h: "c",
  hpp: "cpp",
  cc: "cpp",
};

// File-extension -> language, for callers that only have a path (write_file/
// read_file activity cards and the Workspace file viewer have no fenced-code
// ```lang hint the way a bot's own prose does -- just a path).
const HL_EXT_LANG = {
  py: "python",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "typescript",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  json: "json",
  go: "go",
  rs: "rust",
  java: "java",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  hpp: "cpp",
  yml: "yaml",
  yaml: "yaml",
  sql: "sql",
};

function languageForPath(path) {
  const m = String(path || "").match(/\.([A-Za-z0-9+]+)$/);
  if (!m) return null;
  return HL_EXT_LANG[m[1].toLowerCase()] || null;
}

const HL_KEYWORDS = {
  python: "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield self",
  javascript: "break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof new return super switch this throw try typeof var void while with yield let async await static get set of",
  typescript: "break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof new return super switch this throw try typeof var void while with yield let async await static get set of interface type implements private public readonly enum namespace declare as",
  bash: "if then else elif fi for while do done case esac function in return exit break continue local export readonly declare source alias echo",
  go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var",
  rust: "as break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while async await dyn",
  java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while",
  c: "auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while",
  cpp: "auto break case catch char class const continue default delete do double else enum explicit extern false float for friend goto if inline int long mutable namespace new nullptr operator private protected public register return short signed sizeof static struct switch template this throw true try typedef typename union unsigned using virtual void volatile while",
  sql: "SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE ALTER DROP JOIN LEFT RIGHT INNER OUTER ON GROUP BY ORDER HAVING LIMIT AS AND OR NOT NULL IS IN LIKE DISTINCT UNION select from where insert into values update set delete create table alter drop join left right inner outer on group by order having limit as and or not null is in like distinct union",
};

const HL_COMMENT_STYLE = {
  python: { line: "#" },
  bash: { line: "#" },
  yaml: { line: "#" },
  sql: { line: "--" },
  javascript: { line: "//", block: ["/*", "*/"] },
  typescript: { line: "//", block: ["/*", "*/"] },
  go: { line: "//", block: ["/*", "*/"] },
  rust: { line: "//", block: ["/*", "*/"] },
  java: { line: "//", block: ["/*", "*/"] },
  c: { line: "//", block: ["/*", "*/"] },
  cpp: { line: "//", block: ["/*", "*/"] },
  css: { block: ["/*", "*/"] },
  generic: { line: "//", block: ["/*", "*/"] },
};

const HL_CONST_RE = /^(?:true|false|null|undefined|None|True|False|nil|NaN)$/;

const HL_REGEX_CACHE = new Map();

function hlTokenRegex(lang) {
  if (HL_REGEX_CACHE.has(lang)) return HL_REGEX_CACHE.get(lang);
  const style = HL_COMMENT_STYLE[lang] || {};
  const parts = [];
  if (style.block) {
    parts.push(`(?<comment>${hlEscapeRegex(style.block[0])}[\\s\\S]*?${hlEscapeRegex(style.block[1])})`);
  }
  if (style.line) {
    parts.push(`(?<linecomment>${hlEscapeRegex(style.line)}.*$)`);
  }
  if (lang === "python") {
    parts.push(`(?<tstring>'''[\\s\\S]*?'''|"""[\\s\\S]*?""")`);
  }
  parts.push("(?<string>`(?:[^`\\\\]|\\\\.)*`|\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*')");
  parts.push("(?<number>\\b\\d+\\.?\\d*(?:[eE][+-]?\\d+)?\\b)");
  parts.push("(?<word>[A-Za-z_$][A-Za-z0-9_$]*)");
  const re = new RegExp(parts.join("|"), "gm");
  HL_REGEX_CACHE.set(lang, re);
  return re;
}

function highlightGeneric(rawCode, keywordSet, lang) {
  const regex = hlTokenRegex(lang);
  regex.lastIndex = 0;
  let out = "";
  let last = 0;
  let m;
  while ((m = regex.exec(rawCode))) {
    out += mdEscape(rawCode.slice(last, m.index));
    const g = m.groups;
    if (g.comment !== undefined || g.linecomment !== undefined) {
      out += `<span class="hl-com">${mdEscape(g.comment !== undefined ? g.comment : g.linecomment)}</span>`;
    } else if (g.tstring !== undefined) {
      out += `<span class="hl-str">${mdEscape(g.tstring)}</span>`;
    } else if (g.string !== undefined) {
      out += `<span class="hl-str">${mdEscape(g.string)}</span>`;
    } else if (g.number !== undefined) {
      out += `<span class="hl-num">${mdEscape(g.number)}</span>`;
    } else if (g.word !== undefined) {
      const w = g.word;
      if (keywordSet && keywordSet.has(w)) {
        out += `<span class="hl-kw">${mdEscape(w)}</span>`;
      } else if (HL_CONST_RE.test(w)) {
        out += `<span class="hl-const">${mdEscape(w)}</span>`;
      } else {
        out += mdEscape(w);
      }
    }
    last = regex.lastIndex;
  }
  out += mdEscape(rawCode.slice(last));
  return out;
}

function highlightJson(rawCode) {
  const regex = /("(?:[^"\\]|\\.)*")(\s*:)?|\b(true|false|null)\b|(-?\d+\.?\d*(?:[eE][+-]?\d+)?)/g;
  let out = "";
  let last = 0;
  let m;
  while ((m = regex.exec(rawCode))) {
    out += mdEscape(rawCode.slice(last, m.index));
    if (m[1] !== undefined) {
      const cls = m[2] ? "hl-key" : "hl-str";
      out += `<span class="${cls}">${mdEscape(m[1])}</span>${m[2] ? mdEscape(m[2]) : ""}`;
    } else if (m[3] !== undefined) {
      out += `<span class="hl-const">${mdEscape(m[3])}</span>`;
    } else if (m[4] !== undefined) {
      out += `<span class="hl-num">${mdEscape(m[4])}</span>`;
    }
    last = regex.lastIndex;
  }
  out += mdEscape(rawCode.slice(last));
  return out;
}

// Returns already-escaped, safe-to-inject HTML (same contract as
// mdBlockHtml's other cases) -- never call mdEscape again on the result.
function highlightCode(code, lang) {
  const raw = String(code == null ? "" : code);
  if (!raw) return "";
  const key = HL_LANG_ALIASES[lang] || lang || "";
  if (key === "json") return highlightJson(raw);
  const keywords = HL_KEYWORDS[key];
  if (keywords) return highlightGeneric(raw, new Set(keywords.split(" ")), key);
  if (HL_COMMENT_STYLE[key]) return highlightGeneric(raw, null, key);
  // Unknown/unspecified language -- still dim comments/strings/numbers with
  // a generic C-style guess rather than leaving it completely flat.
  return highlightGeneric(raw, null, "generic");
}
