#!/bin/sh
set -e

mkdir -p /opt/data/bots-ui-avatars

if [ -z "$HERMES_DASHBOARD_BASIC_AUTH_USERNAME" ] || [ -z "$HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" ]; then
    echo "HERMES_DASHBOARD_BASIC_AUTH_USERNAME and HERMES_DASHBOARD_BASIC_AUTH_PASSWORD are required" >&2
    exit 1
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
