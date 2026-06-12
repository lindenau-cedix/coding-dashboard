"""Session routes: start/end interactive agent sessions, WebSocket chat."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import get_agents_config
from ..database import get_db, session_scope
from ..models import Project, Task
from ..schemas import SessionCreate, SessionEndRequest, SessionStartResponse
from ..task_runner import session_manager

router = APIRouter(tags=["sessions"], dependencies=[Depends(get_current_user)])


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #

@router.post("/sessions", response_model=SessionStartResponse, status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreate, db: Session = Depends(get_db)) -> dict:
    """Create a new interactive session task and start the subprocess.

    A Task record is created immediately (status=queued then running), the
    subprocess is started, and the task_id is returned.  The client then
    connects to the session WebSocket to exchange messages.
    """
    project = db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")

    cfg = get_agents_config()
    spec = cfg.agents.get(body.agent)
    if spec is None or not spec.enabled:
        raise HTTPException(400, f"Unknown or disabled agent: {body.agent}")
    if body.model and body.model not in (spec.model_choices or []):
        raise HTTPException(400, f"Model '{body.model}' not supported by {spec.display_name}.")
    if body.effort and body.effort not in (spec.effort_choices or []):
        raise HTTPException(400, f"Effort '{body.effort}' not supported.")

    task = Task(
        project_id=body.project_id,
        agent=body.agent,
        prompt="",
        mode="session",
        model=body.model,
        effort=body.effort,
        is_session=True,
        status="queued",
        chat_history="[]",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    asyncio.create_task(
        session_manager.start(task.id, body.project_id, body.agent, body.model, body.effort)
    )
    with session_scope() as sdb:
        t = sdb.get(Task, task.id)
        if t:
            t.status = "running"
            t.started_at = datetime.now(timezone.utc)

    return {"task_id": task.id, "status": "running"}


@router.get("/sessions/{task_id}", response_model=dict)
async def get_session(task_id: str, db: Session = Depends(get_db)) -> dict:
    """Return session task metadata including current chat_history."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Session nicht gefunden.")
    if not task.is_session:
        raise HTTPException(400, "Task ist keine Session.")
    return {
        "id": task.id,
        "project_id": task.project_id,
        "agent": task.agent,
        "model": task.model,
        "effort": task.effort,
        "chat_history": json.loads(task.chat_history or "[]"),
        "status": task.status,
        "result_summary": task.result_summary,
        "output": task.output,
        "is_session": task.is_session,
    }


@router.post("/sessions/{task_id}/end")
async def end_session(task_id: str, body: SessionEndRequest, db: Session = Depends(get_db)) -> dict:
    """End the interactive session: stop subprocess, commit+push, persist chat_history."""
    with session_scope() as sdb:
        task = sdb.get(Task, task_id)
        if task is None:
            raise HTTPException(404, "Session nicht gefunden.")
        if not task.is_session:
            raise HTTPException(400, "Task ist keine Session.")
        project_id = task.project_id

    result = await session_manager.end_session(
        task_id, project_id, commit_message=body.commit_message
    )
    return result


# --------------------------------------------------------------------------- #
# Session WebSocket — bidirectional chat messages
# --------------------------------------------------------------------------- #

@router.websocket("/ws/sessions/{task_id}")
async def ws_session(websocket: WebSocket, task_id: str, token: str = Query(default="")) -> None:
    """WebSocket for an interactive session.

    Client sends: {type: "message"|"end", content?: string, commit_message?: string}
    Server sends: {type: "started"|"output"|"message"|"status"|"done"|"error", ...}
    """
    from ..auth import user_from_token

    if not user_from_token(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    ch = session_manager.get_channel(task_id)
    if ch is None:
        # Session ended or server restarted: replay state from DB.
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                await websocket.send_json({"type": "error", "message": "Session nicht gefunden"})
                await websocket.close()
                return
            chat = json.loads(task.chat_history or "[]")
            for msg in chat:
                await websocket.send_json(
                    {"type": "message", "role": msg.get("role"), "content": msg.get("content")}
                )
            await websocket.send_json({"type": "status", "status": task.status})
            await websocket.send_json({"type": "done", "task_id": task_id, "status": task.status})
        await websocket.close()
        return

    queue = ch.subscribe()
    send_task = asyncio.create_task(_ws_send(websocket, queue))
    receive_task = asyncio.create_task(_ws_receive(websocket, task_id))

    try:
        done, _ = await asyncio.wait([send_task, receive_task], return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        pass
    finally:
        ch.unsubscribe(queue)
        send_task.cancel()
        receive_task.cancel()


async def _ws_send(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Forward channel messages to the WebSocket client."""
    try:
        while True:
            msg = await queue.get()
            if msg.get("type") == "_eof":
                break
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


async def _ws_receive(websocket: WebSocket, task_id: str) -> None:
    """Receive messages from the client and forward to the session."""
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")
            if msg_type == "message":
                content = data.get("content", "")
                if content:
                    await session_manager.send_message(task_id, content)
            elif msg_type == "end":
                commit_msg = data.get("commit_message", "")
                asyncio.create_task(
                    session_manager.end_session(task_id, "", commit_message=commit_msg)
                )
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
