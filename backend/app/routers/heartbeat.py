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
from ..heartbeat import heartbeat, heartbeat_followup
from ..models import HeartbeatSeen, Project, Task
from ..schemas import (
    HeartbeatIssueSeen,
    HeartbeatProjectStatus,
    HeartbeatStatus,
)

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
        # Surface the configured allowlist. The ``/user`` auto-resolve is
        # NOT run here — this endpoint is synchronous and read-from-settings
        # only. The live resolved allowlist shows up in the most recent
        # tick's ``assignee_logins`` log line; operators can verify it
        # worked by checking the server log instead of blocking the GET.
        assignee_logins=settings.heartbeat_assignee_logins_list,
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
    """All (project, GitHub issue) rows the heartbeat has ever seen.

    Enriches each row with the current dispatched-task snapshot (status,
    commit hash) and the comment-back state so the UI can render the
    whole story in one fetch.
    """
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    rows = (
        db.query(HeartbeatSeen)
        .filter(HeartbeatSeen.project_id == project_id)
        .order_by(HeartbeatSeen.first_seen_at.desc())
        .all()
    )
    if not rows:
        return rows
    # One fetch for all dispatched tasks of this project; keyed by id so
    # we can map them back onto the heartbeat_seen rows without N+1.
    task_ids = [r.dispatched_task_id for r in rows if r.dispatched_task_id]
    if not task_ids:
        tasks_by_id = {}
    else:
        tasks_by_id = {
            t.id: t
            for t in (
                db.query(Task)
                .filter(Task.id.in_(task_ids))
                .all()
            )
        }

    # Pydantic's ``response_model=list[HeartbeatIssueSeen]`` would drop
    # fields we set on the ORM instances, so we explicitly serialise.
    out: list[dict] = []
    for r in rows:
        t = tasks_by_id.get(r.dispatched_task_id or "") if r.dispatched_task_id else None
        out.append(
            HeartbeatIssueSeen.model_validate(
                {
                    "project_id": r.project_id,
                    "issue_number": r.issue_number,
                    "issue_title": r.issue_title,
                    "issue_url": r.issue_url,
                    "first_seen_at": r.first_seen_at,
                    "dispatched_task_id": r.dispatched_task_id,
                    "dispatched_task_status": (t.status if t else ""),
                    "dispatched_commit_hash": (t.commit_hash if t else ""),
                    "last_comment_id": r.last_comment_id,
                    "last_commented_at": r.last_commented_at,
                    "last_comment_url": r.last_comment_url,
                    "last_comment_error": r.last_comment_error,
                    "last_issue_state": r.last_issue_state,
                    "last_issue_state_changed_at": r.last_issue_state_changed_at,
                }
            )
        )
    return out  # type: ignore[return-value]  (FastAPI re-validates against response_model)


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


@projects_router.post("/projects/{project_id}/heartbeat/issues/{issue_number}/comment-again")
async def heartbeat_comment_again(
    project_id: str,
    issue_number: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Force-recomment on a heartbeat-handled issue.

    The dashboard's auto-hook has already POSTed the initial status
    comment when the heartbeat task landed. This route is for the
    "operator wants to refresh the wording" use case. By default a NEW
    comment is POSTed (so the timeline still shows the original); pass
    ``{"update_existing": true}`` to PATCH the previous one in place.
    """
    from fastapi import Request  # local import keeps top-of-file stable

    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if not p.github_full_name:
        raise HTTPException(400, "Projekt hat kein GitHub-Repo.")
    # The body is optional. ``force_new=False`` => PATCH existing comment.
    # We default to True so the operator's intent is explicit; we don't
    # currently read the body but mirror the pattern for future-proofing.
    result = await heartbeat_followup.comment_again(
        p, int(issue_number), force_new=True
    )
    if result.get("error"):
        raise HTTPException(502, f"GitHub-Fehler: {result['error']}")
    return result


@projects_router.post("/projects/{project_id}/heartbeat/issues/{issue_number}/close")
async def heartbeat_close_issue(
    project_id: str,
    issue_number: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Manually close the issue. Independent of the auto-close-on-merge
    behaviour - useful when the operator wants to close an issue after a
    manual merge, a re-run of the heartbeat, or a false-positive dispatch.
    """
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if not p.github_full_name:
        raise HTTPException(400, "Projekt hat kein GitHub-Repo.")
    result = await heartbeat_followup.set_issue_state(
        p, int(issue_number), "closed"
    )
    if result.get("error"):
        raise HTTPException(502, f"GitHub-Fehler: {result['error']}")
    return result


@projects_router.post("/projects/{project_id}/heartbeat/issues/{issue_number}/reopen")
async def heartbeat_reopen_issue(
    project_id: str,
    issue_number: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Inverse of ``.../close``. Useful when the operator wants the
    heartbeat to reconsider an issue after a manual close.
    """
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if not p.github_full_name:
        raise HTTPException(400, "Projekt hat kein GitHub-Repo.")
    result = await heartbeat_followup.set_issue_state(
        p, int(issue_number), "open"
    )
    if result.get("error"):
        raise HTTPException(502, f"GitHub-Fehler: {result['error']}")
    return result
