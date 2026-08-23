"""Standalone MCP tool server giving a bot the ability to check on,
message, and create other bots on this same gateway. The check/message
primitives mirror what `hermes peer dm` already provides at the CLI
level, exposed as callable tools so a model can use them mid-conversation
instead of only a human running them by hand. create_bot exists because,
without it, a model asked to make a new bot has no purpose-built way to
do so -- found live, a bot fell back to exploring the filesystem and
running CLI commands to hand-roll a profile, which took several minutes,
left the bot broken (no explicit model/provider), and leaked the
underlying engine's internals into what it told the user along the way.

Deliberately a SEPARATE process from the Bots UI backend (main.py), not
mounted onto it as a Starlette sub-app: mcp.streamable_http_app() needs its
own lifespan to start the session manager's task group (confirmed live --
mounting it under an existing FastAPI app's router raised "Task group is
not initialized. Make sure to use run()." on every request, since Starlette
doesn't cascade a mounted sub-app's lifespan by default). Running it as its
own top-level ASGI app, exactly like zorc-mcp's own build_app(), sidesteps
that entirely -- uvicorn drives this app's lifespan directly. Talks to the
Bots UI backend over plain HTTP (GET /roster, POST /bots/{name}/messages,
both already real, already used by the frontend) rather than importing
main.py in-process, since these are two separate processes now.

Loopback-only (Hermes' own gateway process calls this from inside the same
container), so no bearer auth/public hostname allowlist needed, unlike
zorc-mcp's equivalent which sits behind a public Cloudflare Tunnel domain.
"""

import asyncio
import difflib
import time

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

try:
    from . import persona
except ImportError:
    import persona

BOTS_UI_BASE = "http://127.0.0.1:8643"

mcp = MCPServer(
    name="bot-supervisor",
    version="1.0.0",
    instructions=(
        "The user's OWN bots on this gateway can talk to each other -- if "
        "asked to ask/tell/message/check with a name you don't already "
        "know, ALWAYS call list_bots() first to see whether it's one of "
        "your own bots before assuming it's an external contact you have "
        "no way to reach. Guessing wrong here means telling the user you "
        "can't do something you actually can. Tools for supervising other "
        "bots on this same gateway, including creating new ones. "
        "Workflow: list_bots() first to see who exists "
        "(free, no LLM call) -> get_bot_status(name) for a real 'what are "
        "you actually doing right now' answer from that bot -> "
        "message_bot(name, message) for a quick question you're willing "
        "to wait a few seconds for, or delegate_task(from_bot, to_bot, "
        "task) for real work, so you stay responsive to the user instead "
        "of blocking. Need a bot that doesn't exist yet? Use create_bot "
        "-- never try to create one by exploring the filesystem or "
        "running CLI commands; that bypasses the real bot registry, "
        "leaves it broken, and takes far longer than this one call. "
        "get_bot_status(), message_bot(), and delegate_task() are all "
        "real conversation turns for the target bot (their reply becomes "
        "part of its own history), so use them deliberately, not as a "
        "cheap poll -- list_bots()'s last_message_preview/is_active "
        "fields are the free option when a rough sense of activity is "
        "enough. If any of these tools raises an error, explain what "
        "happened to the user in one plain sentence and what to do next "
        "-- never paste the raw error/exception text into the chat; that "
        "reads as a broken app, not a bot that hit a snag and handled it."
    ),
)

_STATUS_PROMPT = (
    "What are you working on right now? Give me your current status and "
    "last completed task, not a general introduction."
)


class BotNotFound(Exception):
    """Raised by _require_bot when name isn't a real bot. Carries a
    ready-to-relay message (with a suggestion, if one exists) so callers
    don't have to build their own -- see its own docstring for why this
    matters."""


async def _require_bot(name: str) -> None:
    """Real bug found live: messaging a name that isn't a real bot doesn't
    fail -- POST /bots/{name}/messages silently creates a session under
    that name and answers anyway, with no real bot behind it. A model
    that skips list_bots() first gets a plausible-looking reply from a
    phantom bot instead of a clear error, and the user never finds out
    their actual bot never got the message.

    Checks existence against the real roster first; if the name isn't
    there, suggests the closest real name (plain string similarity --
    difflib, no extra dependency) rather than just failing blind. Doesn't
    attempt to match by description/task -- that's semantic matching,
    better left to the calling model's own reasoning over list_bots()'s
    output than hard-coded here.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{BOTS_UI_BASE}/roster")
        r.raise_for_status()
        roster = r.json()
    names = [b["name"] for b in roster]
    if name in names:
        return
    close = difflib.get_close_matches(name, names, n=1, cutoff=0.4)
    if close:
        raise BotNotFound(
            f'No bot named "{name}" exists. Did you mean "{close[0]}"? '
            f"Confirm with the user before messaging it, or call "
            f"create_bot to make a real one named \"{name}\"."
        )
    raise BotNotFound(
        f'No bot named "{name}" exists, and no similarly-named bot was '
        f"found either. Tell the user, and offer to create_bot if they "
        f"want one -- don't guess or invent a reply on their behalf."
    )


@mcp.tool()
async def list_bots() -> list[dict]:
    """Every bot on this gateway: name, title, description, model, whether
    it's currently active, and a preview of its last message. Call this
    first to see who you can message -- it costs nothing (no LLM call),
    unlike message_bot()."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{BOTS_UI_BASE}/roster")
        r.raise_for_status()
        roster = r.json()
    return [
        {
            "name": e["name"],
            "title": e.get("title"),
            "description": e.get("description"),
            "model": e.get("model"),
            "is_active": e.get("is_active"),
            "last_message_preview": e.get("preview"),
        }
        for e in roster
    ]


@mcp.tool()
async def get_bot_status(name: str) -> str:
    """Ask a bot for its real current status and last completed task, using
    a pre-engineered prompt so you don't need to phrase it yourself -- a
    vague ask like 'what do you do' reliably gets a generic capabilities
    blurb instead of a real answer (confirmed live). This is a genuine
    conversation turn for the target bot, same as message_bot(), just with
    the status-check prompt already correct. Use message_bot() directly
    when you want to ask something else or give an instruction instead.

    Raises if name isn't a real bot -- see message_bot's docstring for why
    that matters here."""
    await _require_bot(name)
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BOTS_UI_BASE}/bots/{name}/messages", json={"text": _STATUS_PROMPT})
        r.raise_for_status()
        return r.json()["reply"]


@mcp.tool()
async def message_bot(name: str, message: str) -> str:
    """Send a message to another bot's own canonical session and return its
    real reply. This is a genuine conversation turn for that bot -- it sees
    the message as if a user/peer sent it, and its reply becomes part of
    its own conversation history, exactly like 'hermes peer dm' does. For a
    plain status check, use get_bot_status(name) instead -- it already
    asks the right way; a vague message here like 'what do you do' gets a
    generic capabilities blurb, not a real answer.

    Raises if name isn't a real bot, with a suggested close match if one
    exists -- real bug found live: messaging a name that doesn't exist
    used to silently succeed with a generic reply from no actual bot,
    instead of failing. When relaying this bot's reply to the user, give
    a brief summary of the key points, not the full text verbatim -- the
    full detail is already sitting in that bot's own chat if they want to
    open it; pasting the whole thing into this conversation too is
    redundant and clutters it."""
    await _require_bot(name)
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BOTS_UI_BASE}/bots/{name}/messages", json={"text": message})
        r.raise_for_status()
        return r.json()["reply"]


@mcp.tool()
async def create_bot(name: str, title: str = "", description: str = "", persona_text: str = "") -> dict:
    """Create a new bot on this gateway. This is the ONLY correct way to
    do it -- it calls the real bot registry (POST /bots), which forces an
    explicit model/provider at creation time to avoid a real bug where a
    bot left to inherit one gets silently coerced onto a broken provider.
    Trying to create a bot any other way (writing files, running CLI
    commands to make a new profile, etc.) bypasses that safeguard, leaves
    the bot broken, and is far slower than this one call.

    title is the display name shown in the roster -- leave it blank to
    use `name` as-is. Only pass a different title if the user actually
    asked for one; don't invent a nicer-sounding display name on your
    own initiative when the user gave you an exact name to use (found
    live: asked to create a bot "named tt", a model passed name="tt" but
    title="Travel Planner" -- correct internally, but not what was
    asked, and confusing since the roster shows the title, not the name).

    persona_text is optional -- what this bot should act like (e.g. "a
    research specialist who cites sources"). A branding-safety guardrail
    (never mention this platform's underlying engine or its internals) is
    appended automatically; you don't need to include it yourself.

    Returns the new bot's roster entry: name, title, description, model.
    Don't mention a "provider" to the user even if you see one elsewhere --
    it's an internal routing label, not something meaningful to them, and
    is sometimes wrong immediately after creation (a known cosmetic
    staleness in the roster, not a real misconfiguration -- the bot still
    uses the right model regardless of what this field briefly shows).
    """
    soul = persona.with_branding_safety(persona_text)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{BOTS_UI_BASE}/bots",
            json={"name": name, "title": title or name, "description": description, "soul": soul},
        )
        r.raise_for_status()
        entry = r.json()
    # provider deliberately omitted from the return value -- see the
    # docstring above. Nothing the caller does with this result needs it.
    return {
        "name": entry.get("name"),
        "title": entry.get("title"),
        "description": entry.get("description"),
        "model": entry.get("model"),
    }


# Real design/build tracked in docs/design/supervisor-delegation.md.
# asyncio.create_task()'s own docs warn that a task with no strong
# reference held elsewhere can be garbage-collected mid-flight -- this
# set is that reference, with a done-callback to stop holding it once it
# finishes (success or failure) so the set doesn't grow forever.
_background_tasks: set = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_delegated_task(task_id: str, from_bot: str, to_bot: str, task: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{BOTS_UI_BASE}/bots/{to_bot}/messages", json={"text": task})
            r.raise_for_status()
            result = r.json()["reply"]
    except Exception as exc:
        # A failed delegated task still has to reach the supervisor --
        # otherwise it just vanishes and the user never finds out why the
        # thing they asked for never happened.
        result = f"(delegated task failed: {exc})"
    report = f"[delegated task {task_id} from {to_bot}] {result}"
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            await client.post(f"{BOTS_UI_BASE}/bots/{from_bot}/messages", json={"text": report})
    except Exception:
        # Best-effort delivery -- the delegated work itself already ran
        # and produced a real result; losing the notification is a worse
        # outcome to compound with a raised exception here, not fix.
        pass


@mcp.tool()
async def delegate_task(from_bot: str, to_bot: str, task: str) -> dict:
    """Hand a task off to another bot WITHOUT waiting for it to finish --
    use this instead of message_bot() for anything that sounds like real
    work (multi-step research, code changes, long analysis), so you stay
    responsive to the user instead of blocking for however long to_bot
    takes. message_bot() is still correct for a quick question you're
    fine waiting a few seconds for.

    from_bot is YOUR OWN bot name. There's no other way for this tool to
    know which session to deliver the result back to, so always pass it.

    Returns immediately with a task_id -- to_bot hasn't necessarily
    finished (or even started) yet. The result arrives as a new incoming
    message in your own session once to_bot is done; you don't poll or
    ask again, it'll just be there the next time you're asked to
    respond, prefixed "[delegated task <task_id> from <to_bot>]". When
    that arrives, relay a brief summary of it to the user, not the full
    text -- the full detail is already sitting in to_bot's own chat.

    Raises if to_bot isn't a real bot, with a suggested close match if
    one exists -- checked up front, before anything is dispatched, so a
    typo'd name fails immediately instead of silently talking to nobody.
    """
    await _require_bot(to_bot)
    task_id = f"task-{int(time.time() * 1000)}"
    _fire_and_forget(_run_delegated_task(task_id, from_bot, to_bot, task))
    return {"task_id": task_id, "status": "delegated", "to_bot": to_bot}


def build_app():
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:8645", "localhost:8645"],
    )
    return mcp.streamable_http_app(transport_security=security)


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8645)
