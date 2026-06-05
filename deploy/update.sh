#!/usr/bin/env bash
# Update einer bestehenden Installation: Code synchronisieren, Frontend neu
# bauen, Backend-Deps aktualisieren, Service neu starten. Env/config bleiben.
set -euo pipefail
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { err "Bitte mit sudo ausführen."; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
APP_DIR=${APP_DIR:-/opt/coding-dashboard}
SERVICE_NAME=coding-dashboard
SERVICE_USER=${SERVICE_USER:-$(systemctl show -p User --value "$SERVICE_NAME" 2>/dev/null || true)}
[[ -n $SERVICE_USER ]] || { err "SERVICE_USER nicht ermittelbar – setze SERVICE_USER=..."; exit 1; }

info "Code -> $APP_DIR (User: $SERVICE_USER)"
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude 'frontend/dist' --exclude 'data' --exclude '*.db' \
  "$REPO_DIR"/ "$APP_DIR"/
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

info "Frontend bauen"
if sudo -u "$SERVICE_USER" -H bash -lc 'command -v npm >/dev/null 2>&1'; then
  sudo -u "$SERVICE_USER" -H bash -lc "cd '$APP_DIR/frontend' && npm ci && VITE_API_BASE='' npm run build"
elif [[ -d "$REPO_DIR/frontend/dist" ]]; then
  mkdir -p "$APP_DIR/frontend/dist"; cp -r "$REPO_DIR/frontend/dist/." "$APP_DIR/frontend/dist/"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR/frontend/dist"
fi

info "Backend-Dependencies aktualisieren"
sudo -u "$SERVICE_USER" -H "$APP_DIR/backend/.venv/bin/pip" install -q --upgrade -r "$APP_DIR/backend/requirements.txt"

info "Service neu starten"
systemctl restart "$SERVICE_NAME"
sleep 2
PORT=$(systemctl show -p Environment --value "$SERVICE_NAME" | tr ' ' '\n' | sed -n 's/^CD_PORT=//p'); PORT=${PORT:-8000}
if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ok "Update ok – Backend antwortet."; else err "Backend antwortet nicht – journalctl -u $SERVICE_NAME -e"; fi
