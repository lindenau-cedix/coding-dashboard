"""First-boot ``config.yaml`` generator.

Extracted from ``deploy/docker/entrypoint.sh`` so the same logic is
unit-testable without spawning bash. The Docker entrypoint calls
:func:`generate_initial_agents_config` once on first boot to produce the
initial ``agents:`` mapping; the result is then ``yaml.safe_dump``-ed to
``/etc/coding-dashboard/config.yaml``. The backend's normal config loader
(``app.config.load_agents_config``) reads the resulting YAML on every
startup.

The contract here intentionally matches the entrypoint heredoc 1:1
(plus the new ``hermes-host`` sibling registration and the
shared-SSH-wiring resolver), so existing operator deployments see no
behaviour change unless they upgrade.
"""
from __future__ import annotations

import copy as _copy
import os
import shutil
from typing import Mapping


# Hermes toolsets allowed in NON-INTERACTIVE task / goal runs.
# Must stay in sync with ``app.config.HERMES_NON_INTERACTIVE_TOOLSETS`` —
# the value is duplicated here to avoid an import cycle (the entrypoint
# can't import from a not-yet-installed ``app`` package at first boot in
# every deployment shape, and the smoke test runner imports this module
# WITHOUT booting FastAPI).
HERMES_NON_INTERACTIVE_TOOLSETS_CSV = (
    "web,browser,terminal,file_search,read_file,write_file,"
    "edit_file,multi_edit,plan,session_search,kanban,image_gen,"
    "computer_use,video_gen,tts,spotify,delegate_task,todo,cronjob"
)

# Local import — guarded so the entrypoint can still import this module
# in environments where ``app.config`` is not on the import path (a
# fall-back copy of DEFAULT_CONTEXT_INSTRUCTION is hardcoded at the
# bottom of this module for the same reason).
try:
    from .config import DEFAULT_CONTEXT_INSTRUCTION, default_agents
except ImportError:  # pragma: no cover - entrypoint-style first-boot
    DEFAULT_CONTEXT_INSTRUCTION = ""
    default_agents = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Helper functions (carried over from the entrypoint heredoc, verbatim)
# --------------------------------------------------------------------------- #
def _set_cli_option(command: list[str], option: str, value: str) -> list[str]:
    """Set or insert a two-token CLI option without disturbing a trailing stdin marker."""
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


def _ssh_argv(
    *,
    user: str,
    host: str,
    port: str,
    keyfile: str,
    remote_shell: str,
    known_hosts: str,
    force_tty: bool = False,
) -> list[str]:
    """Build the argv list for an ``ssh user@host '<remote-cmd>'`` invocation."""
    base = ["ssh"]
    if force_tty:
        base += ["-tt"]
    return base + [
        "-i", keyfile,
        "-p", port,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
        remote_shell,
    ]


def _ssh_remote_path_export() -> str:
    """PATH extension every host-side SSH remote shell prepends.

    SSH login shells do not inherit the operator's interactive shell PATH,
    so a ``claude`` / ``hermes`` installed under ``~/.local/bin``,
    ``~/.npm-global/bin`` or ``~/.cargo/bin`` is invisible without this.
    Mirror this same chain on every remote-shell string so the agent CLIs
    are findable regardless of how the operator installed them on the host.
    """
    return (
        'export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.cargo/bin:'
        '$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"'
    )


def _hermes_ssh_task_remote() -> str:
    """Hermes ``-q`` task-mode remote shell string (stdin prompt)."""
    return (
        f'cd "{{project_dir}}" && {_ssh_remote_path_export()} && '
        'exec env HERMES_ACCEPT_HOOKS=1 NO_COLOR=1 '
        f'hermes chat -q "$(cat)" --yolo --accept-hooks -t {HERMES_NON_INTERACTIVE_TOOLSETS_CSV}'
    )


def _hermes_ssh_session_remote() -> str:
    """Hermes interactive TUI session-mode remote shell string."""
    return (
        f'cd "{{project_dir}}" && {_ssh_remote_path_export()} && '
        'exec hermes chat'
    )


def _claude_ssh_task_remote() -> str:
    """Claude Code ``-p`` task-mode remote shell string (stdin prompt)."""
    return (
        f'cd "{{project_dir}}" && {_ssh_remote_path_export()} && '
        'exec env -u ANTHROPIC_API_KEY claude '
        '-p "$(cat)" --output-format stream-json --verbose '
        '--dangerously-skip-permissions'
    )


def _claude_ssh_session_remote() -> str:
    """Claude Code interactive TUI session-mode remote shell string."""
    return (
        f'cd "{{project_dir}}" && {_ssh_remote_path_export()} && '
        'exec env -u ANTHROPIC_API_KEY claude'
    )


# --------------------------------------------------------------------------- #
# The single public entry point
# --------------------------------------------------------------------------- #
def generate_initial_agents_config(
    env: Mapping[str, str] | None = None,
    *,
    home: str | None = None,
) -> dict:
    """Build the ``agents:`` mapping for a fresh ``config.yaml``.

    Args:
      env:   env-var mapping to read from. Defaults to ``os.environ`` (the
             entrypoint's process env). The smoke tests pass a synthetic
             mapping to drive the four-case shared-SSH matrix without
             mutating the real process env.
      home:  value of ``$HOME`` (used to resolve ``~/.ssh_known_hosts``
             and the ``/home/app/.ssh/id_*`` key paths). Defaults to
             ``os.environ["HOME"]``; can be overridden by callers that
             stage a fake HOME.

    Returns:
      A ``{"context_instruction": ..., "agents": {<key>: <spec-dict>, ...}}``
      dict suitable for ``yaml.safe_dump``. Each spec-dict mirrors the
      ``AgentSpec`` model field set the YAML loader understands.
    """
    env = dict(env) if env is not None else dict(os.environ)
    home = home if home is not None else env.get("HOME", "/root")
    # ``shutil.which`` defaults to ``os.defpath`` (``/bin:/usr/bin``) when
    # called without an explicit ``path=``, NOT to ``os.environ["PATH"]``.
    # We must read PATH from the resolved env so the generator honours a
    # caller-supplied PATH (smoke tests + the Docker entrypoint both rely
    # on this — when the entrypoint runs in a container without
    # ``/bin``/``/usr/bin`` on PATH for every CLI, the wrong default
    # would silently disable every agent).
    effective_path = env.get("PATH") or os.environ.get("PATH") or os.defpath

    # --- Shared host-over-SSH wiring ----------------------------------------
    # Hermes and Claude Code both run on the host over SSH. Operators
    # configure ONE pair of CD_{HERMES,CLAUDE}_SSH_{USER,HOST,PORT} env vars
    # and it applies to BOTH siblings; setting both is allowed but only
    # meaningful when the two agents really point at different hosts. If
    # neither is set, neither ``-host`` sibling is registered.
    hermes_user = env.get("CD_HERMES_SSH_USER", "").strip()
    hermes_host = (env.get("CD_HERMES_SSH_HOST") or "host.docker.internal").strip()
    hermes_port = (env.get("CD_HERMES_SSH_PORT") or "22").strip()
    claude_user = env.get("CD_CLAUDE_SSH_USER", "").strip()
    claude_host = (env.get("CD_CLAUDE_SSH_HOST") or "host.docker.internal").strip()
    claude_port = (env.get("CD_CLAUDE_SSH_PORT") or "22").strip()

    # Effective values: prefer that agent's own env, fall back to the
    # OTHER agent's (so configuring Hermes-only also lights up claude-host,
    # and vice-versa).
    hermes_ssh_user = hermes_user or claude_user
    hermes_ssh_host = hermes_host if hermes_user else (claude_host or "host.docker.internal")
    hermes_ssh_port = hermes_port if hermes_user else (claude_port or "22")
    claude_ssh_user = claude_user or hermes_user
    claude_ssh_host = claude_host if claude_user else (hermes_host or "host.docker.internal")
    claude_ssh_port = claude_port if claude_user else (hermes_port or "22")
    hermes_ssh_active = bool(hermes_ssh_user)
    claude_ssh_active = bool(claude_ssh_user)

    # --- SSH private-key resolution (shared-wiring, mirrors user/host/port) --
    # Each sibling defaults to its own key path, but:
    #   * an explicit ``CD_{HERMES,CLAUDE}_SSH_KEY`` env override wins;
    #   * when a sibling inherited the OTHER agent's SSH user (only one
    #     ``CD_*_SSH_USER`` set), it also inherits that agent's key path — a
    #     Hermes-only setup should drive claude-host with the Hermes key
    #     instead of pointing at a non-existent ``id_claude``;
    #   * finally, if the resolved key file does not exist but the other
    #     agent's key does, fall back to that one (unless pinned by env).
    # The file-existence check honours the caller-supplied HOME so the smoke
    # tests and the container agree on where the keys live.
    default_hermes_key = os.path.join(home, ".ssh", "id_hermes")
    default_claude_key = os.path.join(home, ".ssh", "id_claude")
    hermes_key_env = (env.get("CD_HERMES_SSH_KEY") or "").strip()
    claude_key_env = (env.get("CD_CLAUDE_SSH_KEY") or "").strip()

    # Step 1: env override, else inherit the effective user's own key.
    hermes_ssh_key = hermes_key_env or (
        default_hermes_key if hermes_user else default_claude_key
    )
    claude_ssh_key = claude_key_env or (
        default_claude_key if claude_user else default_hermes_key
    )

    # Step 2: existence fallback — a configured key that isn't on disk yet is
    # useless; prefer the other agent's key if THAT one exists. Skipped when
    # the operator pinned the path explicitly via env.
    def _resolve_key(chosen: str, other: str, pinned: bool) -> str:
        if pinned:
            return chosen
        if not os.path.exists(chosen) and os.path.exists(other):
            return other
        return chosen

    hermes_ssh_key = _resolve_key(
        hermes_ssh_key, default_claude_key, bool(hermes_key_env)
    )
    claude_ssh_key = _resolve_key(
        claude_ssh_key, default_hermes_key, bool(claude_key_env)
    )

    known_hosts = os.path.join(home, ".ssh_known_hosts")
    codex_sandbox = (env.get("CD_CODEX_SANDBOX") or "").strip()

    # The smoke tests run without a real ``app`` package import path in
    # every environment; in those cases ``default_agents()`` is None and
    # we cannot resolve the built-ins. The Docker entrypoint always has
    # the package on PYTHONPATH (/app/backend), so it sees the real
    # defaults. This guard keeps the module importable in isolation.
    if default_agents is None:
        raise RuntimeError(
            "app.config.default_agents is unavailable — "
            "run from the backend/ directory with PYTHONPATH including the "
            "backend root."
        )

    # --- Walk built-ins, emit siblings, write entry -------------------------
    agents: dict[str, dict] = {}
    for key, spec in default_agents().items():
        d = spec.model_dump()
        # The loader re-derives the key from the mapping; strip it from
        # the per-spec dump to avoid spurious noise in the generated YAML.
        d.pop("key", None)

        # Emit the base agent FIRST so it precedes any ``<base>-host``
        # sibling we inject below. ``/api/agents`` preserves this dict
        # order, and the frontend picks the first enabled entry as its
        # default — if a ``-host`` sibling came first, the default agent
        # would silently be the SSH runner (publickey failures on the very
        # first submit even though the UI shows "Container").
        agents[key] = d

        if key == "codex" and codex_sandbox:
            d["command"] = _set_cli_option(d["command"], "--sandbox", codex_sandbox)
            d["enabled"] = shutil.which(spec.command[0], path=effective_path) is not None
        elif key == "claude":
            # Container-side ``claude`` stays enabled iff its CLI is on
            # PATH — same default the loop applies for every other
            # built-in. The SSH-driven sibling is registered SEPARATELY
            # below (no more in-place mutation of this entry).
            d["enabled"] = shutil.which(spec.command[0], path=effective_path) is not None
            if claude_ssh_active:
                host_d = _copy.deepcopy(d)
                host_d["key"] = "claude-host"
                host_d["display_name"] = "Claude Code (Host)"
                host_d["prompt_via"] = "stdin"
                host_d["stream_format"] = "raw"
                host_d["env"] = {}        # set on the remote side instead
                host_d["unset_env"] = []  # ssh client, not a python venv to sanitise
                host_d["command"] = _ssh_argv(
                    user=claude_ssh_user,
                    host=claude_ssh_host,
                    port=claude_ssh_port,
                    keyfile=claude_ssh_key,
                    known_hosts=known_hosts,
                    remote_shell=_claude_ssh_task_remote(),
                )
                host_d["session_command"] = _ssh_argv(
                    user=claude_ssh_user,
                    host=claude_ssh_host,
                    port=claude_ssh_port,
                    keyfile=claude_ssh_key,
                    known_hosts=known_hosts,
                    remote_shell=_claude_ssh_session_remote(),
                    force_tty=True,
                )
                # Claude runs on the HOST, which cannot see the dashboard's
                # data volume. Same host_staging pattern as Hermes-SSH.
                host_d["host_staging"] = True
                host_d["enabled"] = True
                # Re-attach the model/effort argv flags the way the
                # container-side spec does (so the per-task selectors
                # keep working through the SSH argv).
                host_d["command"] = _set_cli_option(host_d["command"], "--model", "{model}")
                host_d["command"] = _set_cli_option(host_d["command"], "--effort", "{effort}")
                agents["claude-host"] = host_d
        elif key == "hermes":
            # The ``hermes`` entry now ALWAYS stays as the container-side
            # CLI (the SSH-driven variant has moved into the
            # ``hermes-host`` sibling below — same shape as the
            # ``claude`` / ``claude-host`` pair). In self-contained mode
            # (no SSH user) this is the only Hermes entry; in SSH mode
            # both this entry AND ``hermes-host`` exist side-by-side.
            d["enabled"] = shutil.which(spec.command[0], path=effective_path) is not None
            if hermes_ssh_active:
                host_d = _copy.deepcopy(d)
                host_d["key"] = "hermes-host"
                host_d["display_name"] = "Hermes (Host)"
                host_d["prompt_via"] = "stdin"
                host_d["stream_format"] = "raw"
                host_d["env"] = {}        # set on the remote side instead
                host_d["unset_env"] = []  # ssh client, not a python venv to sanitise
                host_d["command"] = _ssh_argv(
                    user=hermes_ssh_user,
                    host=hermes_ssh_host,
                    port=hermes_ssh_port,
                    keyfile=hermes_ssh_key,
                    known_hosts=known_hosts,
                    remote_shell=_hermes_ssh_task_remote(),
                )
                host_d["session_command"] = _ssh_argv(
                    user=hermes_ssh_user,
                    host=hermes_ssh_host,
                    port=hermes_ssh_port,
                    keyfile=hermes_ssh_key,
                    known_hosts=known_hosts,
                    remote_shell=_hermes_ssh_session_remote(),
                    force_tty=True,
                )
                # Hermes-on-host cannot see the dashboard's data volume,
                # so it runs in a host_staging copy of the project —
                # same plumbing as Claude-on-host.
                host_d["host_staging"] = True
                host_d["enabled"] = True
                agents["hermes-host"] = host_d
        else:
            # Generic built-in (codex, others): enable iff the CLI is on
            # PATH at first boot.
            d["enabled"] = shutil.which(spec.command[0], path=effective_path) is not None
        # NOTE: ``agents[key] = d`` was done at the top of the loop so the
        # base agent precedes its ``-host`` sibling. ``d`` is stored by
        # reference, so the ``d["enabled"] = ...`` mutations above still
        # take effect on the emitted entry.

    return {"context_instruction": DEFAULT_CONTEXT_INSTRUCTION, "agents": agents}