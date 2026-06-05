#!/usr/bin/env bash
# Entfernt Service + nginx-Site. Daten/Config nur auf Nachfrage.
set -euo pipefail
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }

SERVICE_NAME=coding-dashboard
APP_DIR=${APP_DIR:-/opt/coding-dashboard}
DATA_DIR=${DATA_DIR:-/var/lib/coding-dashboard}
CONFIG_DIR=${CONFIG_DIR:-/etc/coding-dashboard}

info "Service stoppen/deaktivieren"
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload

info "nginx-Site entfernen"
rm -f "/etc/nginx/sites-enabled/$SERVICE_NAME" "/etc/nginx/sites-available/$SERVICE_NAME"
if command -v nginx >/dev/null 2>&1; then nginx -t 2>/dev/null && systemctl reload nginx 2>/dev/null || true; fi

read -r -p "Code-Verzeichnis $APP_DIR löschen? (yes/no) [no]: " a || true
[[ ${a,,} =~ ^(y|yes|j|ja)$ ]] && rm -rf "$APP_DIR" && echo "  entfernt: $APP_DIR"
read -r -p "DATEN (Repos+DB) in $DATA_DIR löschen? (yes/no) [no]: " b || true
[[ ${b,,} =~ ^(y|yes|j|ja)$ ]] && rm -rf "$DATA_DIR" && echo "  entfernt: $DATA_DIR"
read -r -p "Config+Secrets in $CONFIG_DIR löschen? (yes/no) [no]: " c || true
[[ ${c,,} =~ ^(y|yes|j|ja)$ ]] && rm -rf "$CONFIG_DIR" && echo "  entfernt: $CONFIG_DIR"

info "Fertig."
