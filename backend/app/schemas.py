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
    """POST /sessions — start a new interactive session."""

    project_id: str
    agent: str
    model: str = ""
    effort: str = ""
    # Shell-like argv string. It is parsed with shlex.split and appended to the
    # configured session_command; no shell is invoked.
    start_args: str = Field(default="", max_length=1000)


class SessionStartResponse(BaseModel):
    """Response after creating a session task."""

    task_id: str
    status: str


class SessionEndRequest(BaseModel):
    """POST /sessions/{id}/end — user ends the interactive session."""

    commit_message: str = ""
