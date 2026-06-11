"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


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


class TaskCreate(BaseModel):
    agent: str
    prompt: str = Field(min_length=1)
    mode: Literal["task", "goal"] = "task"
    # "" = use the agent's/CLI's default model resp. effort.
    model: str = ""
    effort: str = ""


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    agent: str
    prompt: str
    mode: str
    model: str = ""
    effort: str = ""
    status: str
    exit_code: Optional[int]
    result_summary: str
    error: str
    branch: str
    commit_hash: str
    commit_message: str
    commit_created: bool
    pushed: bool
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class TaskDetail(TaskOut):
    output: str
