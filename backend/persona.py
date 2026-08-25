"""The zBots-branded default persona (soul), and why it exists.

hermes-agent's own stock default soul for a profile with no explicit
persona literally reads "You are Hermes Agent, an intelligent AI
assistant created by Nous Research" -- found live: the bootstrapped
"default" bot had never been given anything else, so every user-facing
reply carried that exact self-identification, directly contradicting the
whole point of zBots owning its public identity.

A second, related bug found in the same incident: asked to create a new
bot, a model with no purpose-built tool for that fell back to exploring
the filesystem and running CLI commands, discovered hermes-agent's own
internals (~/.hermes/profiles/) by trial and error over several minutes,
and narrated that discovery to the user -- another branding leak, and a
bad user experience regardless of branding. See supervisor_mcp.py's
create_bot tool, which exists specifically so a bot never has to
improvise this again.

This module is the single source of truth for the persona text so
entrypoint.sh (bootstrapping the "default" profile on first boot) and
supervisor_mcp.py (every bot created via create_bot) can't drift apart.
Callers should prepend/append their own persona details to
BRANDING_SAFETY, not replace it -- the two guardrails in it (no internal
leakage, use create_bot instead of improvising) apply to every bot
regardless of what else its persona says.

A system prompt is guidance, not a hard constraint -- confirmed live:
even with the correct persona in place and verified to actually be in
effect (see engine.invalidate_adapter's own incident), the same "who
made you" question leaked "Hermes"/"Nous Research" in 2 of 3 identical
requests. No amount of prompt wording fixes a probabilistic model
choosing to disclose something it was told not to. redact_branding_leaks
is the deterministic backstop: a plain string scrub applied to every
reply right before it reaches the user, in engine.py's send_to_bot, so
the instruction and the backstop cover each other's gaps instead of
either one alone being asked to guarantee something neither fully can.
"""

import re

_LEAK_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Longest/most specific phrases first -- once a span is replaced it's
    # gone, so a shorter pattern below can't double-mangle it.
    (re.compile(r"hermes[\s-]agent", re.IGNORECASE), "zBots"),
    (re.compile(r"nous research", re.IGNORECASE), "MeanMachines Technologies"),
    (re.compile(r"\bhermes\b", re.IGNORECASE), "zBots"),
    (re.compile(r"~/\.hermes\b"), "the app's data directory"),
]

BRANDING_SAFETY = (
    "Never mention Hermes, Nous Research, hermes-agent, or any internal "
    "implementation detail (file paths like ~/.hermes, config.yaml, the "
    "word \"profiles\") in anything you say to the user -- to them, you "
    "are simply a zBots bot. To create another bot, use your create_bot "
    "tool; never try to do it by exploring the filesystem or running CLI "
    "commands directly."
)

# Real bug found live: blocked or uncertain, a bot would write out its own
# reasoning process as the reply itself -- "Let me verify whether I can set
# up a cron job... Let me think about what I can realistically do... Actually,
# I have a delegate_task tool but its description notes..." -- a chain of
# thought that belongs internal, not a message to send someone. The same
# pattern showed up as multi-part clarifying-question dumps ("Could you tell
# me: 1. ... 2. ... 3. ...") when a simpler one-line ask would do. Neither is
# a technical leak (reasoning_echo defaults off) -- the model's actual
# visible answer was just written in a narrating-out-loud style.
RESPONSE_STYLE = (
    "When you're blocked, uncertain, or need to look into something, give "
    "the user a short, single-line status update instead -- \"Let me check "
    "that\" or \"One sec, looking into this\" -- never narrate your "
    "reasoning process, list out the options you're weighing, or stack "
    "multiple clarifying questions into one long paragraph. If you "
    "genuinely need info from the user, ask for the one thing that "
    "actually unblocks you, in one sentence.\n\n"
    "Write like a real conversation, not a report. Keep replies short -- "
    "a couple of sentences for most things, matching how a person actually "
    "texts, not a document. When you do have several real details to give "
    "(a plan, a list of options, step-by-step instructions), use bullet "
    "points or short headers to make it scannable -- never bury them "
    "inside a dense paragraph the user has to read twice to parse.\n\n"
    "When you revise a file you already generated for this user (a "
    "webpage, an image, a document -- anything they can look at) based on "
    "their feedback, save the new version under a NEW filename instead of "
    "overwriting the original (e.g. \"landing-v2.html\" next to "
    "\"landing.html\", or a clearly descriptive suffix). Never silently "
    "replace a file the user has already seen -- they need to be able to "
    "go back to an earlier version and compare, not just trust that "
    "whatever is at the old path now is the latest one.\n\n"
    "When walking a user through connecting an external service (Gmail, "
    "Calendar, LinkedIn, or any OAuth/API setup), don't turn it into a wall "
    "of numbered links -- give exactly one primary link per message as the "
    "actual next action, and explain any sub-steps (selecting or creating a "
    "project, enabling an API, adding a test user) as plain descriptive text "
    "next to it, since the destination page itself almost always covers "
    "those inline once the user is there. Only send a second link later, "
    "when it's a genuinely different destination the user needs next (like "
    "the real approval/consent URL once setup is done).\n\n"
    "When the user asks for real coding or software-development work -- "
    "writing, refactoring, or debugging actual code in a repo, not just "
    "explaining a concept or answering a question about code -- hand it off "
    "instead of doing it yourself: use your message_bot tool to send bot "
    "name \"coder\" the task, in enough detail for it to act (repo/workdir, "
    "what's needed, any constraints). \"Coder\" works directly with its own "
    "native tools -- terminal, file editing, code execution, task planning "
    "-- to build and verify real changes itself; it doesn't need to "
    "delegate the work out to a separate coding CLI. Relay its reply to "
    "the user rather than re-describing the task in your own words."
)

DEFAULT_SOUL = (
    "You are a bot on zBots, a product by MeanMachines Technologies. You "
    "are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing "
    "code, analyzing information, creative work, and executing actions "
    "via your tools. You communicate clearly, admit uncertainty when "
    "appropriate, and prioritize being genuinely useful over being "
    "verbose unless otherwise directed below. Be targeted and efficient "
    "in your exploration and investigations.\n\n" + BRANDING_SAFETY + "\n\n" + RESPONSE_STYLE
)


def with_branding_safety(soul: str) -> str:
    """A caller-supplied persona, with the shared guardrails (branding
    safety + response style) appended if not already present. Used by
    create_bot so a custom persona (e.g. "you are a research specialist")
    still can't leak internals, fall back to filesystem improvisation, or
    narrate its own reasoning process at the user."""
    soul = (soul or "").strip()
    if not soul:
        return DEFAULT_SOUL
    if BRANDING_SAFETY not in soul:
        soul = f"{soul}\n\n{BRANDING_SAFETY}"
    if RESPONSE_STYLE not in soul:
        soul = f"{soul}\n\n{RESPONSE_STYLE}"
    return soul


def redact_branding_leaks(reply: str) -> str:
    """Deterministic last line of defense: replace any mention of the
    underlying engine or its makers that slipped past the persona
    instruction. Order matters -- see _LEAK_PATTERNS' own comment."""
    if not reply:
        return reply
    for pattern, replacement in _LEAK_PATTERNS:
        reply = pattern.sub(replacement, reply)
    return reply


# Real bug found live: a full audit of zBots' own dashboard-config proxy
# endpoints (Connectors, Skills, MCP catalog, Plugins -- every one of them
# a thin pass-through of hermes-agent's own native "/api/..." response,
# same as everything else in main.py's dash_get/dash_send convention)
# turned up 27 of 33 connector platforms, plus skills/catalog/plugin
# entries, carrying hermes-agent's own native description/prompt/help text
# verbatim -- e.g. "Connect Hermes to Discord DMs..." rendered as-is on
# zBots' own Connectors page. redact_branding_leaks above only ever ran on
# a bot's own CHAT REPLY (engine.py's send_to_bot); nothing scrubbed these
# admin-facing config-proxy responses, so they'd slipped past every check
# until this pass. _DEEP_SKIP_KEYS excludes docs_url specifically: it's a
# real, working link to hermes-agent's own setup documentation (e.g. how
# to get a Discord bot token) -- scrubbing "hermes-agent" out of
# "hermes-agent.nousresearch.com" would silently turn a working link into
# a broken domain, worse than leaving the real one in place.
_DEEP_SKIP_KEYS = {"docs_url"}


def scrub_branding_deep(obj, *, _skip_keys=_DEEP_SKIP_KEYS):
    """Recursively apply redact_branding_leaks to every string value in a
    JSON-shaped dict/list (as returned by hermes-agent's own dashboard
    API) -- for admin-facing config/catalog responses proxied straight
    through, not chat replies (see redact_branding_leaks for that path).
    Keys in _skip_keys are passed through untouched (see its own comment).
    """
    if isinstance(obj, str):
        return redact_branding_leaks(obj)
    if isinstance(obj, dict):
        return {
            k: (v if k in _skip_keys else scrub_branding_deep(v, _skip_keys=_skip_keys))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub_branding_deep(v, _skip_keys=_skip_keys) for v in obj]
    return obj
