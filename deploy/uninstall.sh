#!/usr/bin/env bash
# Entfernt Service + nginx-Site. Daten/Config nur auf Nachfrage.
set -euo pipefail
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Please run with sudo." >&2; exit 1; }

SERVICE_NAME=coding-dashboard
APP_DIR=${APP_DIR:-/opt/coding-dashboard}
DATA_DIR=${DATA_DIR:-/var/lib/coding-dashboard}
CONFIG_DIR=${CONFIG_DIR:-/etc/coding-dashboard}

info "Stopping/disabling service"
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload

info "Removing nginx site"
rm -f "/etc/nginx/sites-enabled/$SERVICE_NAME" "/etc/nginx/sites-available/$SERVICE_NAME"
if command -v nginx >/dev/null 2>&1; then nginx -t 2>/dev/null && systemctl reload nginx 2>/dev/null || true; fi

read -r -p "Delete code directory $APP_DIR? (yes/no) [no]: " a || true
[[ ${a,,} =~ ^(y|yes)$ ]] && rm -rf "$APP_DIR" && echo "  removed: $APP_DIR"
read -r -p "Delete DATA (repos+DB) in $DATA_DIR? (yes/no) [no]: " b || true
[[ ${b,,} =~ ^(y|yes)$ ]] && rm -rf "$DATA_DIR" && echo "  removed: $DATA_DIR"
read -r -p "Delete config+secrets in $CONFIG_DIR? (yes/no) [no]: " c || true
[[ ${c,,} =~ ^(y|yes)$ ]] && rm -rf "$CONFIG_DIR" && echo "  removed: $CONFIG_DIR"

info "Done."
