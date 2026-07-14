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

# --- Hermes runs on the HOST over SSH --------------------------------------
# By default Hermes is NOT in this image: when CD_HERMES_SSH_USER is set, the
# generated config.yaml runs the host's `hermes` via `ssh <user>@<host> '… hermes …'`
# (see the Python block below). This keeps exactly one Hermes process tree on the
# host, so its cronjobs / paired channels (WhatsApp, Telegram) don't fire twice.
HERMES_SSH_USER="${CD_HERMES_SSH_USER:-}"
HERMES_SSH_HOST="${CD_HERMES_SSH_HOST:-host.docker.internal}"
HERMES_SSH_PORT="${CD_HERMES_SSH_PORT:-22}"
HERMES_SSH_KEY="/home/app/.ssh/id_hermes"
# The host's Hermes (and Claude Code, when the claude-host sibling is also
# SSH-driven) cannot see the data volume, so the dashboard runs them inside
# a COPY of the project here — a dir bind-mounted from the host at the SAME
# path so `cd {project_dir}` resolves identically on both sides. Ensure it
# exists/writable whenever EITHER SSH route is active (since the operator
# may configure only one of the two CD_*_SSH_USER vars — see
# deploy/docker/coding-dashboard.docker.env.example for the shared-wiring
# rules). Both agent CLIs share this single staging dir.
HERMES_STAGING_DIR="${CD_HERMES_STAGING_DIR:-/tmp/coding-dashboard-hermes}"
[[ -n "$HERMES_SSH_USER" || -n "$CLAUDE_SSH_USER" ]] && mkdir -p "$HERMES_STAGING_DIR" 2>/dev/null || true
# known_hosts must live somewhere the app user can write (the single-file key
# bind mount leaves ~/.ssh root-owned), so keep it in the home volume root.
HERMES_KNOWN_HOSTS="$HOME/.ssh_known_hosts"
[[ -n "$HERMES_SSH_USER" || -n "$CLAUDE_SSH_USER" ]] && { : > "$HERMES_KNOWN_HOSTS" 2>/dev/null || true; touch "$HERMES_KNOWN_HOSTS" 2>/dev/null || true; }

# --- Claude Code runs on the HOST over SSH (mirrors the Hermes-SSH block) --
# When CD_CLAUDE_SSH_USER is set, the generated config.yaml gets a SECOND
# Claude agent entry under the key "claude-host" that drives the host's
# `claude` CLI over SSH. The dashboard's per-task "Runner: host" toggle then
# selects that sibling on a per-task basis (see TaskManager._run_inner +
# SessionManager.start). Default (when this env var is empty) = container-only
# claude; the host runner toggle is hidden in the UI.
CLAUDE_SSH_USER="${CD_CLAUDE_SSH_USER:-}"
CLAUDE_SSH_HOST="${CD_CLAUDE_SSH_HOST:-host.docker.internal}"
CLAUDE_SSH_PORT="${CD_CLAUDE_SSH_PORT:-22}"
CLAUDE_SSH_KEY="/home/app/.ssh/id_claude"
# The shared staging dir was already created above when either SSH user
# was set; nothing to do here. (Both agents' host-side copies live in
# that dir with per-project / per-task namespacing — no collision.)

# --- Host-visible lock dir (one lock file per active task/goal/session) ---
# Bind-mounted from the host by docker-compose so operators can see at a glance
# "is something running?" without poking inside the container.  Create on boot
# so the app user owns the subdir even when the host bind-mount is empty.
HOST_LOCK_DIR="${CD_HOST_LOCK_DIR:-/var/lock/coding-dashboard}"
mkdir -p "$HOST_LOCK_DIR" 2>/dev/null || true

# --- self-heal a SELF-CONTAINED (in-image) Hermes install ------------------
# Only relevant when you opted into an in-image Hermes (HERMES_INSTALL_CMD): its
# own installer drops a venv under ~/.hermes plus a launcher shim in ~/.local/bin.
# Some install/restore paths drop the exec bit on the venv entrypoint, so `hermes`
# dies with "cannot execute: Permission denied". Restore +x idempotently; no-op in
# the default SSH mode where neither path exists.
hermes_shim="$HOME/.local/bin/hermes"
hermes_venv_bin="$HOME/.hermes/hermes-agent/venv/bin"
[[ -e "$hermes_shim" ]] && chmod u+rx "$hermes_shim" 2>/dev/null || true
[[ -d "$hermes_venv_bin" ]] && chmod -R u+rx "$hermes_venv_bin" 2>/dev/null || true

# --- first-boot config generation ------------------------------------------
if [[ ! -f "$CONFIG_YAML" ]]; then
  echo "==> First boot: generating $CONFIG_YAML"
  if python - "$CONFIG_YAML.tmp" <<'PY'
import sys, yaml
from app.config_bootstrap import generate_initial_agents_config
doc = generate_initial_agents_config()
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
echo "==> In-container agent CLIs:"
any_agent=0
for a in claude codex; do
  if have "$a"; then
    printf '    %-7s %s\n' "$a" "$(command -v "$a")"
    any_agent=1
  else
    printf '    %-7s (not installed)\n' "$a"
  fi
done
if have hermes; then
  printf '    %-7s %s (self-contained in-image)\n' "hermes" "$(command -v hermes)"
  any_agent=1
fi
if [[ $any_agent -eq 0 && -z "$HERMES_SSH_USER" && -z "$CLAUDE_SSH_USER" ]]; then
  echo "WARN: no agent CLI found in the image — rebuild with the *_NPM_PKG build args." >&2
fi
echo "==> Claude + Codex authenticate via interactive login (credentials persist in the cd-home volume):"
echo "      docker compose exec dashboard claude        # then log in in the TUI"
echo "      docker compose exec dashboard codex login"

# --- Shared host-over-SSH summary ------------------------------------------
# Effective values mirror the resolver in backend/app/config_bootstrap.py:
# each sibling prefers its own env vars and falls back to the other agent's.
# We resolve them here so the boot log shows operators where tasks will go.
EFFECTIVE_HERMES_SSH_USER="${HERMES_SSH_USER:-$CLAUDE_SSH_USER}"
EFFECTIVE_HERMES_SSH_HOST="${HERMES_SSH_HOST:-$CLAUDE_SSH_HOST}"
EFFECTIVE_HERMES_SSH_HOST="${EFFECTIVE_HERMES_SSH_HOST:-host.docker.internal}"
EFFECTIVE_HERMES_SSH_PORT="${HERMES_SSH_PORT:-$CLAUDE_SSH_PORT}"
EFFECTIVE_HERMES_SSH_PORT="${EFFECTIVE_HERMES_SSH_PORT:-22}"
EFFECTIVE_CLAUDE_SSH_USER="${CLAUDE_SSH_USER:-$HERMES_SSH_USER}"
EFFECTIVE_CLAUDE_SSH_HOST="${CLAUDE_SSH_HOST:-$HERMES_SSH_HOST}"
EFFECTIVE_CLAUDE_SSH_HOST="${EFFECTIVE_CLAUDE_SSH_HOST:-host.docker.internal}"
EFFECTIVE_CLAUDE_SSH_PORT="${CLAUDE_SSH_PORT:-$HERMES_SSH_PORT}"
EFFECTIVE_CLAUDE_SSH_PORT="${EFFECTIVE_CLAUDE_SSH_PORT:-22}"

if [[ -n "$EFFECTIVE_HERMES_SSH_USER" ]]; then
  echo "==> Hermes runs on the HOST over SSH as ${EFFECTIVE_HERMES_SSH_USER}@${EFFECTIVE_HERMES_SSH_HOST}:${EFFECTIVE_HERMES_SSH_PORT} (key: $HERMES_SSH_KEY)."
  echo "    Project files are staged in $HERMES_STAGING_DIR (shared with the host at the same path);"
  echo "    the dashboard copies the project there, Hermes edits it, and the result is merged back + pushed."
  echo "    The dashboard agent dropdown exposes 'Hermes' (container) and 'Hermes (Host)' side-by-side;"
  echo "    per-task 'Runner: host' toggles route into the host-staging copy + host's \`hermes\` CLI."
  if [[ -z "$HERMES_SSH_USER" && -n "$CLAUDE_SSH_USER" ]]; then
    echo "    (effective values inherited from CD_CLAUDE_SSH_* since CD_HERMES_SSH_USER is unset)"
  fi
  echo "    Verify connectivity (host must allow this key + have hermes installed):"
  echo "      docker compose exec dashboard ssh -i $HERMES_SSH_KEY -p $EFFECTIVE_HERMES_SSH_PORT -o UserKnownHostsFile=$HERMES_KNOWN_HOSTS -o StrictHostKeyChecking=accept-new ${EFFECTIVE_HERMES_SSH_USER}@${EFFECTIVE_HERMES_SSH_HOST} 'export PATH=\"\$HOME/.local/bin:\$HOME/bin:\$HOME/.cargo/bin:\$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:\$PATH\" && hermes --version'"
elif have hermes; then
  echo "==> Hermes is self-contained in this image (CD_HERMES_SSH_USER unset)."
else
  echo "==> Hermes is DISABLED: set CD_HERMES_SSH_USER (or CD_CLAUDE_SSH_USER — shared wiring) to run the host's Hermes over SSH (delete config.yaml in cd-config to regenerate)."
fi
if [[ -n "$EFFECTIVE_CLAUDE_SSH_USER" ]]; then
  echo "==> Claude Code runs on the HOST over SSH as ${EFFECTIVE_CLAUDE_SSH_USER}@${EFFECTIVE_CLAUDE_SSH_HOST}:${EFFECTIVE_CLAUDE_SSH_PORT} (key: $CLAUDE_SSH_KEY)."
  echo "    The dashboard agent dropdown exposes 'Claude Code' (container) and 'Claude Code (Host)' side-by-side;"
  echo "    per-task 'Runner: host' toggles route into the host-staging copy + host's \`claude\` CLI just like Hermes-SSH."
  if [[ -z "$CLAUDE_SSH_USER" && -n "$HERMES_SSH_USER" ]]; then
    echo "    (effective values inherited from CD_HERMES_SSH_* since CD_CLAUDE_SSH_USER is unset)"
  fi
fi


exec "$@"
