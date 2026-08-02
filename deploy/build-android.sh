#!/usr/bin/env bash
# =============================================================================
# Baut die Android-APK aus dem Frontend via Capacitor.
#
# Prerequisites on the build machine (NOT required on the server):
#   - Node.js + npm
#   - JDK 17+   (java -version)
#   - Android SDK; ANDROID_SDK_ROOT bzw. ANDROID_HOME gesetzt,
#     cmdline-tools + platform-tools + ein Build-Tools/Platform-Paket installiert.
#
# Nutzung:
#   ./deploy/build-android.sh https://dashboard.example.com
#   (URL = publicly reachable backend; ends up as VITE_API_BASE in the build.)
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
  err "Could not determine Java version."
  exit 1
fi
if (( JAVA_MAJOR < 17 )); then
  err "Java $JAVA_MAJOR is too old. This Android build requires at least JDK 17."
  exit 1
fi
if (( JAVA_MAJOR > 24 )); then
  err "Java $JAVA_MAJOR is not yet supported by this Android tooling."
  err "Please start the build with JDK 17 or JDK 21, e.g.:"
  err "  JAVA_HOME=/path/to/jdk-21 PATH=/path/to/jdk-21/bin:\$PATH $0 $API_BASE"
  exit 1
fi

CF_ACCESS_CLIENT_ID=${CF_ACCESS_CLIENT_ID:-}
CF_ACCESS_CLIENT_SECRET=${CF_ACCESS_CLIENT_SECRET:-}

if [[ -t 0 && -z $CF_ACCESS_CLIENT_ID && -z $CF_ACCESS_CLIENT_SECRET ]]; then
  info "Optional: provide a Cloudflare Access Service Token for the Android build"
  read -r -p "CF-Access-Client-Id (empty = no Cloudflare Access in APK build): " CF_ACCESS_CLIENT_ID
  if [[ -n $CF_ACCESS_CLIENT_ID ]]; then
    CF_ACCESS_CLIENT_SECRET=$(prompt_secret "CF-Access-Client-Secret: ")
  fi
fi

if [[ -n $CF_ACCESS_CLIENT_ID && -z $CF_ACCESS_CLIENT_SECRET ]]; then
  err "CF_ACCESS_CLIENT_SECRET is missing."
  exit 1
fi
if [[ -z $CF_ACCESS_CLIENT_ID && -n $CF_ACCESS_CLIENT_SECRET ]]; then
  err "CF_ACCESS_CLIENT_ID is missing."
  exit 1
fi

info "Building web assets (VITE_API_BASE=$API_BASE)"
npm install
if [[ -n $CF_ACCESS_CLIENT_ID ]]; then
  info "Cloudflare Access Service Token will be embedded into the Android build"
fi
VITE_API_BASE="$API_BASE" \
VITE_CF_ACCESS_CLIENT_ID="$CF_ACCESS_CLIENT_ID" \
VITE_CF_ACCESS_CLIENT_SECRET="$CF_ACCESS_CLIENT_SECRET" \
npm run build

if [[ ! -d android ]]; then
  info "Creating Capacitor Android project"
  npx cap add android
else
  info "Syncing Capacitor"
  npx cap sync android
fi

# Generate app icon/logo from frontend/assets/. Must run AFTER 'cap add/sync'
# because android/ is not checked in and is recreated each time.
# Source: frontend/assets/icon-only.png, icon-foreground.png, icon-background.png.
if [[ -f assets/icon-foreground.png ]]; then
  info "Generating app icons from assets/"
  npx @capacitor/assets generate --android
fi

info "Building APK (assembleDebug)"
cd android
chmod +x ./gradlew 2>/dev/null || true
./gradlew assembleDebug

APK="$FRONTEND_DIR/android/app/build/outputs/apk/debug/app-debug.apk"
if [[ -f $APK ]]; then
  info "Done: $APK"
  echo "Transfer to an Android device and install (sideload), e.g.:"
  echo "  adb install -r '$APK'"
else
  err "APK not found – check the Gradle output above."
  exit 1
fi
