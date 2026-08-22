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

# FastAPI backend, loopback-only. nginx fronts it on 8080.
(cd /opt/zbots/backend && exec python -m uvicorn main:app --host 127.0.0.1 --port "${BOTS_UI_PORT:-8643}") &
BACKEND_PID=$!

# Stop the backend if nginx exits so the container signals failure instead of
# hanging around with a half-dead process tree.
trap 'kill "$BACKEND_PID" 2>/dev/null || true' EXIT

nginx -g 'daemon off;'
