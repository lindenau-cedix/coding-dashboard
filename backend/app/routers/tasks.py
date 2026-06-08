"""Task routes: submit work to an agent, list history, inspect results."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import get_agents_config
from ..database import get_db
from ..models import Project, Task
from ..schemas import AgentInfo, TaskCreate, TaskDetail, TaskOut
from ..task_runner import manager

router = APIRouter(tags=["tasks"], dependencies=[Depends(get_current_user)])


@router.get("/agents", response_model=list[AgentInfo])
def list_agents() -> list[AgentInfo]:
    cfg = get_agents_config()
    return [
        AgentInfo(
            key=a.key,
            display_name=a.display_name,
            enabled=a.enabled,
            supports_goal=bool(a.goal_command),
        )
        for a in cfg.agents.values()
    ]


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

    task = Task(
        project_id=project_id,
        agent=body.agent,
        prompt=body.prompt,
        mode=body.mode,
        status="queued",
    )
    db.add(task)
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


@router.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    if db.get(Task, task_id) is None:
        raise HTTPException(404, "Task nicht gefunden.")
    stopped = await manager.stop(task_id)
    return {"stopped": stopped}
