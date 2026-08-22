# Standalone zBots container: nginx in front (static frontend + FastAPI
# backend + uploaded avatars). Chat/sessions run in-process against the
# vendored engine snapshot (vendor/hermes-agent, see engine.py) -- this
# needs the vendored engine's own dependencies installed too, not just
# zBots' own small set. That dependency tree is large (the vendored engine
# is a full-featured agent framework -- voice, browser automation, image
# processing, none of which the embedded chat/session path here actually
# uses) -- installing it in full for correctness now rather than risk
# missing a real runtime import; trimming it to just what's reachable from
# gateway/run.py's construction path is real follow-up work, not done here.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        apache2-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/zbots
COPY vendor/ /opt/zbots/vendor/
# hermes-agent's own build deliberately refuses a regular (non-editable)
# pip install -- its setup.py raises RuntimeError("Building wheels or
# sdists for hermes-agent is not supported. Hermes is distributed via the
# shell installer, Docker image, or Nix.") specifically to steer people
# away from this exact install shape. Their own suggested workaround for
# anyone not using one of those three official paths is an editable
# install (pip install -e .), which is what this actually is here in
# effect -- a local, non-PyPI source tree -- so -e is the correct flag,
# not a hack around the block.
RUN pip install --no-cache-dir -e /opt/zbots/vendor/hermes-agent \
    && pip install --no-cache-dir fastapi uvicorn httpx python-multipart

COPY backend/ /opt/zbots/backend/
COPY frontend/ /opt/zbots/frontend/
COPY nginx.conf /etc/nginx/conf.d/zbots.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV BOTS_UI_PORT=8643
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
