"""Task routes: submit work to an agent, list history, inspect results."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import uploads
from ..auth import get_current_user
from ..config import get_agents_config
from ..database import get_db
from ..models import EnvProfile, Project, Task
from ..schemas import AgentInfo, RunningTaskOut, TaskCreate, TaskDetail, TaskOut
from ..task_runner import manager

router = APIRouter(tags=["tasks"], dependencies=[Depends(get_current_user)])


@router.get("/running", response_model=list[RunningTaskOut])
def list_running(db: Session = Depends(get_db)) -> list[RunningTaskOut]:
    """All currently running/queued tasks, goals and sessions across every
    project — backs the live dashboard on the start page."""
    projects = {p.id: p for p in db.query(Project).all()}
    rows = (
        db.query(Task)
        .filter(Task.status.in_(["running", "queued"]))
        .order_by(Task.created_at.desc())
        .all()
    )
    out: list[RunningTaskOut] = []
    for t in rows:
        item = RunningTaskOut.model_validate(t)
        proj = projects.get(t.project_id)
        if proj:
            item.project_name = proj.name
            item.project_slug = proj.slug
        out.append(item)
    return out


@router.get("/agents", response_model=list[AgentInfo])
def list_agents() -> list[AgentInfo]:
    cfg = get_agents_config()
    # Pre-compute host-sibling map so each row knows whether the per-task
    # "Runner: host" toggle is actually selectable (the Docker entrypoint
    # auto-creates ``<agent>-host`` siblings when ``CD_<AGENT>_SSH_USER``
    # is set; systemd operators hand-write them in config.yaml).
    out: list[AgentInfo] = []
    for a in cfg.agents.values():
        host_key = f"{a.key}-host"
        host = cfg.agents.get(host_key)
        out.append(
            AgentInfo(
                key=a.key,
                display_name=a.display_name,
                enabled=a.enabled,
                supports_goal=bool(a.goal_command),
                supports_session=bool(a.session_command),
                model_choices=a.model_choices if a.model_args else [],
                effort_choices=a.effort_choices if a.effort_args else [],
                host_agent_key=(
                    host_key if (host is not None and host.enabled) else None
                ),
            )
        )
    return out


@router.get("/projects/{project_id}/tasks", response_model=list[TaskOut])
def list_tasks(project_id: str, db: Session = Depends(get_db)) -> list[Task]:
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    return (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .order_by(Task.created_at.desc())
        .all()
    )


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_task(
    project_id: str, body: TaskCreate, db: Session = Depends(get_db)
) -> Task:
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    cfg = get_agents_config()
    spec = cfg.agents.get(body.agent)
    if spec is None or not spec.enabled:
        raise HTTPException(400, f"Unbekannter oder deaktivierter Agent: {body.agent}")
    if body.mode == "goal" and not spec.goal_command:
        raise HTTPException(
            400, f"Agent {spec.display_name} unterstützt keinen Goal-Modus."
        )
    if body.model and body.model not in spec.model_choices:
        raise HTTPException(
            400, f"Agent {spec.display_name} unterstützt das Modell '{body.model}' nicht."
        )
    if body.effort and body.effort not in spec.effort_choices:
        raise HTTPException(
            400, f"Agent {spec.display_name} unterstützt Effort '{body.effort}' nicht."
        )
    # Per-task "host" runner: must have an enabled "<agent>-host" sibling.
    # This is the operator-facing guard for the toggle on the start form;
    # the runner also defensively checks at start time.
    if body.runner == "host":
        host_key = f"{body.agent}-host"
        host_spec = cfg.agents.get(host_key)
        if host_spec is None or not host_spec.enabled:
            raise HTTPException(
                400,
                f"Host-Runner fuer Agent '{body.agent}' nicht aktiviert. "
                f"Setze CD_{body.agent.upper()}_SSH_USER in der Env-Datei "
                f"und starte das Backend neu.",
            )
    # Env-profile must exist when set. ``EnvProfile.key`` is unique so we
    # look up directly — no SELECT roundtrip needed. An empty token in the
    # stored profile is still valid (operator may only want to redirect
    # the base URL).
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
    # Decode/validate the attachments BEFORE creating the task row so a bad
    # upload rejects the whole request without leaving artifacts behind.
    try:
        decoded_images = uploads.decode_images(body.images)
    except uploads.ImageError as exc:
        raise HTTPException(400, str(exc))

    task = Task(
        project_id=project_id,
        agent=body.agent,
        prompt=body.prompt,
        mode=body.mode,
        model=body.model,
        effort=body.effort,
        runner=body.runner,
        env_profile_key=body.env_profile_key,
        status="queued",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    if decoded_images:
        names = uploads.save_images(task.id, decoded_images)
        task.images = json.dumps(names)
        db.commit()
        db.refresh(task)
    manager.submit(task.id, project_id)
    return task


@router.get("/tasks/{task_id}", response_model=TaskDetail)
def get_task(task_id: str, db: Session = Depends(get_db)) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task nicht gefunden.")
    return task


@router.get("/tasks/{task_id}/images/{name}")
def get_task_image(task_id: str, name: str, db: Session = Depends(get_db)) -> FileResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task nicht gefunden.")
    # Only names recorded on the task are served — no path traversal possible.
    names = json.loads(task.images) if task.images else []
    if name not in names:
        raise HTTPException(404, "Bild nicht gefunden.")
    path = uploads.task_image_dir(task_id) / name
    if not path.exists():
        raise HTTPException(404, "Bilddatei nicht (mehr) vorhanden.")
    return FileResponse(path, media_type=uploads.media_type(name))


@router.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    if db.get(Task, task_id) is None:
        raise HTTPException(404, "Task nicht gefunden.")
    stopped = await manager.stop(task_id)
    return {"stopped": stopped}
