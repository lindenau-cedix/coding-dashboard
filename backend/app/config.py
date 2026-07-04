"""Application configuration.

Secrets and deployment settings come from environment variables / .env
(prefix ``CD_``).  Agent command definitions and the cross-agent context
instruction live in a YAML file (``config.yaml``) so they can be tuned on the
server without touching code.
"""
from __future__ import annotations

import os
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Hermes toolsets allowed in NON-INTERACTIVE task / goal runs.
#
# In one-shot `hermes chat -q {prompt} --yolo --accept-hooks` the dashboard streams
# stdout to a browser tab with no way to type back, so the `clarify` toolset would
# call into a None platform callback and either stall the run or bounce back with
# "Clarify tool is not available in this execution context."  Pass `-t <csv>` so
# the model never even sees `clarify` as an option.  Interactive TUI sessions
# (`session_command` = `hermes chat`) intentionally keep the full default toolset
# — the user can answer there.
HERMES_NON_INTERACTIVE_TOOLSETS = (
    "web,browser,terminal,file_search,read_file,write_file,"
    "edit_file,multi_edit,plan,session_search,kanban,image_gen,"
    "computer_use,video_gen,tts,spotify,delegate_task,todo,cronjob"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CD_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Coding Dashboard"

    # --- Security ---
    secret_key: str = "CHANGE-ME-please-generate-a-real-secret"
    access_token_expire_minutes: int = 60 * 24 * 7  # one week
    admin_username: str = "admin"
    # bcrypt-free pbkdf2 hash produced by `python -m app.cli hash-password`.
    admin_password_hash: str = ""
    # Whether API auth (login + Bearer token) is enforced. ``None`` (default)
    # auto-derives it: auth is ON only when an ``admin_password_hash`` is set,
    # so a fresh install with no password runs WITHOUT auth -- intended for
    # deployments fronted by an authenticating proxy (e.g. a Cloudflare tunnel /
    # Access). Set ``CD_REQUIRE_AUTH=true`` to force it on, or ``false`` to force
    # it off even when a hash is present.
    require_auth: Optional[bool] = None

    # --- GitHub ---
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    # Org/user under which new repos are created. Empty => the authenticated user.
    github_owner: str = ""

    # --- Storage ---
    data_dir: Path = Path("./data")
    database_url: str = ""  # empty => sqlite under data_dir

    # --- Git identity used for automatic commits ---
    git_author_name: str = "Coding Dashboard"
    git_author_email: str = "coding-dashboard@localhost"
    default_branch: str = "main"

    # --- Host staging (for agents with ``host_staging`` set, e.g. the SSH-driven
    # Hermes in the Docker deployment) ---
    # When an agent runs on a DIFFERENT machine than the dashboard (the container
    # drives the host's Hermes over SSH), it cannot see the repos that live in the
    # dashboard's data dir.  Such agents instead run inside a throwaway COPY of the
    # project placed under this directory, which is bind-mounted into the container
    # at an IDENTICAL path on both sides so ``cd {project_dir}`` resolves to the
    # same files for the container (copies in / integrates back) and the host
    # (Hermes edits).  Default lives under /tmp and is cleaned per run.
    hermes_staging_dir: Path = Path("/tmp/coding-dashboard-hermes")

    # --- Host-visible lock dir (one file per active task/goal/session) ---
    # Path the dashboard writes ``<kind>-<id>.lock`` files into while a task,
    # goal or session is running.  In Docker this MUST be a bind-mount that
    # reaches the host (otherwise the lock files are only visible inside the
    # container's private volumes); systemd installs naturally run on the
    # host, so any path the service user can write to works.  Default lives
    # under the conventional /var/lock so it survives reboots in the usual way;
    # Docker users bind-mount CD_HOST_LOCK_HOST_DIR (host path) at the SAME
    # path inside the container (``/var/lock/coding-dashboard``) so file
    # creation from the container hits the host.  The files are best-effort
    # visibility only — a failure to write never aborts a run.
    host_lock_dir: Path = Path("/var/lock/coding-dashboard")

    # --- Files / serving ---
    agents_config_path: Path = Path("./config.yaml")
    frontend_dist: Path = Path("../frontend/dist")
    cors_origins: str = "*"  # comma-separated list, or "*"

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def auth_enabled(self) -> bool:
        """Auth is enforced only when explicitly required or a password is set."""
        if self.require_auth is not None:
            return self.require_auth
        return bool(self.admin_password_hash)

    @property
    def projects_dir(self) -> Path:
        return (self.data_dir / "projects").resolve()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'dashboard.db').resolve()}"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# --------------------------------------------------------------------------- #
# Agent configuration (loaded from config.yaml)
# --------------------------------------------------------------------------- #
class AgentSpec(BaseModel):
    """How to invoke one coding agent CLI.

    ``command`` is a list of argv tokens (no shell).  The tokens ``{prompt}``,
    ``{project_dir}`` and ``{last_message_file}`` are substituted at run time
    (the latter becomes a temp file the CLI writes its FINAL message to, e.g.
    codex's ``--output-last-message``; its content is used as the task's
    result summary).  If ``prompt_via`` is ``"stdin"`` the prompt is written to
    the process stdin instead of being substituted into argv.
    """

    key: str
    display_name: str
    command: list[str]
    prompt_via: Literal["arg", "stdin"] = "arg"
    stream_format: Literal["claude-json", "codex", "raw", "lines"] = "raw"
    # If set, this agent supports "goal mode": instead of a one-off task prompt
    # the user states a goal and the agent works until it is reached.  The
    # template wraps the user's goal text before it is sent (``{prompt}`` is the
    # goal).  ``None`` => the agent has no goal mode.
    goal_command: Optional[str] = None
    # If set, this agent supports interactive session mode: the command is
    # invoked in a PTY so the agent runs in its interactive TUI. No prompt is
    # injected -- optional user supplied start parameters are appended as argv.
    session_command: Optional[list[str]] = None
    # Optional model/effort selection. ``*_choices`` is what the UI offers (an
    # empty list hides the selector); ``*_args`` are the argv tokens injected
    # when the user picked a value ("{model}"/"{effort}" are substituted).  The
    # tokens are inserted before a trailing "-" (stdin marker), else appended,
    # so explicit ``command`` lists in config.yaml keep working unchanged.
    model_choices: list[str] = Field(default_factory=list)
    model_args: list[str] = Field(default_factory=list)
    effort_choices: list[str] = Field(default_factory=list)
    effort_args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Environment variables to REMOVE before spawning (e.g. PYTHONPATH/PYTHONHOME
    # that would leak the backend's venv into a Python-based agent CLI).
    unset_env: list[str] = Field(default_factory=list)
    cwd: str = "{project_dir}"
    timeout_seconds: Optional[int] = None
    enabled: bool = True
    # If set, this agent does NOT run where the dashboard's repos live (it runs on
    # another machine, e.g. the host's Hermes driven over SSH).  The dashboard then
    # runs it inside a throwaway COPY of the project under ``settings.hermes_staging_dir``
    # (bind-mounted at an identical path host<->container): the project is copied
    # in, the agent edits it remotely, and its commit is merged back into the
    # canonical repo + pushed afterwards (conflicts are left on a branch for a
    # manual merge).  ``{project_dir}`` then resolves to that staging copy.  Set by
    # the Docker entrypoint on the Hermes agent when SSH mode is active; ``False``
    # everywhere else (local Hermes / systemd installs are unaffected).
    host_staging: bool = False


DEFAULT_CONTEXT_INSTRUCTION = """\
Wichtiger Projekt-Kontext (immer beachten):
1. Lies zuerst die Datei `AGENTS.md` im Projekt-Wurzelverzeichnis, falls vorhanden,
   um Struktur, Tech-Stack, bisherige Entscheidungen und den aktuellen Stand zu verstehen.
2. Erledige anschliessend die oben beschriebene Aufgabe vollstaendig und sauber.
3. Aktualisiere danach `AGENTS.md` (lege sie an, falls nicht vorhanden): beschreibe knapp
   und aktuell die Projektstruktur, den Tech-Stack, getroffene Entscheidungen, den aktuellen
   Stand sowie offene Punkte / Next Steps -- so, dass ein anderer KI-Agent (Claude Code,
   Hermes oder Codex) das Projekt sofort versteht und nahtlos weiterarbeiten kann.
4. Committe oder pushe NICHT selbst -- das uebernimmt das Dashboard automatisch nach dem Task.
5. **Pflege den "Letzter Durchlauf"-Block GANZ AM ANFANG der AGENTS.md** (direkt nach dem
   Titel und dem Zweck-Absatz, noch vor allen anderen Abschnitten): Überschreibe ihn bei
   jedem Durchlauf mit einer kurzen, fuer Menschen lesbaren Zusammenfassung dessen, was du
   in diesem Lauf getan hast -- was die Aufgabe war, was du gefunden/gebaut/geantwortet hast,
   und was die wichtigste Aenderung oder Erkenntnis war. Dieser Block wird vom Dashboard
   NICHT mehr geschrieben; nur das Dashboard entfernt noch alte "Letzte Tasks"-Bloecke
   (von Dashboards vor Version 2026-06-12), falls solche noch in der Datei existieren.
6. **Stelle KEINE Rueckfragen an den User.** Du laeufst nicht-interaktiv: das Dashboard
   streamt deine Ausgabe nur in den Browser, es gibt keine Moeglichkeit zu antworten.
   Wenn etwas mehrdeutig ist oder du mehr Informationen brauchst, triff eine
   vernuenftige Annahme (dokumentiere sie kurz in AGENTS.md / im Latest-Run-Block)
   und mach weiter. Nur in einem offenen TUI-Session-Modus (`hermes chat`, `claude`,
   `codex` ohne `-q`) hat der User eine Tastatur -- dort sind Rueckfragen erlaubt.
"""


def default_agents() -> dict[str, AgentSpec]:
    """Built-in defaults used when config.yaml is missing or has no agents."""
    return {
        "claude": AgentSpec(
            key="claude",
            display_name="Claude Code",
            command=[
                "claude",
                "-p",
                "{prompt}",
                "--output-format",
                "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
            ],
            prompt_via="arg",
            stream_format="claude-json",
            goal_command="/goal {prompt}",
            session_command=["claude"],
            model_choices=["opus", "sonnet", "haiku", "fable"],
            model_args=["--model", "{model}"],
            effort_choices=["low", "medium", "high", "xhigh", "max"],
            effort_args=["--effort", "{effort}"],
        ),
        "hermes": AgentSpec(
            key="hermes",
            display_name="Hermes",
            # `chat -q`: single non-interactive query that STREAMS intermediate
            # steps (tool previews) live. --yolo bypasses approvals, --accept-hooks
            # runs headless. AGENTS.md is auto-injected from the CWD.
            # `-t <csv>` restricts the toolset to non-interactive ones (excludes
            # `clarify`, which would call into a None platform callback in this
            # one-shot mode and stall the run or bounce back with
            # "Clarify tool is not available in this execution context.").
            # See HERMES_NON_INTERACTIVE_TOOLSETS for the rationale + list.
            command=[
                "hermes",
                "chat",
                "-q",
                "{prompt}",
                "--yolo",
                "--accept-hooks",
                "-t",
                HERMES_NON_INTERACTIVE_TOOLSETS,
            ],
            prompt_via="arg",
            stream_format="raw",
            env={"HERMES_ACCEPT_HOOKS": "1", "NO_COLOR": "1"},
            unset_env=["PYTHONPATH", "PYTHONHOME"],
            session_command=["hermes", "chat"],
        ),
        "codex": AgentSpec(
            key="codex",
            display_name="Codex",
            # `codex exec` is non-interactive. Reading the prompt from stdin keeps
            # long dashboard prompts out of argv while `-` tells Codex to consume
            # stdin as the initial instructions. --output-last-message writes the
            # agent's FINAL message to a temp file the runner reads back as the
            # task's result summary (exact, instead of tail-of-transcript).
            command=[
                "codex",
                "exec",
                "--cd",
                "{project_dir}",
                "--sandbox",
                "workspace-write",
                "--color",
                "never",
                "--ephemeral",
                "--output-last-message",
                "{last_message_file}",
                "-",
            ],
            prompt_via="stdin",
            stream_format="codex",
            env={"NO_COLOR": "1"},
            unset_env=["PYTHONPATH", "PYTHONHOME"],
            session_command=["codex"],
            model_choices=[
                "gpt-5.4",
                "gpt-5.5",
                "gpt-5.4-mini",
            ],
            model_args=["--model", "{model}"],
            effort_choices=["low", "medium", "high", "xhigh"],
            effort_args=["-c", "model_reasoning_effort={effort}"],
        ),
    }


class AgentsConfig(BaseModel):
    agents: dict[str, AgentSpec]
    context_instruction: str = DEFAULT_CONTEXT_INSTRUCTION


_HERMES_REMOTE_PATH_EXPORT = (
    'export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.cargo/bin:'
    '$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"'
)


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


def _ensure_hermes_remote_path(remote: str) -> str:
    """Make SSH-driven Hermes find ~/.local/bin/hermes in non-login shells."""
    if "hermes" not in remote or "export PATH=" in remote:
        return remote
    for prefix in ('cd "{project_dir}" && ', "cd {project_dir} && "):
        if remote.startswith(prefix):
            rest = remote[len(prefix) :]
            return f'cd "{{project_dir}}" && {_HERMES_REMOTE_PATH_EXPORT} && {rest}'
    return remote


def _normalize_hermes_ssh_argv(command: list[str]) -> list[str]:
    out = list(command)
    for idx, tok in enumerate(out):
        if "{project_dir}" in tok and "hermes" in tok:
            out[idx] = _ensure_hermes_remote_path(tok)
    return out


def _apply_runtime_agent_overrides(agents: dict[str, AgentSpec]) -> dict[str, AgentSpec]:
    """Apply deployment-level command fixes to both fresh and persisted configs."""
    codex_sandbox = (os.environ.get("CD_CODEX_SANDBOX") or "").strip()
    for key, spec in agents.items():
        if key == "codex" and codex_sandbox:
            spec.command = _set_cli_option(spec.command, "--sandbox", codex_sandbox)
        if key == "hermes" and spec.host_staging:
            spec.command = _normalize_hermes_ssh_argv(spec.command)
            if spec.session_command:
                spec.session_command = _normalize_hermes_ssh_argv(spec.session_command)
    return agents


def _splice_flags_into_hermes_remote(
    command: list[str], flags: list[str]
) -> list[str] | None:
    """Insert ``flags`` into the SSH remote-shell string at the end of ``command``.

    The Docker / SSH-driven Hermes agent has its last token as a single-quoted shell
    string passed to ``ssh user@host '<remote-cmd>'`` (e.g.
    ``cd "{project_dir}" && export PATH=... && exec env ... hermes chat -q "$(cat)" --yolo --accept-hooks``).
    When the built-in command grows (e.g. we add ``-t <csv>`` to disable ``clarify``)
    a naive "append the missing tail as separate argv" would leak those flags into
    ssh's argv and break the remote call.  Instead we splice them into the remote
    string right after ``--accept-hooks``, so the host's Hermes sees them as its
    own CLI flags.

    Returns the new command list, or ``None`` if ``command`` is not in the expected
    ``ssh ... <remote-shell-string>`` shape.
    """
    if not command or not flags:
        return None
    last = command[-1]
    if not isinstance(last, str) or "--accept-hooks" not in last:
        return None
    quoted = " ".join(shlex.quote(f) for f in flags)
    if " --accept-hooks" in last:
        spliced = last.replace(
            " --accept-hooks", f" --accept-hooks {quoted}", 1
        )
    elif last.endswith("--accept-hooks"):
        spliced = f"{last} {quoted}"
    else:
        return None
    return list(command[:-1]) + [spliced]


def _backfill_hermes_flags(spec: dict) -> None:
    """Apply Hermes-specific command backfill (mutates ``spec["command"]`` in place).

    Two cases:

    * **SSH-driven Hermes** (last argv token is a remote-shell string containing
      ``--accept-hooks``): the host's Hermes CLI runs INSIDE that string.  The
      new built-in added ``-t <csv>`` to disable ``clarify``; that flag must be
      spliced INTO the remote string (right after ``--accept-hooks``) so the
      host actually receives it.  Appending it as separate ssh argv would leak
      it into the ssh command line and break the remote call.
    * **Local / flat-argv Hermes** (a normal argv list): mirror the generic
      "append the missing tail" backfill so any newly-added built-in flags
      (e.g. ``--yolo``, ``--accept-hooks`` from a much older installer config,
      or the new ``-t <csv>``) are picked up on the next restart.

    Idempotent: running this twice does not duplicate flags.  Skipped entirely
    when the spec's command is already as long as the built-in (the user has
    fully overridden the command).
    """
    command = spec.get("command")
    if not isinstance(command, list) or not command:
        return
    builtin_cmd = default_agents()["hermes"].command
    if len(command) >= len(builtin_cmd):
        return  # user has overridden or matched the built-in
    # Already has -t with our toolset list?  -> no-op (idempotency).
    # We have to look BOTH at the top-level argv AND inside the SSH remote
    # string (where -t may have been spliced on a previous run).
    def _has_csv() -> bool:
        # Top-level argv check
        for i, tok in enumerate(command):
            if tok == "-t" and i + 1 < len(command):
                if command[i + 1] == HERMES_NON_INTERACTIVE_TOOLSETS:
                    return True
                # User pinned a different toolset list; respect it (don't overwrite).
                return True
        # SSH remote string check: look for " -t <csv>" anywhere in any token.
        for tok in command:
            if (
                isinstance(tok, str)
                and f" -t {HERMES_NON_INTERACTIVE_TOOLSETS}" in tok
            ):
                return True
        return False

    if _has_csv():
        return
    # SSH-driven case: the last token is a remote-shell string.  Splice the
    # new ``-t <csv>`` pair in right after ``--accept-hooks`` so the host's
    # Hermes CLI sees it as its own flag.
    last = command[-1]
    if isinstance(last, str) and "--accept-hooks" in last:
        spliced = _splice_flags_into_hermes_remote(
            command, ["-t", HERMES_NON_INTERACTIVE_TOOLSETS]
        )
        if spliced is not None:
            spec["command"] = spliced
            return
    # Flat-argv case: append the missing tail (legacy installer config that
    # had only `["hermes", "chat", "-q", "{prompt}"]` etc.).
    spec["command"] = command + builtin_cmd[len(command) :]


def load_agents_config(path: Path) -> AgentsConfig:
    if not path.exists():
        return AgentsConfig(agents=_apply_runtime_agent_overrides(default_agents()))

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agents_raw = data.get("agents") or {}
    builtin = default_agents()
    agents: dict[str, AgentSpec] = {}
    for key, spec in agents_raw.items():
        spec = dict(spec or {})
        # For built-in agents, backfill fields the config.yaml does not set from
        # the built-in defaults. This keeps existing (installer-generated) configs
        # forward-compatible: new optional fields like ``goal_command`` light up
        # on the next restart without requiring users to hand-edit config.yaml.
        if key in builtin:
            merged = builtin[key].model_dump()
            # command is a list: if the YAML spec has a shorter command than the
            # built-in, the extra elements (e.g. --use-auth-token, or the new
            # Hermes -t <csv>) must be added.  For most agents, appending them
            # is the right move: they land at the end of a flat argv list.
            # Special case: Hermes (handled by _backfill_hermes_flags) needs to
            # splice new flags into the SSH remote-shell string when present,
            # so they reach the host's Hermes CLI instead of leaking into ssh's
            # argv.
            if (
                "command" in spec
                and isinstance(spec["command"], list)
                and len(merged["command"]) > len(spec["command"])
            ):
                if key == "hermes":
                    _backfill_hermes_flags(spec)
                else:
                    spec["command"] = spec["command"] + merged["command"][len(spec["command"]) :]
            merged.update(spec)
            spec = merged
        spec["key"] = key
        spec.setdefault("display_name", key.capitalize())
        agents[key] = AgentSpec(**spec)
    if not agents:
        agents = default_agents()
    elif _is_legacy_builtin_config(agents_raw, builtin):
        # Existing installer-generated configs contain the original built-ins
        # (claude/hermes) and update.sh intentionally preserves that file. Append
        # newly introduced built-ins, while configs with custom agents remain
        # explicit and are not mutated.
        for key, spec in builtin.items():
            agents.setdefault(key, spec)
    agents = _apply_runtime_agent_overrides(agents)
    return AgentsConfig(
        agents=agents,
        context_instruction=data.get("context_instruction") or DEFAULT_CONTEXT_INSTRUCTION,
    )


def _is_legacy_builtin_config(
    agents_raw: dict[str, object], builtin: dict[str, AgentSpec]
) -> bool:
    configured = set(agents_raw)
    return (
        {"claude", "hermes"}.issubset(configured)
        and configured.issubset(set(builtin))
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_agents_config() -> AgentsConfig:
    return load_agents_config(get_settings().agents_config_path)
