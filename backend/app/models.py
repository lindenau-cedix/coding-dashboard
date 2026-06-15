"""SQLAlchemy ORM models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
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
    commit_hash: Mapped[str] = mapped_column(String(64), default="")
    commit_message: Mapped[str] = mapped_column(Text, default="")
    commit_created: Mapped[bool] = mapped_column(Boolean, default=False)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="tasks")
