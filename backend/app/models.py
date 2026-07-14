"""SQLAlchemy ORM models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """``TIMESTAMP`` column that always round-trips through tz-aware UTC.

    SQLite has no native tz support, so SQLAlchemy drops the tzinfo on read
    by default. That makes naive ``datetime`` values reach the API and get
    serialised as ISO strings without an offset — the JS frontend then
    parses them as **local time**, producing exactly the browser-tz offset
    bug we saw on the Heartbeat page (Europe/Berlin ⇒ −2h displayed).

    This decorator forces every value to come back as a tz-aware UTC
    datetime, so the API always emits a correct ``...Z`` suffix.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    github_full_name: Mapped[str] = mapped_column(String(255), default="")
    github_url: Mapped[str] = mapped_column(String(512), default="")
    clone_url: Mapped[str] = mapped_column(String(512), default="")
    local_path: Mapped[str] = mapped_column(String(1024), default="")
    default_branch: Mapped[str] = mapped_column(String(128), default="main")

    # Archived projects are hidden from the default project list. The repo
    # stays on disk, history stays in the DB, tasks can still be inspected
    # by id - the only effect of archiving is keeping the start page focused
    # on the user's currently-active work.
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    # Per-project heartbeat opt-out (default ON). The user can flip this off
    # on the /heartbeat page or via POST /api/projects/{id}/heartbeat/disable.
    heartbeat_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Last time the heartbeat even LOOKED at this project (regardless of
    # whether it dispatched a task). UI shows "vor 12 Min".
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    # Last time the heartbeat successfully fetched open issues from GitHub.
    # Drives the ``since`` parameter on subsequent polls (incremental fetch).
    last_issue_poll_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    # Short status string for the /heartbeat UI:
    # "" (never ticked) | "success" | "skipped" | "cooldown" |
    # "error" | "no_github" | "no_issues".
    last_heartbeat_status: Mapped[str] = mapped_column(String(32), default="")
    # On error: human-readable one-liner for the UI / logs.
    last_heartbeat_error: Mapped[str] = mapped_column(Text, default="")

    # Per-project override: which env profile (``env_profiles.key``) the
    # heartbeat should inject into auto-spawned tasks for THIS project.
    # Empty (default) = fall through to the global
    # ``settings.heartbeat_env_profile_key`` (env ``CD_HEARTBEAT_ENV_PROFILE_KEY``).
    heartbeat_env_profile_key: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_now, onupdate=_now
    )

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Task.created_at.desc()",
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    agent: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    # "task" (one-off prompt) | "goal" (agent works until the goal is reached)
    mode: Mapped[str] = mapped_column(String(16), default="task")
    # Optional per-task model/effort selection ("" = agent/CLI default).
    model: Mapped[str] = mapped_column(String(128), default="")
    effort: Mapped[str] = mapped_column(String(32), default="")
    # JSON list of attached image filenames (stored under data_dir/task_images/{id}/).
    images: Mapped[str] = mapped_column(Text, default="")
    # Session mode: the task is an interactive session (vs one-off task/goal).
    is_session: Mapped[bool] = mapped_column(Boolean, default=False)
    # Session mode: full chat history as JSON list of {role, content, timestamp}.
    # Updated live after each user turn; final state becomes Task.output on end.
    chat_history: Mapped[str] = mapped_column(Text, default="")
    # Session mode: the directory the agent actually ran in. For a normal session
    # this is the project's local_path; for a parallel session it is an isolated
    # git worktree. Resuming a session re-uses the matching directory so the
    # agent CLI finds its saved conversation (it keys sessions by cwd).
    workdir: Mapped[str] = mapped_column(String(1024), default="")

    # queued | running | success | failed | error | interrupted | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str] = mapped_column(Text, default="")
    result_summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    branch: Mapped[str] = mapped_column(String(128), default="")
    # Result of merging this task's branch back into the default branch:
    # "" (n/a) | "merged" (landed on default) | "conflict" (branch kept for manual merge).
    merge_state: Mapped[str] = mapped_column(String(32), default="")
    commit_hash: Mapped[str] = mapped_column(String(64), default="")
    commit_message: Mapped[str] = mapped_column(Text, default="")
    commit_created: Mapped[bool] = mapped_column(Boolean, default=False)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Heartbeat marker: True when the task was auto-spawned by the heartbeat
    # loop (vs hand-created by the user). Drives the "🤖 Auto-Fix" badge in
    # the task history and the heartbeat's "recent tasks" overview.
    heartbeat_spawned: Mapped[bool] = mapped_column(Boolean, default=False)
    # The GitHub issue number that triggered this task (NULL for hand tasks).
    heartbeat_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stamp when the dashboard successfully POSTed a status comment for
    # this task's commit back on the issue. UI displays the relative age;
    # ``HeartbeatSeen.last_comment_id`` is the source of truth (a single
    # heartbeat-spawned task can only comment once unless the operator
    # hits "Re-comment").
    heartbeat_commented_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    # Stamp when the dashboard successfully closed the GitHub issue
    # after a clean merge. NULL until the close call succeeds.
    heartbeat_closed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    # Env-profile the operator picked on the start form. Empty (default)
    # = no env injection (the agent uses its own baked-in auth / endpoint).
    # The runner resolves this key against ``env_profiles`` at start time
    # and overlays ``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN`` + the
    # explicit empty-string ``ANTHROPIC_API_KEY`` so the host's shell
    # cannot leak an inherited upstream token.
    env_profile_key: Mapped[str] = mapped_column(String(64), default="")
    # Where the agent ran: "" (default = in-container, today's behaviour)
    # or "host" (per-task opt-in to SSH-into-host for Claude Code,
    # mirroring the Hermes-over-SSH model). When "host" the runner picks
    # the ``<agent>-host`` sibling AgentSpec; if missing it returns a
    # clear operator-friendly 400.
    runner: Mapped[str] = mapped_column(String(16), default="")

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_now, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="tasks")


class HeartbeatSeen(Base):
    """Dedup ledger: one row per (project, GitHub issue) the heartbeat has
    already considered. Insert here first (idempotent INSERT OR IGNORE);
    the heartbeat only spawns a task when the row was ACTUALLY new. This is
    a separate table so an issue that's been around for months doesn't get
    re-dispatched every poll — only newly-opened issues trigger a spawn.
    """

    __tablename__ = "heartbeat_seen"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Issue title at first sight (cached for the /heartbeat UI list).
    issue_title: Mapped[str] = mapped_column(String(512), default="")
    issue_url: Mapped[str] = mapped_column(String(512), default="")
    first_seen_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_now
    )
    # Which task was dispatched for this issue, if any. NULL if the issue
    # was filtered out (e.g. heartbeat was off, or labels didn't match).
    dispatched_task_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    # ---- comment-back-on-issue state ----------------------------------- #
    # One commit-hash worth of "the dashboard already POSTed a comment on
    # this issue" tracking. Best-effort: any failure to write to the
    # issue comment endpoint is captured in ``last_comment_error`` and
    # does NOT roll back the task or the push. ``last_comment_id`` is
    # the GitHub-side comment id (so the "Re-comment" UI can overwrite
    # via PATCH instead of stacking a new comment).
    last_comment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_commented_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    last_comment_error: Mapped[str] = mapped_column(Text, default="")
    last_comment_url: Mapped[str] = mapped_column(String(512), default="")
    # ``"open"`` / ``"closed"``: last known issue state after the
    # dashboard's close-on-merge behavior or manual close/reopen calls.
    last_issue_state: Mapped[str] = mapped_column(String(16), default="")
    last_issue_state_changed_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )

    project: Mapped["Project"] = relationship()


class EnvProfile(Base):
    """Named env-var profile that the operator can attach to a task/goal/
    session or to a project's heartbeat.

    Currently exposes the two variables Claude Code actually understands
    for endpoint redirection (``ANTHROPIC_BASE_URL`` and
    ``ANTHROPIC_AUTH_TOKEN``); the schema is trivially extensible to
    further columns when more variables become necessary.  The
    ``anthropic_auth_token_encrypted`` column stores a Fernet token
    derived from ``Settings.secret_key`` — the plaintext never lands on
    disk, and the GET response never echoes it back (only a hint like
    ``sk-…12``).
    """

    __tablename__ = "env_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Stable identifier the API + UI reference (slug, lowercase).
    # Unique so the runner can resolve ``Task.env_profile_key`` directly.
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Human label for the UI dropdowns. Editable.
    name: Mapped[str] = mapped_column(String(200))
    # Empty = "use whatever the agent's baked-in defaults are" (today:
    # Anthropic's upstream endpoint). Non-empty redirects via
    # ``ANTHROPIC_BASE_URL`` env on the spawned subprocess.
    anthropic_base_url: Mapped[str] = mapped_column(String(512), default="")
    # Fernet ciphertext. Empty = no token injected. Never returned to the
    # client — the GET response shows ``anthropic_auth_token_set: bool``
    # + an anonymised hint prefix/suffix instead.
    anthropic_auth_token_encrypted: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_now, onupdate=_now
    )
