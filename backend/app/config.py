"""Application configuration.

Secrets and deployment settings come from environment variables / .env
(prefix ``CD_``).  Agent command definitions and the cross-agent context
instruction live in a YAML file (``config.yaml``) so they can be tuned on the
server without touching code.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Files / serving ---
    agents_config_path: Path = Path("./config.yaml")
    frontend_dist: Path = Path("../frontend/dist")
    cors_origins: str = "*"  # comma-separated list, or "*"

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 8000

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
            command=["hermes", "chat", "-q", "{prompt}", "--yolo", "--accept-hooks"],
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


def load_agents_config(path: Path) -> AgentsConfig:
    if not path.exists():
        return AgentsConfig(agents=default_agents())

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
            # built-in, the extra elements (e.g. --use-auth-token) must be
            # APPENDED to the spec's command, not prepended (which would dup
            # the base CLI).  A full YAML command (same length) replaces outright.
            if (
                "command" in spec
                and isinstance(spec["command"], list)
                and len(merged["command"]) > len(spec["command"])
            ):
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
