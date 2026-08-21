# Standalone zBots container: nginx in front (static frontend + FastAPI
# backend + uploaded avatars), the backend itself loopback-only like the
# reference hermes-agent-wrapper deployment. Point HERMES_DASHBOARD_URL /
# HERMES_API_SERVER_URL at a reachable Hermes instance (loopback by default
# when running as a sidecar in the same container as Hermes).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        apache2-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir fastapi uvicorn httpx python-multipart

WORKDIR /opt/zbots
COPY backend/ /opt/zbots/backend/
COPY frontend/ /opt/zbots/frontend/
COPY nginx.conf /etc/nginx/conf.d/zbots.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV BOTS_UI_PORT=8643
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
