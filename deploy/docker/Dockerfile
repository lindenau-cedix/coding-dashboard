# =============================================================================
# Coding Dashboard — all-in-one container image
#
# A single image that bundles EVERYTHING needed to run the dashboard:
#   * the FastAPI backend (uvicorn) which ALSO serves the built React SPA and
#     the WebSocket endpoints — so no nginx is required inside the container;
#   * the coding-agent CLIs (claude + codex baked in by default, hermes
#     optional — see the *_NPM_PKG / *_INSTALL_CMD build args below);
#   * git, so the agents can clone/commit/push the project repos.
#
# State (SQLite DB + cloned repos), the generated agent config and the agent
# logins all live in volumes (see docker-compose.yml) so nothing has to be
# installed on the host and nothing leaves the container at runtime.
#
# Built via: docker compose build   (context = repo root)
# =============================================================================

# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------- #
# Stage 1 — build the React SPA (Vite -> static dist/)
# --------------------------------------------------------------------------- #
FROM node:20-bookworm-slim AS frontend

WORKDIR /app/frontend

# Install deps first (better layer caching) then build.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# Same-origin: the backend serves the SPA, so the API base is empty (the SPA
# talks to /api on whatever host it is served from). Overridable at build time.
ARG VITE_API_BASE=""
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build      # -> /app/frontend/dist


# --------------------------------------------------------------------------- #
# Stage 2 — runtime: Python backend + Node-based agent CLIs
# --------------------------------------------------------------------------- #
FROM node:20-bookworm-slim AS runtime

# --- which agent CLIs to bake into the image --------------------------------
# Claude + Codex are installed here via npm (-> /usr/local/bin, on PATH). Each
# step is skipped when its arg is empty and is overridable to pin/swap versions.
#
# Hermes IS installed into the image by default (upstream one-liner). The
# installer drops a wrapper at /home/app/.local/bin/hermes which is already
# on PATH inside the container (see the $PATH line below), so the dashboard
# can spawn the in-image `hermes` directly. Operators who prefer to run the
# HOST's Hermes over SSH (one Hermes process tree for cronjobs / paired
# channels like WhatsApp + Telegram) override the build arg to empty and
# set CD_HERMES_SSH_USER instead — see docker-compose.yml + entrypoint.sh:
#   docker compose build --build-arg HERMES_INSTALL_CMD=""
ARG CLAUDE_NPM_PKG="@anthropic-ai/claude-code"
ARG CODEX_NPM_PKG="@openai/codex"
ARG HERMES_NPM_PKG=""
# Upstream installer (https://hermes-agent.nousresearch.com/install.sh):
# bootstraps uv + a Python venv + the hermes CLI into $HOME. Must run AS
# the `app` user with HOME=/home/app (set on the RUN line below) so the
# resulting venv's interpreter paths point inside the container — a venv
# is not relocatable. The HOME-based install is intentional: npm-pkg
# installation (HERMES_NPM_PKG) is supported as an alternative override.
ARG HERMES_INSTALL_CMD="curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"

# --- OS packages: Python runtime + git + ssh client + tini (PID 1 reaping) ---
# openssh-client is what drives the host's Hermes over SSH.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      git ca-certificates curl tini openssh-client \
 && rm -rf /var/lib/apt/lists/*

# --- agent CLIs (global npm -> /usr/local/bin, already on PATH) -------------
# Only the npm-packaged agents go here (they install to /usr/local/bin as root,
# always on PATH, never shadowed by the home volume). HERMES_INSTALL_CMD is a
# HOME-based installer and is run later AS the app user — see below.
RUN set -eux; \
    if [ -n "$CLAUDE_NPM_PKG" ]; then npm install -g "$CLAUDE_NPM_PKG"; fi; \
    if [ -n "$CODEX_NPM_PKG"  ]; then npm install -g "$CODEX_NPM_PKG";  fi; \
    if [ -n "$HERMES_NPM_PKG" ]; then npm install -g "$HERMES_NPM_PKG"; fi; \
    npm cache clean --force >/dev/null 2>&1 || true

# --- Python virtualenv for the backend (sidesteps PEP 668, mirrors layout) --
ENV VENV=/opt/venv
RUN python3 -m venv "$VENV"
# /home/app/.local/bin is where a HOME-based Hermes installer drops its launcher
# shim; keep it on PATH so both an interactive shell AND the backend subprocess
# (which spawns the agent by bare name) can find `hermes`.
ENV PATH="$VENV/bin:/home/app/.local/bin:$PATH"
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /app/backend/requirements.txt

# --- application code + built SPA -------------------------------------------
COPY backend/ /app/backend/
COPY deploy/docker/entrypoint.sh /usr/local/bin/cd-entrypoint
COPY --from=frontend /app/frontend/dist /app/frontend/dist
RUN chmod +x /usr/local/bin/cd-entrypoint

# --- non-root user ----------------------------------------------------------
# claude's --dangerously-skip-permissions (and good hygiene) refuse to run as
# root, so the service runs as an unprivileged user. Its $HOME is /home/app, the
# mount point of the cd-home volume, so agent logins persist across restarts.
#
# UID/GID should match the host user that runs Hermes and owns the shared data
# bind mount (see docker-compose.yml), so the host's Hermes and the container
# agree on repo file ownership. Keep it stable across rebuilds — a change orphans
# the once-chowned cd-home volume (e.g. ~/.cache: Permission denied). -o + the
# best-effort drop of the node:20 base image's 'node' user/group (UID/GID 1000)
# keep the build tolerant of an id collision when APP_UID is set to 1000.
ARG APP_UID=1000
ARG APP_GID=1000
RUN set -eux; \
    userdel -r node 2>/dev/null || true; \
    groupdel node 2>/dev/null || true; \
    groupadd -o -g "$APP_GID" app; \
    useradd -o -m -u "$APP_UID" -g "$APP_GID" -s /bin/bash app; \
    mkdir -p /var/lib/coding-dashboard /etc/coding-dashboard \
             /tmp/coding-dashboard-hermes \
             /var/lock/coding-dashboard \
             /home/app/.ssh /home/app/.local/bin; \
    chmod 700 /home/app/.ssh; \
    chown -R app:app /var/lib/coding-dashboard /etc/coding-dashboard \
                     /tmp/coding-dashboard-hermes \
                     /var/lock/coding-dashboard \
                     /app /home/app

# --- runtime configuration (paths are fixed; secrets come from env_file) ----
ENV HOME=/home/app \
    CD_HOST=0.0.0.0 \
    CD_PORT=8000 \
    CD_DATA_DIR=/var/lib/coding-dashboard \
    CD_AGENTS_CONFIG_PATH=/etc/coding-dashboard/config.yaml \
    CD_FRONTEND_DIST=/app/frontend/dist

# --- optional in-image Hermes install (runs AS the app user) ----------------
# OFF by default: the default deployment runs the HOST's Hermes OVER SSH (see
# docker-compose.yml + entrypoint.sh), so no Hermes is installed here. Set
# HERMES_INSTALL_CMD (or HERMES_NPM_PKG) only for a SELF-CONTAINED in-image
# Hermes instead (and leave CD_HERMES_SSH_USER empty); the HOME-based installer
# must run as `app` with the final HOME so its venv's interpreter paths point
# inside this container (a venv is not relocatable) and its ~/.local/bin shim is
# on PATH.
#
# After the install we also copy the resulting tree into /usr/local/share/hermes
# (a non-HOME dir that's NEVER overlaid by the cd-home volume mount in
# docker-compose.yml). The entrypoint copies from there into $HOME on first
# boot so the install survives the cd-home shadow — see deploy/docker/entrypoint.sh.
RUN set -eux; \
    if [ -n "$HERMES_INSTALL_CMD" ]; then \
      runuser -u app -- env HOME=/home/app sh -c "$HERMES_INSTALL_CMD" && \
      mkdir -p /usr/local/share/hermes && \
      if [ -d /home/app/.hermes ]; then \
        cp -a /home/app/.hermes/. /usr/local/share/hermes/.hermes/ && \
        chmod -R a+rX /usr/local/share/hermes/.hermes; \
      fi && \
      if [ -d /home/app/.local/bin ]; then \
        mkdir -p /usr/local/share/hermes/.local/bin && \
        cp -a /home/app/.local/bin/. /usr/local/share/hermes/.local/bin/ && \
        chmod -R a+rX /usr/local/share/hermes/.local; \
      fi; \
    fi

WORKDIR /app/backend
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${CD_PORT}/api/health" >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/cd-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
