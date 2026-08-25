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
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# Debian's own nodejs/npm packages are Node 20, one major behind what
# @qwen-code/qwen-code declares as its minimum engine (>=22) -- it still
# runs under 20 but prints an EBADENGINE warning on every invocation, noisy
# for something a bot shells out to constantly. NodeSource's own Node 22
# repo instead.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# OpenCode and Qwen Code CLIs -- real, already-supported delegate-to-
# external-coding-agent mechanisms, invoked via the terminal tool exactly
# like any other shell command (OpenCode has its own skill,
# skills/autonomous-ai-agents/opencode/SKILL.md; Qwen Code is the same
# pattern, no bundled skill yet). Installed at image-build time so a
# coding-delegate bot has both on first boot instead of only after a live
# install that a later redeploy would silently lose.
RUN npm install -g opencode-ai@latest @qwen-code/qwen-code@latest

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
# aiohttp isn't in hermes-agent's own base dependency set -- it's pulled in
# only by optional extras (messaging, slack, matrix, ...) that pin it to
# 3.14.3, none of which this container installs. The api_server platform is
# itself built on aiohttp.web though (see gateway/platforms/api_server.py),
# and engine.py's embedding technique calls its handlers directly via
# aiohttp.test_utils.make_mocked_request -- so it's a real, unconditional
# runtime need here regardless of which extras group happens to list it.
# Pinned to match what hermes-agent's own extras already vet.
# mcp is the same story as aiohttp above: an optional extra
# (hermes-agent's own "mcp" extra pins mcp==2.0.0), not a base dependency,
# but supervisor_mcp.py imports it directly to run the bot-supervisor
# tool server. Installed alone rather than the whole extra group -- its
# own transitive deps (httpx2, sse-starlette, ...) come along
# automatically and don't collide with what's already installed.
RUN pip install --no-cache-dir -e /opt/zbots/vendor/hermes-agent \
    && pip install --no-cache-dir fastapi uvicorn httpx python-multipart aiohttp==3.14.3 mcp==2.0.0 pywebpush

COPY backend/ /opt/zbots/backend/
COPY frontend/ /opt/zbots/frontend/
COPY nginx.conf /etc/nginx/conf.d/zbots.conf
# The nginx package ships its own default site (the stock "Welcome to
# nginx!" page) still enabled alongside ours. zbots.conf has no catch-all
# location, so any path it doesn't recognize falls through to that stock
# page instead of a real 404 -- confirmed live, found via a plain request
# to the deployed instance's root. Remove it so unmatched paths behave
# like an actual app instead of leaking that this runs nginx.
RUN rm -f /etc/nginx/sites-enabled/default
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV BOTS_UI_PORT=8643
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
