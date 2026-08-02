#!/usr/bin/env bash
# =============================================================================
# Coding Dashboard - Installer (Ubuntu)
#
# Installs the backend (systemd service) + built frontend + nginx reverse proxy.
# Must run with sudo/root. The service runs as the user that has `claude`,
# `hermes` and `codex` authenticated (default: the sudo-calling user),
# so the agents can find their credentials in that user's $HOME.
#
# Customizable via environment variables, e.g.:
#   sudo SERVICE_USER=deploy DOMAIN=dash.example.com SETUP_NGINX=yes ./install.sh
#   sudo NONINTERACTIVE=1 ADMIN_PASSWORD=... CD_GITHUB_TOKEN=... ./install.sh
# =============================================================================
set -euo pipefail

err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARN: %s\033[0m\n' "$*" >&2; }

if [[ $EUID -ne 0 ]]; then err "Please run with sudo."; exit 1; fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

# --- Defaults (overridable via env) ------------------------------------- #
APP_DIR=${APP_DIR:-/opt/coding-dashboard}
DATA_DIR=${DATA_DIR:-/var/lib/coding-dashboard}
CONFIG_DIR=${CONFIG_DIR:-/etc/coding-dashboard}
PORT=${CD_PORT:-8000}
SERVICE_USER=${SERVICE_USER:-${SUDO_USER:-root}}
NONINTERACTIVE=${NONINTERACTIVE:-0}
FORCE=${FORCE:-0}
SETUP_NGINX=${SETUP_NGINX:-}
DOMAIN=${DOMAIN:-}
SERVICE_NAME=coding-dashboard
ENV_FILE="$CONFIG_DIR/coding-dashboard.env"
CONFIG_YAML="$CONFIG_DIR/config.yaml"

# --- helpers --------------------------------------------------------------- #
ask() { # name prompt default
  local __var=$1 __prompt=$2 __default=${3:-} __in
  if [[ $NONINTERACTIVE == 1 ]]; then printf -v "$__var" '%s' "${!__var:-$__default}"; return; fi
  read -r -p "$__prompt${__default:+ [$__default]}: " __in || true
  printf -v "$__var" '%s' "${__in:-$__default}"
}
ask_secret() { # name prompt
  local __var=$1 __prompt=$2 __in
  if [[ $NONINTERACTIVE == 1 ]]; then return; fi
  read -r -s -p "$__prompt: " __in || true; echo
  printf -v "$__var" '%s' "$__in"
}
yesno() { [[ ${1,,} =~ ^(y|yes|j|ja)$ ]]; }

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  err "Service-User '$SERVICE_USER' existiert nicht. Setze SERVICE_USER=..."; exit 1
fi
if [[ $SERVICE_USER == root ]]; then
  warn "Service runs as root. 'claude --dangerously-skip-permissions' refuses root!"
  warn "Setze SERVICE_USER auf den User, der claude/hermes/codex eingerichtet hat."
fi

info "Konfiguration"
echo "  Service-User : $SERVICE_USER"
echo "  App-Dir      : $APP_DIR"
echo "  Data-Dir     : $DATA_DIR"
echo "  Config-Dir   : $CONFIG_DIR"
echo "  Backend-Port : $PORT"

# --- nginx? ---------------------------------------------------------------- #
if [[ -z $SETUP_NGINX ]]; then
  SETUP_NGINX=no
  if [[ $NONINTERACTIVE != 1 ]]; then
    ask SETUP_NGINX "nginx-Reverse-Proxy einrichten? (yes/no)" "yes"
  fi
fi
if yesno "$SETUP_NGINX" && [[ -z $DOMAIN ]]; then
  ask DOMAIN "Domain/hostname for nginx (empty = any)" ""
fi

# --- system packages ------------------------------------------------------- #
info "System-Pakete installieren"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git rsync curl openssl
if yesno "$SETUP_NGINX"; then apt-get install -y nginx; fi

# --- copy code ------------------------------------------------------------- #
info "Code nach $APP_DIR kopieren"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude '.venv' \
  --exclude 'frontend/dist' \
  --exclude 'data' \
  --exclude '*.db' \
  "$REPO_DIR"/ "$APP_DIR"/
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

# --- build frontend -------------------------------------------------------- #
info "Frontend bauen"
if sudo -u "$SERVICE_USER" -H bash -lc 'command -v npm >/dev/null 2>&1'; then
  sudo -u "$SERVICE_USER" -H bash -lc "cd '$APP_DIR/frontend' && npm ci && VITE_API_BASE='' npm run build"
  ok "Frontend gebaut: $APP_DIR/frontend/dist"
elif [[ -d "$REPO_DIR/frontend/dist" ]]; then
  warn "npm for $SERVICE_USER not found – using prebuilt dist from the repo."
  mkdir -p "$APP_DIR/frontend/dist"
  cp -r "$REPO_DIR/frontend/dist/." "$APP_DIR/frontend/dist/"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR/frontend/dist"
else
  warn "Kein npm und kein vorgebautes frontend/dist – Web-UI wird NICHT ausgeliefert."
  warn "Baue es auf einer Maschine mit Node ('cd frontend && npm ci && npm run build') und kopiere dist nach $APP_DIR/frontend/dist."
fi

# --- python venv ----------------------------------------------------------- #
info "Python-venv + Dependencies"
sudo -u "$SERVICE_USER" -H python3 -m venv "$APP_DIR/backend/.venv"
sudo -u "$SERVICE_USER" -H "$APP_DIR/backend/.venv/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" -H "$APP_DIR/backend/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"
ok "Backend-Dependencies installiert"

# --- detect agent binaries (as the service user) --------------------------- #
info "Agent-CLIs erkennen (als $SERVICE_USER)"
CLAUDE_BIN=$(sudo -u "$SERVICE_USER" -H bash -lc 'command -v claude' 2>/dev/null || true)
HERMES_BIN=$(sudo -u "$SERVICE_USER" -H bash -lc 'command -v hermes' 2>/dev/null || true)
CODEX_BIN=$(sudo -u "$SERVICE_USER" -H bash -lc 'command -v codex' 2>/dev/null || true)
[[ -n $CLAUDE_BIN ]] && echo "  claude: $CLAUDE_BIN" || { warn "claude not found in PATH of $SERVICE_USER."; CLAUDE_BIN=claude; }
[[ -n $HERMES_BIN ]] && echo "  hermes: $HERMES_BIN" || { warn "hermes not found in PATH of $SERVICE_USER."; HERMES_BIN=hermes; }
[[ -n $CODEX_BIN ]] && echo "  codex : $CODEX_BIN" || { warn "codex not found in PATH of $SERVICE_USER."; CODEX_BIN=codex; }

# --- directories ----------------------------------------------------------- #
mkdir -p "$DATA_DIR" "$CONFIG_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"; chmod 750 "$DATA_DIR"
chown root:"$SERVICE_USER" "$CONFIG_DIR"; chmod 750 "$CONFIG_DIR"

# --- config.yaml ----------------------------------------------------------- #
# Per-task "Runner: host" for Claude Code, Hermes, and Codex is OFF by
# default on systemd installs (operators hand-write the sibling
# ``*-host`` blocks if they want them). The Docker install instead does
# the auto-generation in entrypoint.sh when CD_{HERMES,CLAUDE,CODEX}_SSH_USER
# is set; we follow the hand-write path here because systemd deployments
# vary too much in their SSH layout (separate jump-host, different key
# path, etc.) to auto-derive. After adding the block, restart the
# service; the dashboard picks up the new sibling automatically.
if [[ -f $CONFIG_YAML && $FORCE != 1 ]]; then
  info "config.yaml exists – unchanged (FORCE=1 to overwrite)"
else
  info "config.yaml schreiben"
  cat > "$CONFIG_YAML" <<YAML
# Agent configuration for the Coding Dashboard (generated by the installer).
# {prompt} and {project_dir} are substituted at runtime.
context_instruction: |
  Important project context (always observe):
  1. First read the \`AGENTS.md\` file in the project root directory, if it exists,
     to understand the structure, tech stack, past decisions, and the current state.
  2. Then complete the task described above thoroughly and cleanly.
  3. Afterwards update \`AGENTS.md\` (create it if it doesn't exist): describe
     concisely and up-to-date the project structure, the tech stack, decisions
     made, the current state, and open items / next steps -- so that another AI agent
     (Claude Code, Hermes or Codex) immediately understands the project
     and can continue seamlessly.
  4. Do NOT commit or push yourself -- the dashboard handles this automatically after the task.

agents:
  claude:
    display_name: "Claude Code"
    command: ["$CLAUDE_BIN", "-p", "{prompt}", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    prompt_via: arg
    stream_format: claude-json
    enabled: true

  hermes:
    display_name: "Hermes"
    # hermes chat -q: einzelne nicht-interaktive Query, streamt Zwischenschritte live;
    # --yolo (Approvals aus), --accept-hooks (headless), AGENTS.md aus CWD.
    # -t <csv>: restricts toolsets to non-interactive ones (without clarify,
    # das in diesem Einbahn-Modus keinen Platform-Callback hat und den Run
    # and abort). Interactive TUI sessions keep the full toolset.
    # Leise Alternative ohne Live-Stream: command ["$HERMES_BIN", "-z", "{prompt}"]
    command: ["$HERMES_BIN", "chat", "-q", "{prompt}", "--yolo", "--accept-hooks", "-t", "web,browser,terminal,file_search,read_file,write_file,edit_file,multi_edit,plan,session_search,kanban,image_gen,computer_use,video_gen,tts,spotify,delegate_task,todo,cronjob"]
    prompt_via: arg
    stream_format: raw
    enabled: true
    env:
      HERMES_ACCEPT_HOOKS: "1"
      NO_COLOR: "1"
    unset_env: ["PYTHONPATH", "PYTHONHOME"]

  codex:
    display_name: "Codex"
    # codex exec: non-interactive run. "-" reads the prompt from stdin.
    # workspace-write + ask-for-approval never makes the run headless, without
    # completely disabling the sandbox.
    command: ["$CODEX_BIN", "exec", "--cd", "{project_dir}", "--sandbox", "workspace-write", "--ask-for-approval", "never", "--color", "never", "--ephemeral", "-"]
    prompt_via: stdin
    stream_format: raw
    enabled: true
    env:
      NO_COLOR: "1"
    unset_env: ["PYTHONPATH", "PYTHONHOME"]

  # Per-task "Runner: host" for Codex on systemd installs is OFF by default —
  # operators hand-write this sibling block if they want it (the Docker
  # install auto-creates it from the CD_CODEX_SSH_USER env var). To enable,
  # uncomment the block below, set the SSH user/host/port/keyfile, and
  # restart the service. The dashboard's "Runner: host" dropdown then
  # routes Codex tasks through `ssh <user>@<host> 'codex exec ...'`.
  # Note: the host's codex CLI does NOT have access to the dashboard's
  # tempfile for `--output-last-message`, so the SSH form drops that flag
  # and uses the parser's summary instead. The dashboard also copies the
  # project into a staging dir the host can reach (default /tmp), runs the
  # codex CLI there, then merges + pushes the result back.
  # codex-host:
  #   display_name: "Codex (Host)"
  #   command: ["ssh", "-i", "/home/<host-user>/.ssh/id_codex", "-p", "22",
  #             "-o", "StrictHostKeyChecking=accept-new",
  #             "-o", "UserKnownHostsFile=/home/<host-user>/.ssh_known_hosts",
  #             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
  #             "<host-user>@<host>",
  #             'cd "{project_dir}" && export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH" && exec env NO_COLOR=1 codex exec --cd "{project_dir}" --sandbox workspace-write --color never --ephemeral -',
  #             "--model", "{model}", "-c", "model_reasoning_effort={effort}"]
  #   session_command: ["ssh", "-tt", "-i", "/home/<host-user>/.ssh/id_codex", "-p", "22",
  #             "-o", "StrictHostKeyChecking=accept-new",
  #             "-o", "UserKnownHostsFile=/home/<host-user>/.ssh_known_hosts",
  #             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
  #             "<host-user>@<host>",
  #             'cd "{project_dir}" && export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH" && exec codex']
  #   prompt_via: stdin
  #   stream_format: codex
  #   enabled: true
  #   host_staging: true
YAML
  chown root:"$SERVICE_USER" "$CONFIG_YAML"; chmod 640 "$CONFIG_YAML"
fi

# --- env file (secrets) ---------------------------------------------------- #
if [[ -f $ENV_FILE && $FORCE != 1 ]]; then
  info "Env exists – unchanged (FORCE=1 to overwrite)"
else
  info "Collecting credentials"
  ADMIN_USERNAME=${CD_ADMIN_USERNAME:-admin}
  ask ADMIN_USERNAME "Admin username" "$ADMIN_USERNAME"

  # Password is OPTIONAL: leave empty -> Auth off (e.g. behind a Cloudflare Tunnel).
  ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
  if [[ $NONINTERACTIVE != 1 ]]; then
    info "Leave admin password empty = no login (e.g. behind Cloudflare Tunnel)."
    while :; do
      ask_secret ADMIN_PASSWORD "Admin password (empty = no login)"
      [[ -z $ADMIN_PASSWORD ]] && break
      local_pw2=""; ask_secret local_pw2 "Repeat password"
      [[ $ADMIN_PASSWORD == "$local_pw2" ]] && break
      err "Passwords do not match – try again."
    done
  fi

  GITHUB_TOKEN=${CD_GITHUB_TOKEN:-}
  ask_secret GITHUB_TOKEN "GitHub Personal Access Token (repo scope)"
  GITHUB_OWNER=${CD_GITHUB_OWNER:-}
  ask GITHUB_OWNER "GitHub owner/org (empty = authenticated user)" "$GITHUB_OWNER"
  GIT_AUTHOR_NAME=${CD_GIT_AUTHOR_NAME:-Coding Dashboard}
  ask GIT_AUTHOR_NAME "Git author name (auto-commits)" "$GIT_AUTHOR_NAME"
  GIT_AUTHOR_EMAIL=${CD_GIT_AUTHOR_EMAIL:-coding-dashboard@$(hostname -f 2>/dev/null || hostname)}
  ask GIT_AUTHOR_EMAIL "Git author email" "$GIT_AUTHOR_EMAIL"

  SECRET_KEY=$(openssl rand -hex 32)
  if [[ -n ${ADMIN_PASSWORD:-} ]]; then
    info "Generating password hash"
    PASS_HASH=$(cd "$APP_DIR/backend" && ADMIN_PASSWORD="$ADMIN_PASSWORD" "$APP_DIR/backend/.venv/bin/python" -c 'import os;from app.security import hash_password;print(hash_password(os.environ["ADMIN_PASSWORD"]))')
  else
    info "No password set -> Auth disabled (no login screen)."
    PASS_HASH=""
  fi

  umask 077
  cat > "$ENV_FILE" <<ENV
# Generated by install.sh at $(date -Is)
CD_SECRET_KEY=$SECRET_KEY
CD_ADMIN_USERNAME=$ADMIN_USERNAME
CD_ADMIN_PASSWORD_HASH=$PASS_HASH

CD_GITHUB_TOKEN=$GITHUB_TOKEN
CD_GITHUB_OWNER=$GITHUB_OWNER

CD_DATA_DIR=$DATA_DIR

CD_GIT_AUTHOR_NAME=$GIT_AUTHOR_NAME
CD_GIT_AUTHOR_EMAIL=$GIT_AUTHOR_EMAIL
CD_DEFAULT_BRANCH=main

CD_AGENTS_CONFIG_PATH=$CONFIG_YAML
CD_FRONTEND_DIST=$APP_DIR/frontend/dist

# "*" reflects the concrete origin, so Cloudflare Access cookies work with
# credentials:include. Stricter:
# CD_CORS_ORIGINS=https://localhost,https://$DOMAIN
CD_CORS_ORIGINS=*
CD_HOST=127.0.0.1
CD_PORT=$PORT

# Heartbeat: auto-poll GitHub issues + auto-spawn Claude Code tasks.
# Off by default; toggleable via /heartbeat UI in the running process.
CD_HEARTBEAT_ENABLED=false
CD_HEARTBEAT_INTERVAL_SECONDS=900
CD_HEARTBEAT_MAX_CONCURRENT=2
CD_HEARTBEAT_COOLDOWN_MINUTES=30
CD_HEARTBEAT_AGENT_KEY=claude
CD_HEARTBEAT_LOOKBACK_HOURS=24
CD_HEARTBEAT_LABELS=
# Should the dashboard post a comment with commit hash + branch URL
# to the GitHub issue after a successful heartbeat fix? Default: true.
CD_HEARTBEAT_COMMENT_ON_SUCCESS=true
# Should the dashboard auto-close the issue when the fix cleanly lands on
# the default branch (merge_state=merged + pushed=true)? Default: true.
# On merge conflict the issue stays open.
CD_HEARTBEAT_CLOSE_ON_MERGE=true
ENV
  chown root:"$SERVICE_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
  ok "Env written: $ENV_FILE"
fi

# --- systemd service ------------------------------------------------------- #
info "Installing systemd service"
sed -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__GROUP__|$SERVICE_USER|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__CONFIG_DIR__|$CONFIG_DIR|g" \
    "$SCRIPT_DIR/coding-dashboard.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"

# --- nginx ----------------------------------------------------------------- #
if yesno "$SETUP_NGINX"; then
  info "nginx-Site einrichten"
  sed -e "s|__DOMAIN__|${DOMAIN:-_}|g" \
      -e "s|__DIST__|$APP_DIR/frontend/dist|g" \
      -e "s|__PORT__|$PORT|g" \
      "$SCRIPT_DIR/nginx.conf" > "/etc/nginx/sites-available/$SERVICE_NAME"
  ln -sf "/etc/nginx/sites-available/$SERVICE_NAME" "/etc/nginx/sites-enabled/$SERVICE_NAME"
  [[ -e /etc/nginx/sites-enabled/default ]] && rm -f /etc/nginx/sites-enabled/default || true
  if nginx -t; then systemctl reload nginx; ok "nginx neu geladen"; else err "nginx config invalid – please check."; fi
fi

# --- health check ---------------------------------------------------------- #
info "Health-Check"
sleep 2
if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  ok "Backend antwortet auf http://127.0.0.1:$PORT/api/health"
else
  warn "Backend antwortet (noch) nicht – Logs: journalctl -u $SERVICE_NAME -e"
fi

# --- summary --------------------------------------------------------------- #
echo
ok "Installation abgeschlossen."
echo "  Service   : systemctl status $SERVICE_NAME   |   Logs: journalctl -u $SERVICE_NAME -f"
if yesno "$SETUP_NGINX"; then
  echo "  Web       : http://${DOMAIN:-<server-ip>}/"
  echo "  TLS       : sudo apt-get install -y certbot python3-certbot-nginx && sudo certbot --nginx -d ${DOMAIN:-DEINE_DOMAIN}"
else
  echo "  Backend   : http://127.0.0.1:$PORT  (kein nginx – ggf. selbst proxen)"
fi
[[ -z $(sudo -u "$SERVICE_USER" -H bash -lc 'command -v hermes' 2>/dev/null || true) ]] && \
  warn "Hermes not found – check/adjust the 'hermes' section in $CONFIG_YAML and 'systemctl restart $SERVICE_NAME'."
[[ -z $(sudo -u "$SERVICE_USER" -H bash -lc 'command -v codex' 2>/dev/null || true) ]] && \
  warn "Codex not found – check/adjust the 'codex' section in $CONFIG_YAML and 'systemctl restart $SERVICE_NAME'."
echo "  Android   : see deploy/build-android.sh (set VITE_API_BASE to public URL)."
