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

java_major_version() {
  java -XshowSettings:properties -version 2>&1 \
    | awk -F'= ' '/java\.class\.version =/ { print int($2 - 44); exit }'
}

prompt_secret() {
  local prompt=$1
  local value
  read -r -s -p "$prompt" value
  echo >&2
  printf '%s' "$value"
}

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

JAVA_MAJOR=$(java_major_version)
if [[ -z ${JAVA_MAJOR:-} ]]; then
  err "Konnte die Java-Version nicht ermitteln."
  exit 1
fi
if (( JAVA_MAJOR < 17 )); then
  err "Java $JAVA_MAJOR ist zu alt. Fuer diesen Android-Build wird mindestens JDK 17 benoetigt."
  exit 1
fi
if (( JAVA_MAJOR > 24 )); then
  err "Java $JAVA_MAJOR wird von diesem Android-Tooling noch nicht unterstuetzt."
  err "Bitte den Build mit JDK 17 oder JDK 21 starten, z.B.:"
  err "  JAVA_HOME=/pfad/zu/jdk-21 PATH=/pfad/zu/jdk-21/bin:\$PATH $0 $API_BASE"
  exit 1
fi

CF_ACCESS_CLIENT_ID=${CF_ACCESS_CLIENT_ID:-}
CF_ACCESS_CLIENT_SECRET=${CF_ACCESS_CLIENT_SECRET:-}

if [[ -t 0 && -z $CF_ACCESS_CLIENT_ID && -z $CF_ACCESS_CLIENT_SECRET ]]; then
  info "Optional: Cloudflare Access Service Token fuer den Android-Build hinterlegen"
  read -r -p "CF-Access-Client-Id (leer = kein Cloudflare Access im APK-Build): " CF_ACCESS_CLIENT_ID
  if [[ -n $CF_ACCESS_CLIENT_ID ]]; then
    CF_ACCESS_CLIENT_SECRET=$(prompt_secret "CF-Access-Client-Secret: ")
  fi
fi

if [[ -n $CF_ACCESS_CLIENT_ID && -z $CF_ACCESS_CLIENT_SECRET ]]; then
  err "CF_ACCESS_CLIENT_SECRET fehlt."
  exit 1
fi
if [[ -z $CF_ACCESS_CLIENT_ID && -n $CF_ACCESS_CLIENT_SECRET ]]; then
  err "CF_ACCESS_CLIENT_ID fehlt."
  exit 1
fi

info "Web-Assets bauen (VITE_API_BASE=$API_BASE)"
npm install
if [[ -n $CF_ACCESS_CLIENT_ID ]]; then
  info "Cloudflare Access Service Token wird in den Android-Build eingebettet"
fi
VITE_API_BASE="$API_BASE" \
VITE_CF_ACCESS_CLIENT_ID="$CF_ACCESS_CLIENT_ID" \
VITE_CF_ACCESS_CLIENT_SECRET="$CF_ACCESS_CLIENT_SECRET" \
npm run build

if [[ ! -d android ]]; then
  info "Capacitor-Android-Projekt anlegen"
  npx cap add android
else
  info "Capacitor synchronisieren"
  npx cap sync android
fi

# App-Icon/Logo aus frontend/assets/ generieren. Muss NACH 'cap add/sync'
# laufen, weil android/ nicht eingecheckt wird und dort jedes Mal neu entsteht.
# Quelle: frontend/assets/icon-only.png, icon-foreground.png, icon-background.png.
if [[ -f assets/icon-foreground.png ]]; then
  info "App-Icons aus assets/ generieren"
  npx @capacitor/assets generate --android
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
