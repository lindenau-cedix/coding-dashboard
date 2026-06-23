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
# The host's Hermes cannot see the data volume, so the dashboard runs it inside a
# COPY of the project here — a dir bind-mounted from the host at the SAME path so
# `cd {project_dir}` resolves identically on both sides. Ensure it exists/writable.
HERMES_STAGING_DIR="${CD_HERMES_STAGING_DIR:-/tmp/coding-dashboard-hermes}"
[[ -n "$HERMES_SSH_USER" ]] && mkdir -p "$HERMES_STAGING_DIR" 2>/dev/null || true
# known_hosts must live somewhere the app user can write (the single-file key
# bind mount leaves ~/.ssh root-owned), so keep it in the home volume root.
HERMES_KNOWN_HOSTS="$HOME/.ssh_known_hosts"
[[ -n "$HERMES_SSH_USER" ]] && { : > "$HERMES_KNOWN_HOSTS" 2>/dev/null || true; touch "$HERMES_KNOWN_HOSTS" 2>/dev/null || true; }

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
import os, shutil, sys, yaml
from app.config import default_agents, DEFAULT_CONTEXT_INSTRUCTION

# --- Hermes-over-SSH wiring (default Docker mode) ---------------------------
# When CD_HERMES_SSH_USER is set, Hermes is NOT run in this container; instead we
# rewrite its command/session_command to drive the HOST's `hermes` over SSH, and
# flag host_staging so the dashboard runs it inside a COPY of the project placed in
# CD_HERMES_STAGING_DIR — a dir bind-mounted at an identical path on the host — so
# the remote `cd {project_dir}` lands on the same files; the result is merged back.
ssh_user = os.environ.get("CD_HERMES_SSH_USER", "").strip()
ssh_host = (os.environ.get("CD_HERMES_SSH_HOST") or "host.docker.internal").strip()
ssh_port = (os.environ.get("CD_HERMES_SSH_PORT") or "22").strip()
ssh_key = "/home/app/.ssh/id_hermes"
known_hosts = os.path.expanduser("~/.ssh_known_hosts")
hermes_ssh = bool(ssh_user)
codex_sandbox = (os.environ.get("CD_CODEX_SANDBOX") or "").strip()
hermes_remote_path = (
    'export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.cargo/bin:'
    '$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"'
)

def _ssh_opts():
    return [
        "-i", ssh_key,
        "-p", ssh_port,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
    ]

def _set_cli_option(command, option, value):
    if not value:
        return command
    out = list(command)
    for idx, tok in enumerate(out):
        if tok == option:
            if idx + 1 < len(out):
                out[idx + 1] = value
            else:
                out.append(value)
            return out
        if tok.startswith(f"{option}="):
            out[idx] = f"{option}={value}"
            return out
    insert_at = len(out) - 1 if out and out[-1] == "-" else len(out)
    out[insert_at:insert_at] = [option, value]
    return out

# Task mode: the prompt is fed to ssh's stdin (prompt_via=stdin) and read on the
# host with "$(cat)", so arbitrary multi-line prompts pass safely without argv
# quoting games. HERMES_ACCEPT_HOOKS/NO_COLOR are set on the REMOTE side.
# `-t <csv>` (mirroring HERMES_NON_INTERACTIVE_TOOLSETS from the Python side)
# restricts the host's Hermes toolset to non-interactive ones (excludes
# `clarify`, which would call into a None platform callback in this one-shot
# mode and stall the run or bounce back with "Clarify tool is not available
# in this execution context."). The dashboard's backfill splices this in
# automatically for existing configs (see _backfill_hermes_flags), so legacy
# SSH-driven installs get the flag too.
HERMES_NON_INTERACTIVE_TOOLSETS_CSV = (
    "web,browser,terminal,file_search,read_file,write_file,"
    "edit_file,multi_edit,plan,session_search,kanban,image_gen,"
    "computer_use,video_gen,tts,spotify,delegate_task,todo,cronjob"
)
HERMES_SSH_TASK_REMOTE = (
    f'cd "{{project_dir}}" && {hermes_remote_path} && '
    'exec env HERMES_ACCEPT_HOOKS=1 NO_COLOR=1 '
    f'hermes chat -q "$(cat)" --yolo --accept-hooks -t {HERMES_NON_INTERACTIVE_TOOLSETS_CSV}'
)
# Session mode: -tt forces a remote PTY (the container side is already a PTY), so
# the interactive TUI works through the double PTY. Start params are appended by
# the runner as extra remote args after `hermes chat`. NO `-t` here: the user is
# at a real terminal and needs the full toolset (including `clarify`).
HERMES_SSH_SESSION_REMOTE = (
    f'cd "{{project_dir}}" && {hermes_remote_path} && exec hermes chat'
)

agents = {}
for key, spec in default_agents().items():
    d = spec.model_dump()
    d.pop("key", None)  # the loader re-derives the key from the mapping
    if key == "hermes" and hermes_ssh:
        d["prompt_via"] = "stdin"
        d["stream_format"] = "raw"
        d["env"] = {}        # set on the remote side instead
        d["unset_env"] = []  # ssh client, not a python venv to sanitise
        d["command"] = ["ssh"] + _ssh_opts() + [f"{ssh_user}@{ssh_host}", HERMES_SSH_TASK_REMOTE]
        d["session_command"] = ["ssh", "-tt"] + _ssh_opts() + [f"{ssh_user}@{ssh_host}", HERMES_SSH_SESSION_REMOTE]
        # Hermes runs on the HOST, which cannot see the dashboard's data volume.
        # host_staging makes the dashboard run it inside a COPY of the project under
        # CD_HERMES_STAGING_DIR (bind-mounted at the same path host<->container):
        # the project is copied in, the host edits it, and the dashboard merges the
        # result back + pushes (a merge conflict is left on a branch for the user).
        d["host_staging"] = True
        d["enabled"] = True
    elif key == "codex" and codex_sandbox:
        d["command"] = _set_cli_option(d["command"], "--sandbox", codex_sandbox)
        d["enabled"] = shutil.which(spec.command[0]) is not None
    else:
        # Enable an agent only if its CLI is actually installed in this image
        # (claude/codex baked in; Hermes only when self-contained / no SSH user).
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
if [[ $any_agent -eq 0 && -z "$HERMES_SSH_USER" ]]; then
  echo "WARN: no agent CLI found in the image — rebuild with the *_NPM_PKG build args." >&2
fi
echo "==> Claude + Codex authenticate via interactive login (credentials persist in the cd-home volume):"
echo "      docker compose exec dashboard claude        # then log in in the TUI"
echo "      docker compose exec dashboard codex login"
if [[ -n "$HERMES_SSH_USER" ]]; then
  echo "==> Hermes runs on the HOST over SSH as ${HERMES_SSH_USER}@${HERMES_SSH_HOST}:${HERMES_SSH_PORT} (key: $HERMES_SSH_KEY)."
  echo "    Project files are staged in $HERMES_STAGING_DIR (shared with the host at the same path);"
  echo "    the dashboard copies the project there, Hermes edits it, and the result is merged back + pushed."
  echo "    Verify connectivity (host must allow this key + have hermes installed):"
  echo "      docker compose exec dashboard ssh -i $HERMES_SSH_KEY -p $HERMES_SSH_PORT -o UserKnownHostsFile=$HERMES_KNOWN_HOSTS -o StrictHostKeyChecking=accept-new ${HERMES_SSH_USER}@${HERMES_SSH_HOST} 'export PATH=\"\$HOME/.local/bin:\$HOME/bin:\$HOME/.cargo/bin:\$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:\$PATH\" && hermes --version'"
elif have hermes; then
  echo "==> Hermes is self-contained in this image (CD_HERMES_SSH_USER unset)."
else
  echo "==> Hermes is DISABLED: set CD_HERMES_SSH_USER to run the host's Hermes over SSH (delete config.yaml in cd-config to regenerate)."
fi


exec "$@"
