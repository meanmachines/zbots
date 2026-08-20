# Credits

zBots is built on top of [Hermes Agent](https://hermes-agent.nousresearch.com)
by [Nous Research](https://github.com/NousResearch) ([MIT License](https://github.com/NousResearch/hermes-agent)).
Hermes Agent provides the actual agent runtime -- the LLM tool-calling loop,
session storage, MCP client, skills/plugins system, cron scheduler, and the
`api_server` protocol zBots's backend talks to. zBots does not modify
or redistribute Hermes Agent's own source; it's a separate application that
uses Hermes' public REST API surface (both the dashboard's session-authenticated
API and the `api_server` platform's Bearer-authenticated API) the same way any
other client would.

**What Hermes Agent doesn't have yet, which is what zBots actually is:**
a multi-bot roster with group chat, a companion admin UI covering the parts
of the dashboard the closed frontend doesn't expose a build for, and a
resilience layer that keeps conversations intact across a real upstream bug
in session-turn resolution (see `backend/main.py`'s `send_to_bot` docstring
for the full writeup, and [NousResearch/hermes-agent#89119](https://github.com/NousResearch/hermes-agent/issues/89119)
for the upstream tracking issue).
