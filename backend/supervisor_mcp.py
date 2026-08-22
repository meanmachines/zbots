"""Standalone MCP tool server giving a Hermes bot the ability to check on
and message other bots on this same gateway -- the same primitive
`hermes peer dm` already provides at the CLI level, exposed as callable
tools so a model can use it mid-conversation instead of only a human
running it by hand.

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

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

BOTS_UI_BASE = "http://127.0.0.1:8643"

mcp = MCPServer(
    name="bot-supervisor",
    version="1.0.0",
    instructions=(
        "Tools for supervising other Hermes bot profiles on this same "
        "gateway. Workflow: list_bots() first to see who exists (free, no "
        "LLM call) -> get_bot_status(name) for a real 'what are you "
        "actually doing right now' answer from that bot -> message_bot"
        "(name, message) to give it an instruction, ask something else, or "
        "delegate a task. get_bot_status() and message_bot() are both real "
        "conversation turns for the target bot (their reply becomes part "
        "of its own history), so use them deliberately, not as a cheap "
        "poll -- list_bots()'s last_message_preview/is_active fields are "
        "the free option when a rough sense of activity is enough."
    ),
)

_STATUS_PROMPT = (
    "What are you working on right now? Give me your current status and "
    "last completed task, not a general introduction."
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
    when you want to ask something else or give an instruction instead."""
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
    generic capabilities blurb, not a real answer."""
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BOTS_UI_BASE}/bots/{name}/messages", json={"text": message})
        r.raise_for_status()
        return r.json()["reply"]


def build_app():
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:8645", "localhost:8645"],
    )
    return mcp.streamable_http_app(transport_security=security)


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8645)
