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

A note on why every docstring below is short: unlike hermes-agent's own
built-in tools, which go through a capped, deferred tool_search catalog
(config.yaml's tools.tool_search.listing_max_tokens -- see
config_defaults.py), an external MCP server's tool descriptions get sent
to the model in full on every turn, uncapped. Confirmed live: a trivial
"what is 15 plus 27" cost 31,337 input tokens with these tools attached,
identical whether or not the bot's own bundled skills were loaded (i.e.
skills weren't the cost -- these tool descriptions almost certainly are).
The reasoning and incident history that used to live in these docstrings
now lives in comments instead, which cost nothing at runtime.
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
        "Bots on this gateway can talk to each other. If asked to ask/"
        "tell/message a name you don't recognize, call list_bots() "
        "first before assuming it's an external contact you can't "
        "reach. On a tool error, explain it to the user in one plain "
        "sentence -- never paste the raw error text."
    ),
)

_STATUS_PROMPT = (
    "What are you working on right now? Give me your current status and "
    "last completed task, not a general introduction."
)


class BotNotFound(Exception):
    """Raised by _require_bot when name isn't a real bot."""


# Real bug found live: messaging a name that isn't a real bot didn't fail --
# POST /bots/{name}/messages silently created a session under that name and
# answered anyway, no actual bot behind it. A model that skipped list_bots()
# first got a plausible-looking reply from a phantom bot instead of a clear
# error, and the user never found out their actual bot never got the
# message. This checks the real roster first and suggests the closest name
# (difflib, no extra dependency) rather than failing blind. Doesn't attempt
# to match by description/task -- that's semantic matching, better left to
# the calling model's own reasoning over list_bots()'s output.
async def _require_bot(name: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{BOTS_UI_BASE}/roster")
        r.raise_for_status()
        roster = r.json()
    names = [b["name"] for b in roster]
    if name in names:
        return
    close = difflib.get_close_matches(name, names, n=1, cutoff=0.4)
    if close:
        raise BotNotFound(f'No bot named "{name}" exists. Did you mean "{close[0]}"?')
    raise BotNotFound(f'No bot named "{name}" exists.')


@mcp.tool()
async def list_bots() -> list[dict]:
    """Every bot on this gateway: name, title, description, model,
    is_active, last_message_preview. Free -- no LLM call."""
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
    """Ask a bot for its real current status. Costs a real turn for
    that bot. Raises if name isn't a real bot."""
    # Pre-engineered prompt: a vague "what do you do" reliably gets a
    # generic capabilities blurb instead of a real answer (confirmed live).
    await _require_bot(name)
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BOTS_UI_BASE}/bots/{name}/messages", json={"text": _STATUS_PROMPT})
        r.raise_for_status()
        return r.json()["reply"]


@mcp.tool()
async def message_bot(name: str, message: str) -> str:
    """Send a message to another bot and return its real reply -- a
    genuine turn for that bot, blocking until it answers. Raises if
    name isn't a real bot, with a close-match suggestion if one exists.
    When relaying the reply, summarize the key points -- the full text
    is already in that bot's own chat if the user wants to open it."""
    await _require_bot(name)
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BOTS_UI_BASE}/bots/{name}/messages", json={"text": message})
        r.raise_for_status()
        return r.json()["reply"]


@mcp.tool()
async def create_bot(name: str, title: str = "", description: str = "", persona_text: str = "") -> dict:
    """Create a new bot -- the only correct way to do it (never
    improvise one via filesystem/CLI access). title defaults to name;
    only pass a different one if the user actually asked for one.
    persona_text is optional (what this bot should act like); a
    branding-safety guardrail is appended automatically. Returns name,
    title, description, model."""
    # Calls the real registry (POST /bots), which forces an explicit
    # model/provider at creation so a bot never inherits a broken one.
    # Real bug found live: a bot asked to create one named "tt" invented
    # the display title "Travel Planner" on its own -- title now
    # defaults to name specifically to stop that kind of over-eager
    # renaming.
    soul = persona.with_branding_safety(persona_text)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{BOTS_UI_BASE}/bots",
            json={"name": name, "title": title or name, "description": description, "soul": soul},
        )
        r.raise_for_status()
        entry = r.json()
    # "provider" deliberately omitted -- a known cosmetic staleness right
    # after creation (real routing is correct regardless), not worth
    # spending tokens on or surfacing to the user as meaningful.
    return {
        "name": entry.get("name"),
        "title": entry.get("title"),
        "description": entry.get("description"),
        "model": entry.get("model"),
    }


# asyncio.create_task()'s own docs warn that a task with no strong
# reference held elsewhere can be garbage-collected mid-flight -- this set
# is that reference, with a done-callback to stop holding it once it
# finishes (success or failure) so the set doesn't grow forever. Full
# design in docs/design/supervisor-delegation.md.
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
    """Hand a task to another bot WITHOUT waiting -- use for real work
    (research, multi-step tasks) instead of message_bot(), so you stay
    responsive. from_bot is YOUR OWN name (required -- there's no other
    way to know where to deliver the result). Returns a task_id
    immediately; the result arrives later as a new message in your own
    session, prefixed "[delegated task <id> from <to_bot>]" -- summarize
    it when it arrives, don't paste it verbatim. Raises up front if
    to_bot isn't a real bot."""
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
