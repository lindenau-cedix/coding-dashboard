#!/usr/bin/env bash
# =============================================================================
# Baut die Android-APK aus dem Frontend via Capacitor.
#
# Voraussetzungen auf der Build-Maschine (NICHT auf dem Server nötig):
#   - Node.js + npm
#   - JDK 17+   (java -version)
#   - Android SDK; ANDROID_SDK_ROOT bzw. ANDROID_HOME gesetzt,
#     cmdline-tools + platform-tools + ein Build-Tools/Platform-Paket installiert.
#
# Nutzung:
#   ./deploy/build-android.sh https://dashboard.example.com
#   (URL = öffentlich erreichbares Backend; landet als VITE_API_BASE im Build.)
# =============================================================================
set -euo pipefail
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }

API_BASE=${1:-}
if [[ -z $API_BASE ]]; then
  err "Backend-URL fehlt.  Beispiel: $0 https://dashboard.example.com"
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FRONTEND_DIR=$(cd "$SCRIPT_DIR/../frontend" && pwd)
cd "$FRONTEND_DIR"

command -v npm >/dev/null 2>&1 || { err "npm fehlt."; exit 1; }
command -v java >/dev/null 2>&1 || { err "JDK (java) fehlt."; exit 1; }
if [[ -z ${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}} ]]; then
  err "ANDROID_SDK_ROOT/ANDROID_HOME nicht gesetzt – Android SDK erforderlich."
  exit 1
fi

info "Web-Assets bauen (VITE_API_BASE=$API_BASE)"
npm install
VITE_API_BASE="$API_BASE" npm run build

if [[ ! -d android ]]; then
  info "Capacitor-Android-Projekt anlegen"
  npx cap add android
else
  info "Capacitor synchronisieren"
  npx cap sync android
fi

info "APK bauen (assembleDebug)"
cd android
chmod +x ./gradlew 2>/dev/null || true
./gradlew assembleDebug

APK="$FRONTEND_DIR/android/app/build/outputs/apk/debug/app-debug.apk"
if [[ -f $APK ]]; then
  info "Fertig: $APK"
  echo "Auf ein Android-Gerät übertragen und installieren (Sideload), z.B.:"
  echo "  adb install -r '$APK'"
else
  err "APK nicht gefunden – prüfe die Gradle-Ausgabe oben."
  exit 1
fi
