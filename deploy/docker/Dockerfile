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
# Hermes is NOT installed into the image by default. The default deployment runs
# the HOST's Hermes via runtime bind mounts (see docker-compose.yml): a venv is
# not relocatable, so reusing the host's login/data means reusing the host's
# install, mirrored into the container. Set HERMES_INSTALL_CMD / HERMES_NPM_PKG
# only if you want a SELF-CONTAINED in-image Hermes instead of the host's:
#   docker compose build --build-arg HERMES_NPM_PKG=@your-scope/hermes-cli
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

# --- non-root user + host-Hermes mirroring ----------------------------------
# claude's --dangerously-skip-permissions (and good hygiene) refuse to run as
# root, so the service runs as an unprivileged user. Its $HOME is /home/app, the
# mount point of the cd-home volume, so agent logins persist across restarts.
#
# UID/GID should match the owner of the host ~/.hermes that is bind-mounted at
# runtime (see docker-compose.yml), so files Hermes writes into that shared dir
# stay owned by the host user. Keep it stable across rebuilds — a change orphans
# the once-chowned cd-home volume (e.g. ~/.cache: Permission denied). -o + the
# best-effort drop of the node:20 base image's 'node' user/group (UID/GID 1000)
# keep the build tolerant of an id collision when APP_UID is set to 1000.
#
# HERMES_HOST_HOME is the home dir of that host user (e.g. /home/debian) — the
# absolute path baked into the host Hermes venv launcher's shebang and into the
# venv's link to the uv-managed Python. We can't relocate a venv, so we symlink
# that path to /home/app: with the host ~/.hermes and ~/.local/share/uv mounted
# under /home/app at runtime, the venv's hardcoded /home/<host>/... paths resolve.
ARG APP_UID=1000
ARG APP_GID=1000
ARG HERMES_HOST_HOME=/home/debian
RUN set -eux; \
    userdel -r node 2>/dev/null || true; \
    groupdel node 2>/dev/null || true; \
    groupadd -o -g "$APP_GID" app; \
    useradd -o -m -u "$APP_UID" -g "$APP_GID" -s /bin/bash app; \
    mkdir -p /var/lib/coding-dashboard /etc/coding-dashboard \
             /home/app/.local/bin /home/app/.local/share; \
    chown -R app:app /var/lib/coding-dashboard /etc/coding-dashboard /app /home/app; \
    # Mirror the host home path so the host venv's absolute shebang/links resolve.
    if [ "$HERMES_HOST_HOME" != "/home/app" ] && [ ! -e "$HERMES_HOST_HOME" ]; then \
      mkdir -p "$(dirname "$HERMES_HOST_HOME")"; \
      ln -s /home/app "$HERMES_HOST_HOME"; \
    fi; \
    # `hermes` on PATH: a shim that execs the (host-mounted) venv launcher, same
    # as the host installer's ~/.local/bin/hermes. Resolves via $HOME=/home/app.
    printf '#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\nexec "$HOME/.hermes/hermes-agent/venv/bin/hermes" "$@"\n' \
      > /usr/local/bin/hermes; \
    chmod +x /usr/local/bin/hermes

# --- runtime configuration (paths are fixed; secrets come from env_file) ----
ENV HOME=/home/app \
    CD_HOST=0.0.0.0 \
    CD_PORT=8000 \
    CD_DATA_DIR=/var/lib/coding-dashboard \
    CD_AGENTS_CONFIG_PATH=/etc/coding-dashboard/config.yaml \
    CD_FRONTEND_DIST=/app/frontend/dist

# --- optional in-image Hermes install (runs AS the app user) ----------------
# OFF by default: the default deployment runs the HOST's Hermes via the runtime
# bind mounts (see docker-compose.yml + the symlink above), so installing Hermes
# here would just be shadowed by that mount. Set HERMES_INSTALL_CMD (or
# HERMES_NPM_PKG) only for a SELF-CONTAINED Hermes instead of the host's; the
# HOME-based installer must run as `app` with the final HOME so its venv's
# interpreter paths point inside this container (a venv is not relocatable) and
# its ~/.local/bin shim is on PATH. It seeds into cd-home on first boot.
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
