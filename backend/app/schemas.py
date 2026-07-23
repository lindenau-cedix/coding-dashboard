"""Pydantic request/response schemas."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class AgentInfo(BaseModel):
    key: str
    display_name: str
    enabled: bool
    supports_goal: bool = False
    supports_session: bool = False
    # Selectable models/effort levels ([] = no selector in the UI).
    model_choices: list[str] = []
    effort_choices: list[str] = []
    # Name of the "<base>-host" sibling AgentSpec, if one exists AND is
    # enabled. Lets the UI hide the Runner dropdown for agents whose
    # host-over-SSH sibling is not registered (e.g. a clean install with
    # no `CD_CLAUDE_SSH_USER`).
    host_agent_key: Optional[str] = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    mode: Literal["create", "import"] = "create"
    private: bool = True
    # For import: "owner/repo" or a full clone/html URL.
    repo: str = ""


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    description: str
    github_full_name: str
    github_url: str
    default_branch: str
    # Archived projects are hidden from the default list; UI uses this to
    # show an "Archiviert" badge and render the card with reduced opacity.
    archived: bool = False
    archived_at: Optional[datetime] = None
    # Heartbeat fields: read-only mirror of the dashboard-side auto-poll
    # state. UI uses these to render the "🤖 Heartbeat" chip on each card.
    heartbeat_enabled: bool = True
    last_heartbeat_at: Optional[datetime] = None
    last_heartbeat_status: str = ""
    last_heartbeat_error: str = ""
    # Per-project env-profile override for the heartbeat. Empty = the
    # global ``CD_HEARTBEAT_ENV_PROFILE_KEY`` applies (or no env injection
    # when both are empty). Settable per project from the /heartbeat page.
    heartbeat_env_profile_key: str = ""
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectOut):
    local_path: str
    clone_url: str


class TaskImagePayload(BaseModel):
    """One uploaded image: original filename + base64 content (or data-URL)."""

    name: str = ""
    data: str = Field(min_length=1)


class TaskCreate(BaseModel):
    agent: str
    prompt: str = Field(min_length=1)
    mode: Literal["task", "goal"] = "task"
    # "" = use the agent's/CLI's default model resp. effort.
    model: str = ""
    effort: str = ""
    # Optional image attachments; stored server-side and handed to the agent
    # as local file paths appended to the prompt.
    images: list[TaskImagePayload] = Field(default_factory=list)
    # "" = run inside the container (default), "host" = SSH into the host's
    # agent CLI via the "<agent>-host" sibling AgentSpec. Requires that
    # sibling to exist and be enabled; the route 400s with an operator
    # message when it doesn't.
    runner: Literal["", "host"] = ""
    # Optional env-profile: resolved against env_profiles at run time and
    # overlaid onto the spawned subprocess as ANTHROPIC_BASE_URL +
    # ANTHROPIC_AUTH_TOKEN + explicit ANTHROPIC_API_KEY="" so a host
    # shell cannot leak an inherited upstream key.
    env_profile_key: str = ""


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    agent: str
    prompt: str
    mode: str
    model: str = ""
    effort: str = ""
    # Filenames of the attached images (DB stores them as a JSON string).
    images: list[str] = []
    is_session: bool = False
    chat_history: list[SessionMessage] = []
    status: str
    exit_code: Optional[int]
    result_summary: str
    error: str
    branch: str
    merge_state: str = ""
    commit_hash: str
    commit_message: str
    commit_created: bool
    pushed: bool
    # Heartbeat marker: True for tasks auto-spawned by the dashboard
    # heartbeat (vs hand-created by the user). Drives the "🤖 Auto-Fix"
    # badge in the task history and the heartbeat overview.
    heartbeat_spawned: bool = False
    heartbeat_issue_number: Optional[int] = None
    # Set by ``HeartbeatFollowup`` once the dashboard successfully POSTs
    # the status comment on the GitHub issue. NULL until then.
    heartbeat_commented_at: Optional[datetime] = None
    # Set by ``HeartbeatFollowup`` after the close-on-merge PATCH
    # succeeds. NULL when the issue is left open (e.g. branch kept
    # for a manual merge).
    heartbeat_closed_at: Optional[datetime] = None
    # Per-task runner selection: "" (default = in container) or "host"
    # (per-task opt-in to SSH-into-host for Claude Code, mirroring the
    # Hermes-over-SSH model). UI shows a 🖥 host chip when set.
    runner: str = ""
    # Per-task env-profile key resolved at run time against
    # ``env_profiles``; ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN are
    # injected into the spawned subprocess. UI shows a 🔑 profile chip
    # when set.
    env_profile_key: str = ""
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    @field_validator("images", mode="before")
    @classmethod
    def _images_from_json(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else []
        return v or []

    @field_validator("chat_history", mode="before")
    @classmethod
    def _chat_history_from_json(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else []
        return v or []


class TaskDetail(TaskOut):
    output: str


class RunningTaskOut(TaskOut):
    """A running/queued task enriched with its project's name/slug for the
    cross-project dashboard on the start page."""

    project_name: str = ""
    project_slug: str = ""


# --------------------------------------------------------------------------- #
# File browser
# --------------------------------------------------------------------------- #

class FileEntry(BaseModel):
    name: str
    path: str  # POSIX path relative to the project root
    is_dir: bool
    size: int = 0


class DirListing(BaseModel):
    path: str  # the listed directory, relative to the project root ("" = root)
    entries: list[FileEntry]


class FileContent(BaseModel):
    path: str
    size: int
    is_binary: bool
    truncated: bool = False
    content: str = ""


# --------------------------------------------------------------------------- #
# Session mode
# --------------------------------------------------------------------------- #

class SessionMessage(BaseModel):
    """One turn in the chat history."""

    role: Literal["user", "assistant"]
    content: str
    timestamp: str  # ISO-8601


class SessionCreate(BaseModel):
    """POST /sessions — start a new interactive session.

    A plain session (``mode == "session"``) starts a bare TUI the user types
    into. An INTERACTIVE Task/Goal (``mode`` is ``"task"``/``"goal"`` with a
    non-empty ``initial_prompt`` — the "Interaktiv" checkbox on the start form)
    is the same PTY session, but the dashboard auto-types ``initial_prompt``
    into the TUI once and then hands control to the user (answer questions,
    interrupt, follow-up prompts). ``mode == "goal"`` wraps the prompt in the
    agent's ``goal_command``.
    """

    project_id: str
    agent: str
    model: str = ""
    effort: str = ""
    # "session" = plain TUI; "task"/"goal" = interactive Task/Goal (needs
    # ``initial_prompt``; "goal" additionally requires the agent's goal_command).
    mode: Literal["task", "goal", "session"] = "session"
    # The prompt auto-typed into the TUI on start (interactive Task/Goal only).
    initial_prompt: str = Field(default="", max_length=20000)
    # Shell-like argv string. It is parsed with shlex.split and appended to the
    # configured session_command; no shell is invoked. Not used together with
    # ``initial_prompt`` (interactive sessions start fresh, not via --resume).
    start_args: str = Field(default="", max_length=1000)
    # Same semantics as ``TaskCreate.runner`` / ``TaskCreate.env_profile_key``
    # — sessions can also be SSH'd into the host and/or run with an
    # env-profile overlay.
    runner: Literal["", "host"] = ""
    env_profile_key: str = ""


class SessionStartResponse(BaseModel):
    """Response after creating a session task."""

    task_id: str
    status: str


class SessionEndRequest(BaseModel):
    """POST /sessions/{id}/end — user ends the interactive session."""

    commit_message: str = ""


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #

class HeartbeatProjectStatus(BaseModel):
    """Per-project heartbeat snapshot for the /heartbeat UI."""

    id: str
    name: str
    slug: str
    enabled: bool
    github_full_name: str
    last_heartbeat_at: Optional[datetime] = None
    last_issue_poll_at: Optional[datetime] = None
    last_heartbeat_status: str = ""
    last_heartbeat_error: str = ""
    # Per-project override for the heartbeat's env-profile. Empty = the
    # global ``CD_HEARTBEAT_ENV_PROFILE_KEY`` applies. Settable from the
    # /heartbeat page; effective at the next dispatch.
    heartbeat_env_profile_key: str = ""
    # Number of open issues GitHub reports for this repo right now. Refreshed
    # only when the heartbeat polls; not real-time.
    open_issues_count: int = 0
    # Tasks (running/queued) currently spawned by the heartbeat for this project.
    inflight_task_ids: list[str] = []


class HeartbeatStatus(BaseModel):
    """Overall heartbeat state — backs the /heartbeat page header + toggles."""

    enabled: bool
    interval_seconds: int
    agent_key: str
    cooldown_minutes: int
    # Resolved GitHub logins the heartbeat will dispatch on, sourced from
    # ``CD_HEARTBEAT_ASSIGNEE_LOGINS`` (CSV) or auto-resolved from the
    # ``CD_GITHUB_TOKEN`` at tick time. Surfaced here so the operator can
    # see the live allowlist — and notice when it's empty (in which case
    # every tick short-circuits to ``no_assignee``).
    assignee_logins: list[str] = []
    # Effective global env-profile for auto-spawned tasks. Mirrors
    # ``Settings.heartbeat_env_profile_key`` (``CD_HEARTBEAT_ENV_PROFILE_KEY``).
    # Empty = no env injection (default). Non-empty = every auto-spawned
    # task runs that profile unless a per-project override is set.
    env_profile_key: str = ""
    # Agent keys the operator can pick the heartbeat's auto-spawned agent
    # from. Includes the configured default (CD_HEARTBEAT_AGENT_KEY) and
    # every ``<key>-host`` sibling with ``enabled=True`` (the Docker
    # entrypoint auto-creates ``claude-host`` when CD_CLAUDE_SSH_USER is
    # set; systemd operators hand-write it in config.yaml). Mirrors the
    # same selector the start form uses so the heartbeat can be flipped
    # between container / host without a backend restart.
    available_agent_keys: list[str] = []
    last_tick_at: Optional[datetime] = None
    last_tick_summary: Optional[str] = None
    projects: list[HeartbeatProjectStatus] = []


class HeartbeatIssueSeen(BaseModel):
    """One row from the heartbeat_seen ledger, used by the per-project
    drill-down on the /heartbeat page."""

    project_id: str
    issue_number: int
    issue_title: str
    issue_url: str
    first_seen_at: datetime
    dispatched_task_id: Optional[str] = None
    # Live status of the dispatched task so the UI can render
    # "✅ merged in abc12345" without a second fetch.
    dispatched_task_status: str = ""
    dispatched_commit_hash: str = ""
    # Comment-back state: set when the dashboard has successfully POSTed
    # a status comment onto the GitHub issue. ``last_comment_id`` is the
    # GitHub-side comment id (used by the "Re-comment" route to PATCH
    # the existing comment in-place instead of stacking a second one).
    last_comment_id: Optional[int] = None
    last_commented_at: Optional[datetime] = None
    last_comment_url: str = ""
    last_comment_error: str = ""
    # Last known GitHub issue state ("open"/"closed") the dashboard has
    # touched. Updated by close-on-merge and the manual close/reopen
    # routes.
    last_issue_state: str = ""
    last_issue_state_changed_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Env profiles (per-task / per-project ANTHROPIC_* env injection)
# --------------------------------------------------------------------------- #

# Slug rule: lowercase + digits + hyphens. Mirrors the dash-url convention
# used elsewhere in the project. Reserved ``default`` (case-insensitive).
_ENV_PROFILE_KEY_RE = r"^[a-z0-9][a-z0-9-]{0,62}$"


class EnvProfileIn(BaseModel):
    """Payload for ``POST /api/env-profiles`` and
    ``PATCH /api/env-profiles/{key}``.

    The ``anthropic_auth_token`` field is **write-only** at the API
    surface — the GET response replaces it with a boolean + anonymised
    hint so operators can rotate (PATCH with a new token) but cannot
    recover plaintext once submitted.
    """

    key: str = Field(pattern=_ENV_PROFILE_KEY_RE)
    name: str = Field(min_length=1, max_length=200)
    # Optional http(s) URL. Empty string = "use agent's baked-in endpoint".
    anthropic_base_url: str = ""
    # Plaintext token the operator just pasted. Stored encrypted on disk;
    # the response shaper (model_config above) never echoes it.
    anthropic_auth_token: str = ""


class EnvProfileOut(BaseModel):
    """Response shape for the env-profiles CRUD. Never carries plaintext."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    anthropic_base_url: str = ""
    # True iff a Fernet-encrypted token is currently stored. Operators can
    # rotate by PATCH with a new token; deletion of the token itself is
    # done by PATCH with empty string.
    anthropic_auth_token_set: bool = False
    # Anonymised hint of the plaintext (e.g. ``sk-…12``) so the operator
    # can see "this is the right key" in the UI without leaking it.
    anthropic_auth_token_hint: str = ""
    created_at: datetime
    updated_at: datetime


class ProjectHeartbeatEnvProfileIn(BaseModel):
    """Body for ``POST /api/projects/{id}/heartbeat/env-profile``.

    Empty string clears the per-project override (falls back to the
    global ``CD_HEARTBEAT_ENV_PROFILE_KEY`` or to no env injection when
    that is empty too)."""

    env_profile_key: str = ""


class HeartbeatEnvProfileIn(BaseModel):
    """Body for ``POST /api/heartbeat/env-profile`` — global default.

    Empty string clears the global default (no env injection; per-project
    overrides still win when set). Non-empty must reference an existing
    ``env_profiles.key``. In-memory only: resets on backend restart,
    same as the global enable toggle and the agent-key selector."""

    env_profile_key: str = ""


class HeartbeatAgentKeyIn(BaseModel):
    """Body for ``POST /api/heartbeat/agent-key`` — swap the auto-spawned agent.

    Lets the operator flip the heartbeat between ``claude`` (default,
    in-container) and ``claude-host`` (SSH-into-host) without editing
    env vars and restarting the backend. The key must exist in
    ``agents.agents`` and be enabled — otherwise the route 400s. The
    choice is in-memory only; resets on backend restart (operators
    wanting a permanent switch set ``CD_HEARTBEAT_AGENT_KEY`` in the
    service config)."""

    agent_key: str
