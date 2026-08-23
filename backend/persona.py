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
    "inside a dense paragraph the user has to read twice to parse."
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
