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

# --- Hermes runs on the HOST over SSH (opt-in) -----------------------------
# Hermes IS in this image by default (the Dockerfile installs it via the
# upstream one-liner). When CD_HERMES_SSH_USER is set, the generated
# config.yaml ALSO registers a `hermes-host` sibling that runs the host's
# `hermes` via `ssh <user>@<host> '… hermes …'` (see the Python block below).
# This is the opt-in path for operators who want exactly one Hermes process
# tree on the host — its cronjobs / paired channels (WhatsApp, Telegram) keep
# firing when the dashboard container is down. With CD_HERMES_SSH_USER set
# the in-image Hermes stays selectable too; the per-task "Runner: host"
# dropdown exposes both. Leave CD_HERMES_SSH_USER empty for the in-image
# default (no SSH, no second Hermes on the host).
HERMES_SSH_USER="${CD_HERMES_SSH_USER:-}"
HERMES_SSH_HOST="${CD_HERMES_SSH_HOST:-host.docker.internal}"
HERMES_SSH_PORT="${CD_HERMES_SSH_PORT:-22}"
# Default key path follows the same shared-wiring rule as user/host/port:
# each agent owns ``/home/app/.ssh/id_<agent>`` by default, but a sibling
# that inherited its SSH user from the other agent also inherits that
# agent's key path. An explicit ``CD_{HERMES,CLAUDE}_SSH_KEY`` env var
# pins the path (no fallback). The Python generator
# (backend/app/config_bootstrap.py) applies the same rule plus an
# on-disk existence fallback; this shell resolver keeps the boot log
# honest about which key will actually be used.
HERMES_SSH_KEY="${CD_HERMES_SSH_KEY:-/home/app/.ssh/id_hermes}"
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
CLAUDE_SSH_KEY="${CD_CLAUDE_SSH_KEY:-/home/app/.ssh/id_claude}"
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
#
# The cd-home VOLUME is bind-mounted over /home/app, which SHADOWS the
# ~/.local/bin/hermes + ~/.hermes/ the Dockerfile installed into the image.
# To make the in-image install visible in the container, seed it from the
# image on first boot of a fresh cd-home volume: copy /usr/local/share/hermes
# (a non-overlapping dir the image installs to) over the volume's empty
# ~/.local/bin + ~/.hermes. On subsequent restarts the volume already has
# the install (and the seed marker), so this is a no-op.
HERMES_INSTALL_CMD="${HERMES_INSTALL_CMD:-}"
# Dockerfile's HERMES_INSTALL_CMD is a BUILD arg, NOT a runtime env. The
# docker-compose.yml `environment:` block (see below) forwards it as a
# runtime env when set — empty here means the operator chose the SSH
# fallback. Default in the Docker image is the upstream installer; we
# always seed from the image-baked install, not by re-running the script.
seed_marker="$HOME/.hermes/.seeded_from_image"
image_hermes_src="/usr/local/share/hermes"
if [[ -d "$image_hermes_src" ]] && [[ ! -e "$seed_marker" ]] && ! have hermes; then
  echo "==> First boot: seeding in-image Hermes from $image_hermes_src into $HOME"
  mkdir -p "$HOME/.local/bin" "$HOME/.hermes" 2>/dev/null || true
  # Copy conservatively (no clobber) so we never overwrite a half-installed
  # setup if one exists. The marker + a successful hermes --version is the
  # real success signal.
  if cp -rn "$image_hermes_src/." "$HOME/" 2>/dev/null \
       && chown -R app:app "$HOME/.local" "$HOME/.hermes" 2>/dev/null; then
    if have hermes; then
      touch "$seed_marker" 2>/dev/null || true
      echo "==> Hermes seeded from image: $(hermes --version 2>&1 | head -1)"
    else
      echo "==> WARNING: seed copied files but 'hermes' is still not on PATH" >&2
    fi
  else
    echo "==> WARNING: Hermes seed copy failed (cp/chown)" >&2
  fi
fi
hermes_shim="$HOME/.local/bin/hermes"
hermes_venv_bin="$HOME/.hermes/hermes-agent/venv/bin"
[[ -e "$hermes_shim" ]] && chmod u+rx "$hermes_shim" 2>/dev/null || true
[[ -d "$hermes_venv_bin" ]] && chmod -R u+rx "$hermes_venv_bin" 2>/dev/null || true

# --- first-boot config generation ------------------------------------------
# Two cases trigger generation:
#   (a) the file is absent (fresh cd-config volume — the established contract);
#   (b) the `hermes` seed step above just placed a fresh install on PATH that
#       the existing config.yaml predates — re-generate so `hermes: enabled`
#       flips to True without requiring the operator to delete the file.
# We trigger (b) only when EITHER the seed just happened OR the seed already
# ran on this volume AND `hermes` is on PATH AND the YAML still says
# `hermes: enabled: false` (a leftover from a pre-seed boot).
seed_just_done=0
# Use Python to peek at the YAML — robust against indent quirks, no awk regex
# pitfalls. We trigger regen when:
#   - hermes is now on PATH, AND
#   - the existing YAML's `hermes:` entry has `enabled: false` (left over from
#     a pre-seed boot — or any other time hermes was genuinely missing).
hermes_yaml_state="$(python - <<'PY' 2>/dev/null || echo MISSING
import sys, yaml, os
path = "/etc/coding-dashboard/config.yaml"
if not os.path.exists(path):
    print("MISSING"); sys.exit(0)
try:
    doc = yaml.safe_load(open(path))
except Exception:
    print("MISSING"); sys.exit(0)
agents = (doc or {}).get("agents") or {}
h = agents.get("hermes")
if h is None:
    print("MISSING")
elif h.get("enabled") is False:
    print("DISABLED")
else:
    print("OK")
PY
)"
hermes_on_path_now=0
have hermes && hermes_on_path_now=1
should_regen=0
if [[ ! -f "$CONFIG_YAML" ]]; then
  should_regen=1
elif [[ "$hermes_on_path_now" -eq 1 ]] && [[ "$hermes_yaml_state" == "DISABLED" ]]; then
  should_regen=1
fi
if [[ "$should_regen" -eq 1 ]]; then
  if [[ -f "$CONFIG_YAML" ]]; then
    echo "==> Regenerating $CONFIG_YAML (hermes now on PATH, old YAML predates seed)"
  else
    echo "==> First boot: generating $CONFIG_YAML"
  fi
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
# Key paths follow the same shared-wiring rule (a sibling that inherited
# the other agent's SSH user also inherits its key path), plus an on-disk
# existence fallback: if the chosen key is missing but the other agent's
# key is present, the existing key wins. This keeps the boot log + the
# ``Verify connectivity`` snippet aligned with the Python generator.
# Key paths follow the same shared-wiring rule as user/host/port: a sibling
# that inherited its SSH user from the other agent also inherits that
# agent's default key path. An explicit ``CD_{HERMES,CLAUDE}_SSH_KEY`` env
# override always wins — a pin is the operator's way to say "use THIS key,
# do not second-guess me", regardless of which user the SSH connection
# ended up with. The Python generator in backend/app/config_bootstrap.py
# applies the same rule plus an on-disk existence fallback; this shell
# resolver keeps the boot log + the ``Verify connectivity`` snippet in
# sync with it.
# ``set -u`` requires explicit default-initialisation before referencing
# these env vars (they are optional in docker-compose env_file).
CD_HERMES_SSH_KEY="${CD_HERMES_SSH_KEY:-}"
CD_CLAUDE_SSH_KEY="${CD_CLAUDE_SSH_KEY:-}"
EFFECTIVE_HERMES_SSH_KEY="$HERMES_SSH_KEY"
EFFECTIVE_CLAUDE_SSH_KEY="$CLAUDE_SSH_KEY"
if [[ -n "$HERMES_SSH_USER" ]]; then
  [[ -z "$CD_HERMES_SSH_KEY" ]] && EFFECTIVE_HERMES_SSH_KEY="$HERMES_SSH_KEY"
elif [[ -n "$EFFECTIVE_HERMES_SSH_USER" && -z "$CD_HERMES_SSH_KEY" ]]; then
  EFFECTIVE_HERMES_SSH_KEY="$CLAUDE_SSH_KEY"
fi
if [[ -n "$CLAUDE_SSH_USER" ]]; then
  [[ -z "$CD_CLAUDE_SSH_KEY" ]] && EFFECTIVE_CLAUDE_SSH_KEY="$CLAUDE_SSH_KEY"
elif [[ -n "$EFFECTIVE_CLAUDE_SSH_USER" && -z "$CD_CLAUDE_SSH_KEY" ]]; then
  EFFECTIVE_CLAUDE_SSH_KEY="$HERMES_SSH_KEY"
fi
# On-disk existence fallback — a configured key that isn't there yet is
# useless; prefer the other agent's key if THAT one exists. Skipped when
# the operator pinned the path via env.
if [[ -n "$EFFECTIVE_HERMES_SSH_KEY" && ! -e "$EFFECTIVE_HERMES_SSH_KEY" \
      && -z "$CD_HERMES_SSH_KEY" \
      && -e "$EFFECTIVE_CLAUDE_SSH_KEY" ]]; then
  EFFECTIVE_HERMES_SSH_KEY="$EFFECTIVE_CLAUDE_SSH_KEY"
fi
if [[ -n "$EFFECTIVE_CLAUDE_SSH_KEY" && ! -e "$EFFECTIVE_CLAUDE_SSH_KEY" \
      && -z "$CD_CLAUDE_SSH_KEY" \
      && -e "$EFFECTIVE_HERMES_SSH_KEY" ]]; then
  EFFECTIVE_CLAUDE_SSH_KEY="$EFFECTIVE_HERMES_SSH_KEY"
fi

if [[ -n "$EFFECTIVE_HERMES_SSH_USER" ]]; then
  echo "==> Hermes runs on the HOST over SSH as ${EFFECTIVE_HERMES_SSH_USER}@${EFFECTIVE_HERMES_SSH_HOST}:${EFFECTIVE_HERMES_SSH_PORT} (key: $EFFECTIVE_HERMES_SSH_KEY)."
  echo "    Project files are staged in $HERMES_STAGING_DIR (shared with the host at the same path);"
  echo "    the dashboard copies the project there, Hermes edits it, and the result is merged back + pushed."
  echo "    The dashboard agent dropdown exposes 'Hermes' (container) and 'Hermes (Host)' side-by-side;"
  echo "    per-task 'Runner: host' toggles route into the host-staging copy + host's \`hermes\` CLI."
  if [[ -z "$HERMES_SSH_USER" && -n "$CLAUDE_SSH_USER" ]]; then
    echo "    (effective values inherited from CD_CLAUDE_SSH_* since CD_HERMES_SSH_USER is unset)"
  fi
  if [[ "$EFFECTIVE_HERMES_SSH_KEY" != "$HERMES_SSH_KEY" ]]; then
    echo "    (key path inherited/fell back to $EFFECTIVE_HERMES_SSH_KEY — set CD_HERMES_SSH_KEY to pin)"
  fi
  echo "    Verify connectivity (host must allow this key + have hermes installed):"
  echo "      docker compose exec dashboard ssh -i $EFFECTIVE_HERMES_SSH_KEY -p $EFFECTIVE_HERMES_SSH_PORT -o UserKnownHostsFile=$HERMES_KNOWN_HOSTS -o StrictHostKeyChecking=accept-new ${EFFECTIVE_HERMES_SSH_USER}@${EFFECTIVE_HERMES_SSH_HOST} 'export PATH=\"\$HOME/.local/bin:\$HOME/bin:\$HOME/.cargo/bin:\$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:\$PATH\" && hermes --version'"
elif have hermes; then
  echo "==> Hermes is self-contained in this image (CD_HERMES_SSH_USER unset)."
else
  echo "==> Hermes is DISABLED: set CD_HERMES_SSH_USER (or CD_CLAUDE_SSH_USER — shared wiring) to run the host's Hermes over SSH (delete config.yaml in cd-config to regenerate)."
fi
if [[ -n "$EFFECTIVE_CLAUDE_SSH_USER" ]]; then
  echo "==> Claude Code runs on the HOST over SSH as ${EFFECTIVE_CLAUDE_SSH_USER}@${EFFECTIVE_CLAUDE_SSH_HOST}:${EFFECTIVE_CLAUDE_SSH_PORT} (key: $EFFECTIVE_CLAUDE_SSH_KEY)."
  echo "    The dashboard agent dropdown exposes 'Claude Code' (container) and 'Claude Code (Host)' side-by-side;"
  echo "    per-task 'Runner: host' toggles route into the host-staging copy + host's \`claude\` CLI just like Hermes-SSH."
  if [[ -z "$CLAUDE_SSH_USER" && -n "$HERMES_SSH_USER" ]]; then
    echo "    (effective values inherited from CD_HERMES_SSH_* since CD_CLAUDE_SSH_USER is unset)"
  fi
  if [[ "$EFFECTIVE_CLAUDE_SSH_KEY" != "$CLAUDE_SSH_KEY" ]]; then
    echo "    (key path inherited/fell back to $EFFECTIVE_CLAUDE_SSH_KEY — set CD_CLAUDE_SSH_KEY to pin)"
  fi
fi


exec "$@"
