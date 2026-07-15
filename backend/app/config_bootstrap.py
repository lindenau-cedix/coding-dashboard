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


def _codex_ssh_task_remote() -> str:
    """Codex ``exec`` task-mode remote shell string (stdin prompt).

    No ``--output-last-message {last_message_file}`` — the placeholder
    resolves to a tempfile path inside the dashboard container, which the
    SSH-driven host process cannot write to. The summary falls back to
    ``_CodexParser.summary()`` via the empty ``{last_message_file}``
    no-op branch in ``agents.run_agent``.
    """
    return (
        f'cd "{{project_dir}}" && {_ssh_remote_path_export()} && '
        'exec env NO_COLOR=1 codex exec --cd "{project_dir}" '
        '--sandbox workspace-write --color never --ephemeral -'
    )


def _codex_ssh_session_remote() -> str:
    """Codex interactive TUI session-mode remote shell string."""
    return (
        f'cd "{{project_dir}}" && {_ssh_remote_path_export()} && '
        'exec codex'
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
    # Hermes, Claude Code, and Codex all run on the host over SSH. Operators
    # configure ONE pair of CD_{HERMES,CLAUDE,CODEX}_SSH_{USER,HOST,PORT} env
    # vars and it applies to ALL three siblings; setting more than one is
    # allowed but only meaningful when the agents really point at different
    # hosts. If none are set, no ``-host`` sibling is registered.
    #
    # The agents list below also drives the loop that picks each sibling's
    # effective user/host/port (preferring its own env, falling back to the
    # next agent's values in order) and the matching key-path resolver. Keep
    # the order in sync with the corresponding main-loop branch order so
    # the smoke test ordering invariant (``base agent before its -host``)
    # is preserved when a sibling falls back to the next agent's wiring.
    ssh_agents: tuple[str, ...] = ("hermes", "claude", "codex")

    def _own(agent: str, kind: str) -> tuple[str, str, str]:
        """Read an agent's own CD_<AGENT>_SSH_{USER,HOST,PORT} triple."""
        user = env.get(f"CD_{agent.upper()}_SSH_USER", "").strip()
        host = (
            env.get(f"CD_{agent.upper()}_SSH_HOST") or "host.docker.internal"
        ).strip()
        port = (env.get(f"CD_{agent.upper()}_SSH_PORT") or "22").strip()
        return user, host, port

    # Per-agent OWN triples (for the loop below + the key resolver).
    own: dict[str, tuple[str, str, str]] = {a: _own(a, "user_host_port") for a in ssh_agents}

    # Effective triples: prefer the agent's own env; otherwise walk the
    # OTHER agents in ``ssh_agents`` order and inherit the first one that
    # has a non-empty user. Active iff the resolved user is non-empty.
    effective: dict[str, tuple[str, str, str]] = {}
    active: dict[str, bool] = {}
    for idx, agent in enumerate(ssh_agents):
        user, host, port = own[agent]
        if not user:
            for other in ssh_agents[:idx] + ssh_agents[idx + 1:]:
                o_user, o_host, o_port = own[other]
                if o_user:
                    user, host, port = o_user, o_host, o_port
                    break
        host = host or "host.docker.internal"
        port = port or "22"
        effective[agent] = (user, host, port)
        active[agent] = bool(user)

    hermes_user, hermes_host, hermes_port = own["hermes"]
    claude_user, claude_host, claude_port = own["claude"]
    codex_user, codex_host, codex_port = own["codex"]
    hermes_ssh_user, hermes_ssh_host, hermes_ssh_port = effective["hermes"]
    claude_ssh_user, claude_ssh_host, claude_ssh_port = effective["claude"]
    codex_ssh_user, codex_ssh_host, codex_ssh_port = effective["codex"]
    hermes_ssh_active = active["hermes"]
    claude_ssh_active = active["claude"]
    codex_ssh_active = active["codex"]

    # --- SSH private-key resolution (shared-wiring, mirrors user/host/port) --
    # Each sibling defaults to its own key path, but:
    #   * an explicit ``CD_{HERMES,CLAUDE,CODEX}_SSH_KEY`` env override wins;
    #   * when a sibling inherited another agent's SSH user (only one
    #     ``CD_*_SSH_USER`` set), it also inherits that agent's key path — a
    #     Hermes-only setup should drive claude-host with the Hermes key
    #     instead of pointing at a non-existent ``id_claude``;
    #   * finally, if the resolved key file does not exist but the other
    #     agent's key does, fall back to that one (unless pinned by env).
    # The file-existence check honours the caller-supplied HOME so the smoke
    # tests and the container agree on where the keys live.
    default_keys: dict[str, str] = {
        agent: os.path.join(home, ".ssh", f"id_{agent}")
        for agent in ssh_agents
    }
    key_env: dict[str, str] = {
        agent: (env.get(f"CD_{agent.upper()}_SSH_KEY") or "").strip()
        for agent in ssh_agents
    }

    # Step 1: env override, else inherit the effective user's own key.
    # ``inherited_from[agent]`` is the name of the agent whose user/host/port
    # we fell back to (or ``agent`` itself when it owns its own user).
    inherited_from: dict[str, str] = {}
    for idx, agent in enumerate(ssh_agents):
        own_user = own[agent][0]
        if own_user:
            inherited_from[agent] = agent
        else:
            for other in ssh_agents[:idx] + ssh_agents[idx + 1:]:
                if own[other][0]:
                    inherited_from[agent] = other
                    break
            else:
                inherited_from[agent] = agent

    ssh_key: dict[str, str] = {}
    for agent in ssh_agents:
        if key_env[agent]:
            ssh_key[agent] = key_env[agent]
        else:
            # Own key path if the operator configured this agent's own user;
            # otherwise inherit the key path of the agent whose user we
            # fell back to (e.g. Hermes-only setup -> claude-host uses
            # ``id_hermes`` rather than the absent ``id_claude``).
            ssh_key[agent] = default_keys[inherited_from[agent]]

    # Step 2: existence fallback — a configured key that isn't on disk yet is
    # useless; prefer the next agent's key if THAT one exists. Skipped when
    # the operator pinned the path explicitly via env.
    def _resolve_key(agent: str, chosen: str, pinned: bool) -> str:
        if pinned:
            return chosen
        for other in ssh_agents:
            if other == agent:
                continue
            other_key = ssh_key.get(other) or default_keys[other]
            if other_key != chosen and os.path.exists(other_key) and not os.path.exists(chosen):
                return other_key
        return chosen

    for agent in ssh_agents:
        ssh_key[agent] = _resolve_key(agent, ssh_key[agent], bool(key_env[agent]))

    hermes_ssh_key = ssh_key["hermes"]
    claude_ssh_key = ssh_key["claude"]
    codex_ssh_key = ssh_key["codex"]

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

        if key == "codex":
            # Container-side ``codex`` stays enabled iff its CLI is on PATH
            # (same default the loop applies for every other built-in).
            # The SSH-driven sibling is registered SEPARATELY below when
            # ``CD_CODEX_SSH_USER`` is set — same shape as the
            # ``claude`` / ``claude-host`` pair.
            if codex_sandbox:
                d["command"] = _set_cli_option(d["command"], "--sandbox", codex_sandbox)
            d["enabled"] = shutil.which(spec.command[0], path=effective_path) is not None
            if codex_ssh_active:
                host_d = _copy.deepcopy(d)
                host_d["key"] = "codex-host"
                host_d["display_name"] = "Codex (Host)"
                host_d["prompt_via"] = "stdin"
                host_d["stream_format"] = "codex"
                host_d["env"] = {}        # set on the remote side instead
                host_d["unset_env"] = []  # ssh client, not a python venv to sanitise
                host_d["command"] = _ssh_argv(
                    user=codex_ssh_user,
                    host=codex_ssh_host,
                    port=codex_ssh_port,
                    keyfile=codex_ssh_key,
                    known_hosts=known_hosts,
                    remote_shell=_codex_ssh_task_remote(),
                )
                host_d["session_command"] = _ssh_argv(
                    user=codex_ssh_user,
                    host=codex_ssh_host,
                    port=codex_ssh_port,
                    keyfile=codex_ssh_key,
                    known_hosts=known_hosts,
                    remote_shell=_codex_ssh_session_remote(),
                    force_tty=True,
                )
                # Codex-on-host cannot see the dashboard's data volume, so
                # it runs in a host_staging copy of the project — same
                # plumbing as Claude-on-host and Hermes-on-host.
                host_d["host_staging"] = True
                host_d["enabled"] = True
                # Drop ``--output-last-message {last_message_file}``: the
                # placeholder resolves to a tempfile inside the container,
                # which the host SSH process can't write to. Without the
                # placeholder ``run_agent`` skips creating the temp file
                # entirely and the parser's ``summary()`` is used instead.
                host_d["command"] = [
                    tok for tok in host_d["command"]
                    if not (tok == "--output-last-message"
                            or tok.startswith("{last_message_file}"))
                ]
                # Re-attach the model/effort argv flags. Codex uses
                # ``-c model_reasoning_effort=...`` rather than a
                # ``--effort`` CLI flag, so splice the model_args + the
                # effort pair through ``_set_cli_option`` so a future
                # ``model_args`` change here doesn't silently drop effort.
                host_d["command"] = _set_cli_option(
                    host_d["command"], "--model", "{model}"
                )
                host_d["command"] = _set_cli_option(
                    host_d["command"], "-c", "model_reasoning_effort={effort}"
                )
                agents["codex-host"] = host_d
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