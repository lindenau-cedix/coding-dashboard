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


# Default prompt scaffold used when the heartbeat auto-spawns a Claude Code
# task for a freshly-seen open GitHub issue. Override via
# ``CD_HEARTBEAT_PROMPT_TEMPLATE`` to swap languages or tweak the workflow.
# Placeholders are filled by ``heartbeat._build_prompt``: ``{number}``,
# ``{repo}``, ``{title}``, ``{user}``, ``{labels}``, ``{created_at}``,
# ``{body}``, ``{html_url}``.
DEFAULT_HEARTBEAT_PROMPT_TEMPLATE = """\
Du arbeitest automatisch im Auftrag des Coding Dashboards. Das Dashboard \
hat auf GitHub Issue #{number} im Repo {repo} aufmerksam gemacht:

**Titel:** {title}
**Autor:** {user}
**Labels:** {labels}
**Erstellt:** {created_at}

**Beschreibung:**
{body}

**URL:** {html_url}

So gehst du vor:

1. Lies das Repo, um den Code zu verstehen. clone / pull nur ueber das
   Dashboard, niemals eigenstaendig.
2. Reproduziere den Bug (oder implementiere die Anforderung). Wenn
   moeglich: schreibe zuerst einen Test, der fehlschlaegt.
3. Implementiere den Fix. Halte dich an die existierenden Patterns und
   Conventions im Repo.

WICHTIG -- was du NICHT tun darfst:

Du bist NUR fuer den Code zustaendig. Das Dashboard macht am Ende JEDEN
Commit, Push, PR und Merge SELBST. Fuehre deshalb folgende Befehle
UNTER KEINEN UMSTAENDEN selbst aus, auch nicht als Teil einer
"Convenience"-Operation:

- `git add` (egal mit welchen Pfaden / `-A`) -- das Dashboard staeted
  selbst.
- `git commit` (egal mit welcher Message / `-m` / `-a`) -- das
  Dashboard macht den Commit mit einer eigenen Message aus deiner
  Zusammenfassung.
- `git push` (egal welcher Branch / Remote) -- das Dashboard pusht auf
  den richtigen Branch.
- `git checkout -b`, `git switch -c`, `git branch <name>` -- das
  Dashboard legt den Branch `heartbeat/fix-{number}-<slug>` fuer dich
  an. Wechsle NICHT selbst auf einen anderen Branch; das Dashboard
  startet dich bereits auf einer frischen Worktree.
- `gh pr create`, `gh repo view`, `gh issue *`, `hub pull-request`
  oder jeder andere Aufruf von GitHub-CLIs -- das Dashboard oeffnet
  den PR selbst und postet die Commit-URL als Kommentar auf den Issue.
- Jedes andere VCS-Tool (`jj`, `svn`, ...) -- nicht verwendet.

Wenn du commit/push selbst machst, sieht das Dashboard hinterher eine
**saubere** Working Tree, denkt "nichts zu committen", ueberspringt den
Auto-Commit und pushed dann leere Diffs. Der Fix verschwindet und es
oeffnet sich kein PR. Das ist der haeufigste Fehler in dieser
Pipeline -- lass es bleiben.

Konkrekt: AENDERE NUR DATEIEN. Wenn du fertig bist, soll `git status`
uncommitted Aenderungen zeigen (das ist erwünscht -- das Dashboard
uebernimmt sie). Wenn du aus Versehen doch einen `git commit` gemacht
hast, mach `git reset --soft HEAD~1` (oder loesche den letzten Commit
gleichwertig), damit die Aenderungen wieder uncommitted im Index
liegen, BEVOR du zurueckmeldest.

4. Verifiziere deine Aenderung lokal (Lint, Tests, was das Repo
   bietet).
5. Schreibe am Ende einen kurzen Status (max 200 Woerter) mit: was du
   gefunden hast, welche Dateien du geaendert hast, ob Tests gruen
   sind, und ob du den Fix bewusst NICHT committest/pusht/PR-stellst
   (immer -- das macht das Dashboard).

Sonstiges:

- KEINE Rueckfragen an den User -- das Dashboard hat keine UI fuer
  Rueckfragen.
- KEINE destruktiven Operationen (force-push, rm -rf, drop tables,
  Branch-Loeschungen).
- KEIN eigenstaendiges Branch- oder Worktree-Management.
- Wenn der Issue unklar ist oder nicht reproduzierbar: dokumentiere das
  im Status; das Dashboard oeffnet den PR dann mit einem
  'investigation notes' Body und schliesst den Issue nicht.
"""


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

    # --- Heartbeat (auto-poll GitHub issues, auto-spawn Claude Code tasks) ---
    # ``CD_HEARTBEAT_ENABLED`` master switch. When False the heartbeat never
    # ticks; the UI toggle mirrors this in-process (not persisted to disk).
    heartbeat_enabled: bool = False
    # ``CD_HEARTBEAT_INTERVAL_SECONDS``: how often the heartbeat wakes up.
    # Default is 15 minutes; production should be >= 5 min to stay polite to
    # GitHub's rate limits.
    heartbeat_interval_seconds: int = 15 * 60
    # ``CD_HEARTBEAT_MAX_CONCURRENT``: cap on parallel project polls per tick.
    # A Semaphore gates _process_project coroutines so a tick doesn't fan out
    # dozens of GitHub requests simultaneously.
    heartbeat_max_concurrent: int = 2
    # ``CD_HEARTBEAT_COOLDOWN_MINUTES``: after a heartbeat-spawned task for a
    # project reaches ``success``, do NOT spawn another one for that project
    # for this many minutes. Only SUCCESS blocks; failed/error/cancelled runs
    # do not start the cooldown (so a misfiring agent gets another chance
    # next tick).
    heartbeat_cooldown_minutes: int = 30
    # ``CD_HEARTBEAT_AGENT_KEY``: which AgentSpec key to spawn. Default
    # "claude" — the goal explicitly says "Claude Code allways for
    # automatic runs". Operators can override (e.g. to "codex") via env.
    heartbeat_agent_key: str = "claude"
    # ``CD_HEARTBEAT_LOOKBACK_HOURS``: on first poll for a project, look at
    # issues updated within this many hours. Subsequent polls use the
    # project's last_issue_poll_at.
    heartbeat_lookback_hours: int = 24
    # ``CD_HEARTBEAT_PROMPT_TEMPLATE``: the prompt scaffold for auto-spawned
    # tasks. See DEFAULT_HEARTBEAT_PROMPT_TEMPLATE below for the defaults;
    # ops can override (e.g. to swap German for English) via env.
    heartbeat_prompt_template: str = DEFAULT_HEARTBEAT_PROMPT_TEMPLATE
    # ``CD_HEARTBEAT_LABELS``: comma-separated list of GitHub issue labels.
    # Empty (default) = poll every open issue. Non-empty = only dispatch on
    # issues that have AT LEAST ONE of these labels (e.g. "bug,good first
    # issue"). Wire-through; no UI yet.
    heartbeat_labels: str = ""
    # ``CD_HEARTBEAT_ASSIGNEE_LOGINS``: comma-separated list of GitHub
    # logins. Empty (default) = auto-resolve from the ``CD_GITHUB_TOKEN``
    # by calling ``/user`` at the start of each tick; the resolved login
    # becomes the implicit allowlist. Non-empty = explicit allowlist
    # (overrides auto-resolution). The heartbeat is RESTRICTED to issues
    # whose ``assignees`` array intersects this allowlist; if the value
    # remains empty after auto-resolution (no token, ``/user`` failure,
    # empty ``login``) the tick short-circuits to ``no_assignee`` rather
    # than falling back to "every open issue".
    heartbeat_assignee_logins: str = ""
    # ``CD_HEARTBEAT_ENV_PROFILE_KEY``: default env-profile for all
    # auto-spawned tasks. Empty (default) = today's behaviour: no env
    # injection for heartbeat-spawned runs (standard Anthropic auth +
    # endpoint the agent CLI defaults to). Settable per-project on the
    # /heartbeat page; the per-project override beats this global
    # default. Resolved at dispatch time (NOT persisted on
    # ``HeartbeatSeen``) so rotating the value applies to the next tick.
    heartbeat_env_profile_key: str = ""
    # ``CD_HEARTBEAT_COMMENT_ON_SUCCESS`` (default ``True``): when a
    # heartbeat-spawned task lands a commit, post a comment on the GitHub
    # issue with the commit hash + a short summary. Comments are idempotent
    # (``HeartbeatSeen.last_comment_id``) and best-effort; a failure to
    # comment does not roll back the task or the push.
    heartbeat_comment_on_success: bool = True
    # ``CD_HEARTBEAT_CLOSE_ON_MERGE`` (default ``True``): when a
    # heartbeat-spawned task LANDS its commit on the default branch
    # (``merge_state == "merged"`` AND ``pushed=True``), the dashboard
    # also closes the GitHub issue via ``PATCH /repos/.../issues/{n}``.
    # When the branch is kept (merge_state=="conflict") the issue is left
    # OPEN with the comment only — a human decides after the manual merge.
    heartbeat_close_on_merge: bool = True

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

    @property
    def heartbeat_labels_list(self) -> list[str]:
        return [o.strip() for o in self.heartbeat_labels.split(",") if o.strip()]

    @property
    def heartbeat_assignee_logins_list(self) -> list[str]:
        """CSV → list, trimmed, lowercased, de-duplicated (order preserved).

        GitHub logins are case-insensitive, so we lowercase for comparison.
        Mirrors ``heartbeat_labels_list`` semantics with the extra
        normalization step. Empty string in → empty list out.
        """
        seen: set[str] = set()
        out: list[str] = []
        for raw in self.heartbeat_assignee_logins.split(","):
            s = raw.strip().lower()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out


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
    # Shared-SSH-wiring backfill for the SSH-driven siblings. The first-boot
    # generator emits all three of {hermes-host,claude-host,codex-host}
    # whenever ANY CD_*_SSH_USER is set. If the on-disk YAML predates this
    # support (e.g. written before codex-host landed) one or more of those
    # keys is missing even though the operator's current SSH env vars would
    # create it today. The entrypoint also detects this and regenerates the
    # YAML on container start; this loader-side backfill covers the case
    # where the operator is mid-restart-loop or just wants the new sibling
    # without waiting for the next full restart. Only applied to
    # built-in-style configs — operator-curated custom agents are never
    # silently augmented with built-in -host siblings. Operator-set
    # ``enabled: false`` on a present -host key is preserved because the
    # backfill only ADDS missing keys.
    if _is_legacy_builtin_config_with_siblings(agents_raw, builtin):
        try:
            from .config_bootstrap import generate_initial_agents_config
            generated = generate_initial_agents_config()
        except Exception:
            generated = None
        if generated is not None:
            for host_key in ("hermes-host", "claude-host", "codex-host"):
                if host_key in generated["agents"] and host_key not in agents:
                    spec_dict = dict(generated["agents"][host_key])
                    spec_dict["key"] = host_key
                    agents[host_key] = AgentSpec(**spec_dict)
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


def _is_legacy_builtin_config_with_siblings(
    agents_raw: dict[str, object], builtin: dict[str, AgentSpec]
) -> bool:
    """Like :func:`_is_legacy_builtin_config` but also accepts ``-host``
    siblings (built-in SSH-driven variants generated by the first-boot
    helper, not part of the base spec). Used to gate the SSH-sibling
    backfill: a YAML with the original base built-ins plus possibly some
    ``-host`` siblings from a previous upgrade is still safe to augment
    with the missing ``-host`` siblings the generator would emit today.
    Custom (non-builtin, non-host) keys keep the YAML explicit, so
    operator-curated agent sets are never silently mutated.
    """
    configured = set(agents_raw)
    builtin_set = set(builtin)
    allowed = builtin_set | {f"{k}-host" for k in builtin_set}
    return (
        {"claude", "hermes"}.issubset(configured)
        and configured.issubset(allowed)
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_agents_config() -> AgentsConfig:
    return load_agents_config(get_settings().agents_config_path)
