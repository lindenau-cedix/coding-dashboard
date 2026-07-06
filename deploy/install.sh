#!/usr/bin/env bash
# =============================================================================
# Coding Dashboard - Installer (Ubuntu)
#
# Installiert Backend (systemd-Service) + gebautes Frontend + nginx-Reverse-Proxy.
# Muss mit sudo/root laufen. Der Service läuft als der User, der `claude`,
# `hermes` und `codex` authentifiziert hat (Standard: der sudo-aufrufende User),
# damit die Agenten ihre Credentials in dessen $HOME finden.
#
# Anpassbar über Umgebungsvariablen, z.B.:
#   sudo SERVICE_USER=deploy DOMAIN=dash.example.com SETUP_NGINX=yes ./install.sh
#   sudo NONINTERACTIVE=1 ADMIN_PASSWORD=... CD_GITHUB_TOKEN=... ./install.sh
# =============================================================================
set -euo pipefail

err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARN: %s\033[0m\n' "$*" >&2; }

if [[ $EUID -ne 0 ]]; then err "Bitte mit sudo ausführen."; exit 1; fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

# --- Defaults (überschreibbar via env) ------------------------------------- #
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
  warn "Service läuft als root. 'claude --dangerously-skip-permissions' verweigert root!"
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
  ask DOMAIN "Domain/Hostname für nginx (leer = beliebig)" ""
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
  warn "npm für $SERVICE_USER nicht gefunden – nutze vorgebautes dist aus dem Repo."
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
[[ -n $CLAUDE_BIN ]] && echo "  claude: $CLAUDE_BIN" || { warn "claude nicht im PATH von $SERVICE_USER gefunden."; CLAUDE_BIN=claude; }
[[ -n $HERMES_BIN ]] && echo "  hermes: $HERMES_BIN" || { warn "hermes nicht im PATH von $SERVICE_USER gefunden."; HERMES_BIN=hermes; }
[[ -n $CODEX_BIN ]] && echo "  codex : $CODEX_BIN" || { warn "codex nicht im PATH von $SERVICE_USER gefunden."; CODEX_BIN=codex; }

# --- directories ----------------------------------------------------------- #
mkdir -p "$DATA_DIR" "$CONFIG_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"; chmod 750 "$DATA_DIR"
chown root:"$SERVICE_USER" "$CONFIG_DIR"; chmod 750 "$CONFIG_DIR"

# --- config.yaml ----------------------------------------------------------- #
if [[ -f $CONFIG_YAML && $FORCE != 1 ]]; then
  info "config.yaml existiert – unverändert (FORCE=1 zum Überschreiben)"
else
  info "config.yaml schreiben"
  cat > "$CONFIG_YAML" <<YAML
# Agent-Konfiguration für das Coding Dashboard (vom Installer generiert).
# {prompt} und {project_dir} werden zur Laufzeit ersetzt.
context_instruction: |
  Wichtiger Projekt-Kontext (immer beachten):
  1. Lies zuerst die Datei \`AGENTS.md\` im Projekt-Wurzelverzeichnis, falls vorhanden,
     um Struktur, Tech-Stack, bisherige Entscheidungen und den aktuellen Stand zu verstehen.
  2. Erledige anschliessend die oben beschriebene Aufgabe vollstaendig und sauber.
  3. Aktualisiere danach \`AGENTS.md\` (lege sie an, falls nicht vorhanden): beschreibe knapp
     und aktuell die Projektstruktur, den Tech-Stack, getroffene Entscheidungen, den aktuellen
     Stand sowie offene Punkte / Next Steps -- so, dass ein anderer KI-Agent (Claude Code,
     Hermes oder Codex) das Projekt sofort versteht und nahtlos weiterarbeiten kann.
  4. Committe oder pushe NICHT selbst -- das uebernimmt das Dashboard automatisch nach dem Task.

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
    # -t <csv>: schränkt die Toolsets auf nicht-interaktive ein (ohne clarify,
    # das in diesem Einbahn-Modus keinen Platform-Callback hat und den Run
    # abbrechen würde). Interaktive TUI-Sessions behalten das volle Toolset.
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
    # codex exec: nicht-interaktiver Lauf. "-" liest den Prompt von stdin.
    # workspace-write + ask-for-approval never macht den Lauf headless, ohne die
    # Sandbox komplett zu deaktivieren.
    command: ["$CODEX_BIN", "exec", "--cd", "{project_dir}", "--sandbox", "workspace-write", "--ask-for-approval", "never", "--color", "never", "--ephemeral", "-"]
    prompt_via: stdin
    stream_format: raw
    enabled: true
    env:
      NO_COLOR: "1"
    unset_env: ["PYTHONPATH", "PYTHONHOME"]
YAML
  chown root:"$SERVICE_USER" "$CONFIG_YAML"; chmod 640 "$CONFIG_YAML"
fi

# --- env file (secrets) ---------------------------------------------------- #
if [[ -f $ENV_FILE && $FORCE != 1 ]]; then
  info "Env existiert – unverändert (FORCE=1 zum Überschreiben)"
else
  info "Zugangsdaten erfassen"
  ADMIN_USERNAME=${CD_ADMIN_USERNAME:-admin}
  ask ADMIN_USERNAME "Admin-Benutzername" "$ADMIN_USERNAME"

  # Passwort ist OPTIONAL: leer lassen -> Auth aus (z.B. hinter Cloudflare Tunnel).
  ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
  if [[ $NONINTERACTIVE != 1 ]]; then
    info "Admin-Passwort leer lassen = ohne Login (z.B. hinter Cloudflare Tunnel)."
    while :; do
      ask_secret ADMIN_PASSWORD "Admin-Passwort (leer = ohne Login)"
      [[ -z $ADMIN_PASSWORD ]] && break
      local_pw2=""; ask_secret local_pw2 "Passwort wiederholen"
      [[ $ADMIN_PASSWORD == "$local_pw2" ]] && break
      err "Passwörter ungleich – nochmal."
    done
  fi

  GITHUB_TOKEN=${CD_GITHUB_TOKEN:-}
  ask_secret GITHUB_TOKEN "GitHub Personal Access Token (repo-Scope)"
  GITHUB_OWNER=${CD_GITHUB_OWNER:-}
  ask GITHUB_OWNER "GitHub Owner/Org (leer = authentifizierter User)" "$GITHUB_OWNER"
  GIT_AUTHOR_NAME=${CD_GIT_AUTHOR_NAME:-Coding Dashboard}
  ask GIT_AUTHOR_NAME "Git author name (Auto-Commits)" "$GIT_AUTHOR_NAME"
  GIT_AUTHOR_EMAIL=${CD_GIT_AUTHOR_EMAIL:-coding-dashboard@$(hostname -f 2>/dev/null || hostname)}
  ask GIT_AUTHOR_EMAIL "Git author email" "$GIT_AUTHOR_EMAIL"

  SECRET_KEY=$(openssl rand -hex 32)
  if [[ -n ${ADMIN_PASSWORD:-} ]]; then
    info "Passwort-Hash erzeugen"
    PASS_HASH=$(cd "$APP_DIR/backend" && ADMIN_PASSWORD="$ADMIN_PASSWORD" "$APP_DIR/backend/.venv/bin/python" -c 'import os;from app.security import hash_password;print(hash_password(os.environ["ADMIN_PASSWORD"]))')
  else
    info "Kein Passwort gesetzt -> Auth deaktiviert (kein Login-Screen)."
    PASS_HASH=""
  fi

  umask 077
  cat > "$ENV_FILE" <<ENV
# Generiert von install.sh am $(date -Is)
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

# "*" spiegelt die konkrete Origin, damit Cloudflare-Access-Cookies mit
# credentials:include funktionieren. Strenger:
# CD_CORS_ORIGINS=https://localhost,https://$DOMAIN
CD_CORS_ORIGINS=*
CD_HOST=127.0.0.1
CD_PORT=$PORT

# Heartbeat: auto-poll GitHub issues + auto-spawn Claude Code tasks.
# Standardmaessig AUS; via /heartbeat UI im laufenden Prozess einschaltbar.
CD_HEARTBEAT_ENABLED=false
CD_HEARTBEAT_INTERVAL_SECONDS=900
CD_HEARTBEAT_MAX_CONCURRENT=2
CD_HEARTBEAT_COOLDOWN_MINUTES=30
CD_HEARTBEAT_AGENT_KEY=claude
CD_HEARTBEAT_LOOKBACK_HOURS=24
CD_HEARTBEAT_LABELS=
# Soll das Dashboard nach einem erfolgreichen Heartbeat-Fix einen Kommentar
# mit Commit-Nr. + Branch-URL auf das GitHub-Issue posten? Default: true.
CD_HEARTBEAT_COMMENT_ON_SUCCESS=true
# Soll das Dashboard das Issue automatisch schliessen, wenn der Fix sauber
# auf dem Default-Branch gelandet ist (merge_state=merged + pushed=true)?
# Default: true. Bei Merge-Konflikt bleibt das Issue offen.
CD_HEARTBEAT_CLOSE_ON_MERGE=true
ENV
  chown root:"$SERVICE_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
  ok "Env geschrieben: $ENV_FILE"
fi

# --- systemd service ------------------------------------------------------- #
info "systemd-Service installieren"
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
  if nginx -t; then systemctl reload nginx; ok "nginx neu geladen"; else err "nginx-Konfig fehlerhaft – bitte prüfen."; fi
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
  warn "Hermes wurde nicht gefunden – prüfe/justiere die 'hermes'-Sektion in $CONFIG_YAML und 'systemctl restart $SERVICE_NAME'."
[[ -z $(sudo -u "$SERVICE_USER" -H bash -lc 'command -v codex' 2>/dev/null || true) ]] && \
  warn "Codex wurde nicht gefunden – prüfe/justiere die 'codex'-Sektion in $CONFIG_YAML und 'systemctl restart $SERVICE_NAME'."
echo "  Android   : siehe deploy/build-android.sh (VITE_API_BASE auf öffentliche URL setzen)."
