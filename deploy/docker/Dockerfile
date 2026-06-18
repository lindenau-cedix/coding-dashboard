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
# Each install step is skipped when its arg is empty, and every command is
# overridable so you can pin versions or swap install methods without editing
# this file:
#   docker compose build \
#     --build-arg HERMES_NPM_PKG=@your-scope/hermes-cli      # enable Hermes via npm
#   docker compose build \
#     --build-arg HERMES_INSTALL_CMD='curl -fsSL https://example.com/hermes/install.sh | sh'
# claude + codex ship enabled by default; hermes is OFF until you set one of
# the HERMES_* args (matches the "make it optional" deployment choice).
ARG CLAUDE_NPM_PKG="@anthropic-ai/claude-code"
ARG CODEX_NPM_PKG="@openai/codex"
ARG HERMES_NPM_PKG=""
ARG HERMES_INSTALL_CMD=""

# --- OS packages: Python runtime + git + tini (PID 1 / zombie reaping) ------
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      git ca-certificates curl tini \
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
# root, so the service runs as an unprivileged user. Its $HOME is a real dir we
# mount a volume onto so agent logins persist across container restarts.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd -g "$APP_GID" app \
 && useradd -m -u "$APP_UID" -g "$APP_GID" -s /bin/bash app \
 && mkdir -p /var/lib/coding-dashboard /etc/coding-dashboard \
 && chown -R app:app /var/lib/coding-dashboard /etc/coding-dashboard /app /home/app

# --- runtime configuration (paths are fixed; secrets come from env_file) ----
ENV HOME=/home/app \
    CD_HOST=0.0.0.0 \
    CD_PORT=8000 \
    CD_DATA_DIR=/var/lib/coding-dashboard \
    CD_AGENTS_CONFIG_PATH=/etc/coding-dashboard/config.yaml \
    CD_FRONTEND_DIST=/app/frontend/dist

# --- optional HOME-based Hermes installer (runs AS the app user) ------------
# Unlike the npm globals above, Hermes' own installer drops a venv + launcher
# shim under the user's HOME (~/.hermes + ~/.local/bin). It must run as the
# unprivileged `app` user with the final HOME so the files land where the home
# volume mounts, are owned by app, and are reachable via the ~/.local/bin entry
# already on PATH. (npm-packaged Hermes goes through HERMES_NPM_PKG above.)
RUN set -eux; \
    if [ -n "$HERMES_INSTALL_CMD" ]; then \
      runuser -u app -- env HOME=/home/app sh -c "$HERMES_INSTALL_CMD"; \
    fi

WORKDIR /app/backend
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${CD_PORT}/api/health" >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/cd-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
