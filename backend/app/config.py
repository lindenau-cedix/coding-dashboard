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

    ``command`` is a list of argv tokens (no shell).  The tokens ``{prompt}``
    and ``{project_dir}`` are substituted at run time.  If ``prompt_via`` is
    ``"stdin"`` the prompt is written to the process stdin instead of being
    substituted into argv.
    """

    key: str
    display_name: str
    command: list[str]
    prompt_via: Literal["arg", "stdin"] = "arg"
    stream_format: Literal["claude-json", "raw", "lines"] = "raw"
    # If set, this agent supports "goal mode": instead of a one-off task prompt
    # the user states a goal and the agent works until it is reached.  The
    # template wraps the user's goal text before it is sent (``{prompt}`` is the
    # goal).  ``None`` => the agent has no goal mode.
    goal_command: Optional[str] = None
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
   Stand sowie offene Punkte / Next Steps -- so, dass ein anderer KI-Agent (Claude Code oder
   Hermes) das Projekt sofort versteht und nahtlos weiterarbeiten kann.
4. Committe oder pushe NICHT selbst -- das uebernimmt das Dashboard automatisch nach dem Task.
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
    agents: dict[str, AgentSpec] = {}
    for key, spec in agents_raw.items():
        spec = dict(spec or {})
        spec["key"] = key
        spec.setdefault("display_name", key.capitalize())
        agents[key] = AgentSpec(**spec)
    if not agents:
        agents = default_agents()
    return AgentsConfig(
        agents=agents,
        context_instruction=data.get("context_instruction") or DEFAULT_CONTEXT_INSTRUCTION,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_agents_config() -> AgentsConfig:
    return load_agents_config(get_settings().agents_config_path)
