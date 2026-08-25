#!/bin/sh
set -e

mkdir -p /opt/data/bots-ui-avatars

if [ -z "$HERMES_DASHBOARD_BASIC_AUTH_USERNAME" ] || [ -z "$HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" ]; then
    echo "HERMES_DASHBOARD_BASIC_AUTH_USERNAME and HERMES_DASHBOARD_BASIC_AUTH_PASSWORD are required" >&2
    exit 1
fi

# The engine now runs in-process (backend/engine.py) instead of talking to a
# separately-running gateway for chat/sessions, so this container needs its
# own real engine profile -- config.yaml with a working model provider, not
# just a URL to somewhere else. Bootstrap one on first boot if the
# persistent volume doesn't already have one; leave an existing one alone so
# a restart doesn't stomp on state.
export HERMES_HOME="/opt/data/engine-profile"
mkdir -p "$HERMES_HOME"

if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    if [ -z "$ZBOTS_MODEL_BASE_URL" ] || [ -z "$ZBOTS_MODEL_NAME" ]; then
        echo "ZBOTS_MODEL_BASE_URL and ZBOTS_MODEL_NAME are required to bootstrap the engine profile" >&2
        exit 1
    fi
    # Shape verified against a real deployed instance: `hermes -z ... --cli`
    # resolved and chatted through this exact config fine, so the provider
    # key name itself doesn't matter (tried "default" first, "zbots" here
    # only for clarity) -- the actual bug that produced "No inference
    # provider configured" during testing was HERMES_HOME not being set on
    # an ad-hoc process restart, not this file. `name`/`model`/
    # `discover_models` mirror the shape a real working profile writes.
    #
    # title_generation disabled: engine.py's own session-family tracking
    # (get_bot_messages, the rollover logic) recognizes a bot's sessions
    # by a specific title pattern it sets at creation time. Auto-titling
    # rewrites that title from the opening message's content, which is a
    # real UX nicety on its own but works against tracking here.
    cat > "$HERMES_HOME/config.yaml" <<EOF
model:
  provider: zbots
  default: ${ZBOTS_MODEL_NAME}
providers:
  zbots:
    name: zbots
    base_url: ${ZBOTS_MODEL_BASE_URL}
    model: ${ZBOTS_MODEL_NAME}
    discover_models: true
    api_key: ${ZBOTS_MODEL_API_KEY:-none}
    models:
      ${ZBOTS_MODEL_NAME}: {}
EOF
    # OpenRouter is one of hermes-agent's own native routing modes,
    # resolved through OPENROUTER_API_KEY at call time -- NOT a
    # custom-endpoint entry, so this block deliberately carries no
    # base_url/api_key of its own (see main.py's _reserved_provider_ids
    # for why the UI itself blocks adding a provider literally named
    # "openrouter" the normal way). It only exists to give OpenRouter a
    # models catalog, so it shows up in the model switcher/Models page
    # like every other provider -- and only when the key is actually
    # set, so a fresh deploy without one doesn't offer a choice that
    # would just fail when picked.
    if [ -n "$OPENROUTER_API_KEY" ]; then
        cat >> "$HERMES_HOME/config.yaml" <<EOF
  openrouter:
    name: openrouter
    models:
      anthropic/claude-sonnet-5: {}
      anthropic/claude-opus-5: {}
      openai/gpt-5.5: {}
      google/gemini-2.5-pro: {}
      deepseek/deepseek-r1: {}
      x-ai/grok-4.5: {}
EOF
    fi
    cat >> "$HERMES_HOME/config.yaml" <<EOF
mcp_servers:
  bot-supervisor:
    url: http://127.0.0.1:8645/mcp
auxiliary:
  title_generation:
    enabled: false
# Real bug found live: without this, the api_server platform's own
# /p/<profile>/ URL-prefix routing silently no-ops -- _resolve_request_profile
# (gateway/platforms/api_server.py) ignores the prefix entirely and treats
# every request as the "default" profile whenever multiplex_profiles is
# unset (default False). Confirmed live: a non-default bot's session-lock
# call (_lock_active_session_model, POST /api/sessions/{id}/model) kept
# reporting success but never actually took effect until switching to
# /models/activate instead (which sets the global main slot, unaffected by
# profile-scoping) -- almost certainly this exact gap, not a separate bug.
gateway:
  multiplex_profiles: true
EOF
fi

# hermes-agent seeds its OWN default persona into a fresh profile's
# SOUL.md -- literally "You are Hermes Agent, an intelligent AI assistant
# created by Nous Research." Found live: the bootstrapped "default" bot
# had never been given anything else, so every reply carried that exact
# self-identification, directly contradicting zBots owning its public
# identity. hermes-agent only auto-writes its own default when SOUL.md
# is missing or still its legacy empty scaffold (_ensure_default_soul_md
# in hermes_cli/config.py) -- writing real content here first means it's
# recognized as user-customized and never touched again, on this or any
# later boot.
if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
    (cd /opt/zbots/backend && python3 -c "from persona import DEFAULT_SOUL; print(DEFAULT_SOUL)") > "$HERMES_HOME/SOUL.md"
fi

# Reuse the Hermes dashboard credential pair as the nginx basic-auth gate for
# /bots, /bots-api and /bots-avatars -- identical behavior to the reference
# hermes-agent-wrapper deployment. Regenerated every boot so a changed
# password takes effect on restart.
htpasswd -bc /etc/nginx/.htpasswd-bots "$HERMES_DASHBOARD_BASIC_AUTH_USERNAME" "$HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"

# Chat/sessions run in-process against the engine (backend/engine.py), but
# everything else main.py does -- roster, profile CRUD, MCP servers, skills,
# env vars, cron, webhooks, files, raw config -- is still a real HTTP client
# (dash_get/dash_send) against a Hermes dashboard server, same as the old
# sidecar architecture. That server has to actually run somewhere, and
# nothing else in this container provides it, so start the headless backend
# (`hermes serve` -- same gateway as `hermes dashboard`, minus the browser
# and the web UI build main.py never needs).
#
# Host is 0.0.0.0, matching the official image's own default (see
# docker/s6-rc.d/dashboard/run upstream) -- NOT a loopback bind, on purpose.
# The dashboard's real password-login/cookie auth gate (what main.py's
# _dashboard_login actually speaks) only engages when auth_required is
# True, which the server derives from the bind host: loopback binds are
# treated as trusted-local and fall back to a different, session-token-only
# auth path instead, which main.py doesn't implement -- tried 127.0.0.1
# first and every dash_get/dash_send call 401'd for exactly this reason.
# Binding 0.0.0.0 here is still container-internal only: nothing in the
# Dockerfile publishes 9119 to the host, so it's unreachable from outside
# regardless of which interface it listens on inside the container.
(exec hermes serve --host 0.0.0.0 --port "${HERMES_DASHBOARD_PORT:-9119}" --skip-build --no-open) &
DASHBOARD_PID=$!

# FastAPI backend, loopback-only. nginx fronts it on 8080.
(cd /opt/zbots/backend && exec python -m uvicorn main:app --host 127.0.0.1 --port "${BOTS_UI_PORT:-8643}") &
BACKEND_PID=$!

# bot-supervisor MCP tool server (backend/supervisor_mcp.py) -- gives a bot
# the ability to list/message/check-status-on other bots on this same
# gateway. Loopback-only, registered in the bootstrapped config.yaml above
# so the engine actually connects to it; was present in the repo but never
# started by anything, so it sat dormant until now.
(cd /opt/zbots/backend && exec python -m uvicorn supervisor_mcp:app --host 127.0.0.1 --port 8645) &
SUPERVISOR_PID=$!

# Messaging-platform gateway (Telegram/Discord/WhatsApp/Slack/...) -- the
# actual connector daemon the Connectors page's real /api/messaging/platforms
# API expects to be running; without this, a platform can be configured
# there but never actually connects (confirmed live: the dashboard's own
# "gateway_running" flag stayed false with only `hermes serve` up). `hermes
# gateway start` refuses inside Docker and points here instead ("the gateway
# runs as the container's main process... or run the gateway directly:
# hermes gateway run") -- confirmed live it starts cleanly alongside the
# three processes above with no port conflicts. Real cost, not free: this
# roughly doubles idle memory use (measured ~197MB -> ~365MB with zero
# platforms enabled), which is why the container's memory budget was raised
# to 1024MB before adding this rather than after.
(cd /opt/zbots/backend && exec python3 -m hermes_cli.main gateway run) &
GATEWAY_PID=$!

# Stop the others if nginx exits so the container signals failure instead
# of hanging around with a half-dead process tree.
trap 'kill "$BACKEND_PID" "$DASHBOARD_PID" "$SUPERVISOR_PID" "$GATEWAY_PID" 2>/dev/null || true' EXIT

nginx -g 'daemon off;'
