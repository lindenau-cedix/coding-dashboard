#!/usr/bin/env bash
# =============================================================================
# Container entrypoint for the Coding Dashboard.
#
#   1. make sure the data + config dirs exist (they are volume mount points),
#   2. on first boot, generate config.yaml ENABLING ONLY the agent CLIs that
#      are actually installed (claude/codex/hermes) — so an agent that was not
#      baked into the image never shows up as a dead entry in the UI,
#   3. print which agents are available + how to log in,
#   4. exec the backend (uvicorn, passed as CMD).
#
# config.yaml is generated from the backend's own default_agents(), so the
# agent command lines never drift from the code. The file is written once and
# NEVER overwritten — edit it and restart the container to customise.
# =============================================================================
set -euo pipefail

APP_DIR="${CD_APP_DIR:-/app/backend}"
DATA_DIR="${CD_DATA_DIR:-/var/lib/coding-dashboard}"
CONFIG_YAML="${CD_AGENTS_CONFIG_PATH:-/etc/coding-dashboard/config.yaml}"

cd "$APP_DIR"
mkdir -p "$DATA_DIR" "$(dirname "$CONFIG_YAML")" 2>/dev/null || true

have() { command -v "$1" >/dev/null 2>&1; }

# --- self-heal a home-volume Hermes install --------------------------------
# claude/codex are global npm installs, but Hermes is typically installed into
# the home volume by its own installer: a venv under ~/.hermes plus a launcher
# shim in ~/.local/bin (which is on PATH). Some install/restore paths drop the
# executable bit on the venv entrypoint, so `hermes` dies with
#   "cannot execute: Permission denied".
# Restore +x idempotently on every boot — BEFORE the availability check below
# so first-boot config and the report both see a working hermes. Runs against
# whatever the home volume currently holds, so a plain rebuild + restart fixes
# an already-broken install without re-running the installer.
hermes_shim="$HOME/.local/bin/hermes"
hermes_venv_bin="$HOME/.hermes/hermes-agent/venv/bin"
[[ -e "$hermes_shim" ]] && chmod u+rx "$hermes_shim" 2>/dev/null || true
[[ -d "$hermes_venv_bin" ]] && chmod -R u+rx "$hermes_venv_bin" 2>/dev/null || true

# --- first-boot config generation ------------------------------------------
if [[ ! -f "$CONFIG_YAML" ]]; then
  echo "==> First boot: generating $CONFIG_YAML"
  if python - "$CONFIG_YAML.tmp" <<'PY'
import shutil, sys, yaml
from app.config import default_agents, DEFAULT_CONTEXT_INSTRUCTION

agents = {}
for key, spec in default_agents().items():
    d = spec.model_dump()
    d.pop("key", None)  # the loader re-derives the key from the mapping
    # Enable an agent only if its CLI is actually installed in this image.
    d["enabled"] = shutil.which(spec.command[0]) is not None
    agents[key] = d

doc = {"context_instruction": DEFAULT_CONTEXT_INSTRUCTION, "agents": agents}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write("# Auto-generated on first container boot (deploy/docker/entrypoint.sh).\n")
    f.write("# 'enabled' reflects which agent CLIs were on PATH at first boot.\n")
    f.write("# Edit freely and restart the container; this file is never overwritten.\n\n")
    yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
PY
  then
    mv "$CONFIG_YAML.tmp" "$CONFIG_YAML"
    echo "    wrote $CONFIG_YAML"
  else
    rm -f "$CONFIG_YAML.tmp"
    echo "WARN: could not generate config.yaml — backend will use built-in agent defaults" >&2
  fi
fi

# --- report agent availability + login hint --------------------------------
echo "==> Agent CLIs in this container:"
any_agent=0
for a in claude codex hermes; do
  if have "$a"; then
    printf '    %-7s %s\n' "$a" "$(command -v "$a")"
    any_agent=1
  else
    printf '    %-7s (not installed)\n' "$a"
  fi
done
if [[ $any_agent -eq 0 ]]; then
  echo "WARN: no agent CLI found in the image — rebuild with the *_NPM_PKG build args." >&2
fi
echo "==> Agents authenticate via interactive login (credentials persist in the home volume):"
echo "      docker compose exec dashboard claude        # then log in in the TUI"
echo "      docker compose exec dashboard codex login"
if have hermes; then
  echo "      docker compose exec dashboard hermes        # then log in / configure"
fi

exec "$@"
