"""Heartbeat HTTP endpoints.

This module exposes TWO routers:

- ``router``         : global heartbeat routes (``/api/heartbeat`` ...) mounted
                       at ``/api/heartbeat`` in ``main.py``.
- ``projects_router``: per-project heartbeat routes
                       (``/api/projects/{id}/heartbeat/...``) mounted at ``/api``.

Splitting them keeps the prefix clean (``/api/heartbeat`` reads naturally)
and avoids a name clash with ``/api/projects`` siblings.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import github_client
from ..auth import get_current_user
from ..config import get_agents_config, get_settings
from ..database import get_db
from ..heartbeat import heartbeat
from ..models import HeartbeatSeen, Project, Task
from ..schemas import HeartbeatIssueSeen, HeartbeatProjectStatus, HeartbeatStatus

log = logging.getLogger("coding-dashboard.heartbeat")


# Global routes — mounted at /api/heartbeat. The empty prefix here becomes
# ``/api/heartbeat`` in main.py.
router = APIRouter(prefix="/heartbeat", tags=["heartbeat"])


@router.get("", response_model=HeartbeatStatus)
def get_heartbeat_status(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> HeartbeatStatus:
    settings = get_settings()
    agents = get_agents_config()
    agent_key = settings.heartbeat_agent_key
    if agent_key not in agents.agents:
        agent_key = next(iter(agents.agents), "")

    projects = (
        db.query(Project)
        .filter(Project.archived.is_(False))
        .order_by(Project.name)
        .all()
    )
    statuses: list[HeartbeatProjectStatus] = []
    for p in projects:
        inflight = [
            t.id
            for t in (
                db.query(Task)
                .filter(Task.project_id == p.id)
                .filter(Task.heartbeat_spawned.is_(True))
                .filter(Task.status.in_(["queued", "running"]))
                .all()
            )
        ]
        statuses.append(
            HeartbeatProjectStatus(
                id=p.id,
                name=p.name,
                slug=p.slug,
                enabled=p.heartbeat_enabled,
                github_full_name=p.github_full_name,
                last_heartbeat_at=p.last_heartbeat_at,
                last_issue_poll_at=p.last_issue_poll_at,
                last_heartbeat_status=p.last_heartbeat_status,
                last_heartbeat_error=p.last_heartbeat_error,
                open_issues_count=0,
                inflight_task_ids=inflight,
            )
        )

    return HeartbeatStatus(
        enabled=heartbeat.enabled,
        interval_seconds=settings.heartbeat_interval_seconds,
        agent_key=agent_key,
        cooldown_minutes=settings.heartbeat_cooldown_minutes,
        projects=statuses,
    )


@router.post("/enable")
def enable_heartbeat(
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    heartbeat.set_enabled(True)
    return {"enabled": True}


@router.post("/disable")
def disable_heartbeat(
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    heartbeat.set_enabled(False)
    return {"enabled": False}


@router.post("/trigger")
async def trigger_heartbeat(
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Kick a tick now. Awaits completion so the HTTP response doubles as
    a 'done' signal (the tick takes a few seconds at most — projects are
    polled in parallel under a semaphore; an in-flight tick is rejected
    via the in-process ``_tick_lock`` so this endpoint is safe to call
    repeatedly)."""
    summary = await heartbeat.tick_now()
    return {"triggered": True, "summary": summary}


# Per-project routes — mounted at /api. Paths start with
# ``/projects/{id}/heartbeat/...`` so they don't clash with anything
# inside ``routers/projects.py``.
projects_router = APIRouter(tags=["heartbeat"])


@projects_router.post("/projects/{project_id}/heartbeat/enable")
def enable_project_heartbeat(
    project_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    p.heartbeat_enabled = True
    db.commit()
    return {"id": project_id, "heartbeat_enabled": True}


@projects_router.post("/projects/{project_id}/heartbeat/disable")
def disable_project_heartbeat(
    project_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    p.heartbeat_enabled = False
    db.commit()
    return {"id": project_id, "heartbeat_enabled": False}


@projects_router.get(
    "/projects/{project_id}/heartbeat/issues",
    response_model=list[HeartbeatIssueSeen],
)
def list_seen_issues(
    project_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> list[HeartbeatSeen]:
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    return (
        db.query(HeartbeatSeen)
        .filter(HeartbeatSeen.project_id == project_id)
        .order_by(HeartbeatSeen.first_seen_at.desc())
        .all()
    )


@projects_router.get("/projects/{project_id}/heartbeat/open")
async def list_open_issues(
    project_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Live open issues from GitHub (NOT from heartbeat_seen). Used by
    the ProjectDetail "GitHub Issues" expandable section."""
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if not p.github_full_name:
        return {"issues": [], "note": "no_github_full_name"}
    try:
        issues = await github_client.list_issues(p.github_full_name, state="open")
    except github_client.GitHubError as exc:
        raise HTTPException(502, f"GitHub-Fehler: {exc}") from exc

    real = [i for i in issues if not i.get("pull_request")]
    return {
        "issues": [
            {
                "number": i.get("number"),
                "title": i.get("title") or "",
                "html_url": i.get("html_url") or "",
                "user": (i.get("user") or {}).get("login") if i.get("user") else "",
                "labels": [
                    lbl.get("name")
                    for lbl in (i.get("labels") or [])
                    if isinstance(lbl, dict)
                ],
                "updated_at": i.get("updated_at"),
                "created_at": i.get("created_at"),
                "body": (i.get("body") or "")[:1500],
            }
            for i in real
        ]
    }