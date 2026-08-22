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
    cat > "$HERMES_HOME/config.yaml" <<EOF
model:
  provider: default
  default: ${ZBOTS_MODEL_NAME}
providers:
  default:
    type: openai
    base_url: ${ZBOTS_MODEL_BASE_URL}
    api_key: ${ZBOTS_MODEL_API_KEY:-none}
    models:
      ${ZBOTS_MODEL_NAME}: {}
EOF
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

# Stop the other two if nginx exits so the container signals failure instead
# of hanging around with a half-dead process tree.
trap 'kill "$BACKEND_PID" "$DASHBOARD_PID" 2>/dev/null || true' EXIT

nginx -g 'daemon off;'
