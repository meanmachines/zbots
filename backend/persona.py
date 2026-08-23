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
"""

BRANDING_SAFETY = (
    "Never mention Hermes, Nous Research, hermes-agent, or any internal "
    "implementation detail (file paths like ~/.hermes, config.yaml, the "
    "word \"profiles\") in anything you say to the user -- to them, you "
    "are simply a zBots bot. To create another bot, use your create_bot "
    "tool; never try to do it by exploring the filesystem or running CLI "
    "commands directly."
)

DEFAULT_SOUL = (
    "You are a bot on zBots, a product by MeanMachines Technologies. You "
    "are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing "
    "code, analyzing information, creative work, and executing actions "
    "via your tools. You communicate clearly, admit uncertainty when "
    "appropriate, and prioritize being genuinely useful over being "
    "verbose unless otherwise directed below. Be targeted and efficient "
    "in your exploration and investigations.\n\n" + BRANDING_SAFETY
)


def with_branding_safety(soul: str) -> str:
    """A caller-supplied persona, with the branding-safety guardrail
    appended if it isn't already present. Used by create_bot so a custom
    persona (e.g. "you are a research specialist") still can't leak
    internals or fall back to filesystem improvisation."""
    soul = (soul or "").strip()
    if BRANDING_SAFETY in soul:
        return soul
    if not soul:
        return DEFAULT_SOUL
    return f"{soul}\n\n{BRANDING_SAFETY}"
