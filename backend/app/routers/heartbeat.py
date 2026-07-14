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
from ..models import EnvProfile, HeartbeatSeen, Project, Task
from ..schemas import (
    HeartbeatAgentKeyIn,
    HeartbeatEnvProfileIn,
    HeartbeatIssueSeen,
    HeartbeatProjectStatus,
    HeartbeatStatus,
    ProjectHeartbeatEnvProfileIn,
    ProjectOut,
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
    # Effective agent key: in-memory override (set via POST
    # /api/heartbeat/agent-key) wins; falls back to the env var.
    agent_key = heartbeat.agent_key or settings.heartbeat_agent_key
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
                heartbeat_env_profile_key=p.heartbeat_env_profile_key,
                open_issues_count=0,
                inflight_task_ids=inflight,
            )
        )

    # Compute the agent keys the operator can switch to. Includes the
    # configured default plus every enabled "<key>-host" sibling so the
    # heartbeat can flip between container / host without an env edit.
    available_agent_keys: list[str] = []
    base_default = settings.heartbeat_agent_key
    if base_default in agents.agents and agents.agents[base_default].enabled:
        available_agent_keys.append(base_default)
    for key, spec in agents.agents.items():
        if (
            key.endswith("-host")
            and spec.enabled
            and key not in available_agent_keys
        ):
            available_agent_keys.append(key)

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
        # Global env-profile default: in-memory override (set via POST
        # /api/heartbeat/env-profile) wins; falls back to the env var.
        # Empty = standard Anthropic auth / endpoint.
        env_profile_key=heartbeat.env_profile_key
        or settings.heartbeat_env_profile_key,
        available_agent_keys=available_agent_keys,
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
    repeatedly).

    The manual trigger intentionally bypasses the per-project success
    cooldown (``bypass_cooldown=True``) so the operator's "▶ Run now"
    button actually does work after a recent successful auto-fix instead
    of silently no-op'ing on every project. The background loop still
    passes the default (cooldown enforced), so automatic re-dispatches
    remain throttled.
    """
    summary = await heartbeat.tick_now(bypass_cooldown=True)
    return {"triggered": True, "summary": summary}


@router.post("/env-profile")
def set_heartbeat_env_profile(
    body: HeartbeatEnvProfileIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Set or clear the GLOBAL heartbeat env-profile default.

    Empty string clears the global default (no env injection; per-project
    overrides on the projects table still win when set). Non-empty must
    reference an existing ``env_profiles.key``. Effective at the NEXT
    heartbeat tick (and applies to manual triggers too — the resolver
    is read at dispatch time, not at HTTP time).

    In-memory only: the change resets on backend restart, same as the
    global enable toggle and the agent-key selector. Operators wanting a
    permanent switch set ``CD_HEARTBEAT_ENV_PROFILE_KEY`` in the service
    config and restart.
    """
    key = body.env_profile_key
    if key:
        exists = (
            db.query(EnvProfile.id)
            .filter(EnvProfile.key == key)
            .first()
        )
        if exists is None:
            raise HTTPException(
                404, f"Env-Profil '{key}' nicht gefunden."
            )
    heartbeat.set_env_profile_key(key)
    return {"env_profile_key": heartbeat.env_profile_key}


@router.post("/agent-key")
def set_heartbeat_agent_key(
    body: HeartbeatAgentKeyIn,
    user: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Swap the heartbeat's auto-spawned agent at runtime.

    Default ``CD_HEARTBEAT_AGENT_KEY`` (env var) wins at startup; the
    operator can override at runtime via this endpoint to flip between
    ``claude`` (in-container) and ``claude-host`` (SSH-into-host) without
    editing env vars and restarting. The override resets on backend
    restart (operators wanting a permanent switch set the env var).

    The key must exist in ``agents.agents`` AND be enabled — otherwise
    the route 400s with an operator message. Empty string falls back to
    the env-var default (clear the runtime override).
    """
    key = body.agent_key.strip()
    if key:
        agents = get_agents_config()
        spec = agents.agents.get(key)
        if spec is None or not spec.enabled:
            raise HTTPException(
                400,
                f"Agent '{key}' ist nicht aktiviert. Verfügbar: "
                + ", ".join(sorted(agents.agents))
                or "(keine)",
            )
    heartbeat.set_agent_key(key)
    effective = heartbeat.agent_key or get_settings().heartbeat_agent_key
    return {"agent_key": effective}


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


@projects_router.post(
    "/projects/{project_id}/heartbeat/env-profile",
    response_model=ProjectOut,
)
def set_project_heartbeat_env_profile(
    project_id: str,
    body: ProjectHeartbeatEnvProfileIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[str, Depends(get_current_user)],
) -> Project:
    """Set or clear this project's heartbeat env-profile override.

    Empty string clears the override (falls back to ``CD_HEARTBEAT_ENV_PROFILE_KEY``
    or to "no env injection" when that is empty too). Non-empty must reference
    an existing ``env_profiles.key``. Effective at the NEXT heartbeat tick;
    the currently-running tick is not modified.
    """
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if body.env_profile_key:
        exists = (
            db.query(EnvProfile.id)
            .filter(EnvProfile.key == body.env_profile_key)
            .first()
        )
        if exists is None:
            raise HTTPException(
                404,
                f"Env-Profil '{body.env_profile_key}' nicht gefunden.",
            )
    p.heartbeat_env_profile_key = body.env_profile_key
    db.commit()
    db.refresh(p)
    return p


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
