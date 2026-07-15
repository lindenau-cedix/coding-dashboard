"""Session routes: start/end interactive agent sessions, WebSocket chat."""
from __future__ import annotations

import asyncio
import json
import shlex
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import get_agents_config
from ..database import get_db, session_scope
from ..models import EnvProfile, Project, Task
from ..schemas import SessionCreate, SessionEndRequest, SessionStartResponse
from ..task_runner import session_manager

# NOTE: We deliberately do NOT use router-level ``dependencies=`` here, because
# the ``@router.websocket(...)`` endpoint below cannot satisfy ``HTTPBearer`` (it
# needs a real ``Request`` with a Bearer header, which WebSocket handshakes
# don't provide). The WebSocket does its own auth via ``user_from_token(token)``.
# Each HTTP route below declares ``Depends(get_current_user)`` explicitly.
router = APIRouter(tags=["sessions"])


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #

@router.post("/sessions", response_model=SessionStartResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> dict:
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
    if not spec.session_command:
        raise HTTPException(400, f"Agent {spec.display_name} unterstützt keinen Session-Modus.")
    if body.model and body.model not in (spec.model_choices or []):
        raise HTTPException(400, f"Model '{body.model}' not supported by {spec.display_name}.")
    if body.effort and body.effort not in (spec.effort_choices or []):
        raise HTTPException(400, f"Effort '{body.effort}' not supported.")
    # Per-session "host" runner guard — same semantics as ``create_task``.
    if body.runner == "host":
        # Strip an already-present ``-host`` suffix so a stale/explicit
        # ``claude-host`` selection combined with runner="host" does not
        # build ``claude-host-host`` (mirrors SessionManager.start, which
        # guards its shim with ``endswith("-host")``).
        base_agent = (
            body.agent[:-5] if body.agent.endswith("-host") else body.agent
        )
        host_key = f"{base_agent}-host"
        host_spec = cfg.agents.get(host_key)
        if host_spec is None or not host_spec.enabled or not host_spec.session_command:
            raise HTTPException(
                400,
                f"Host-Runner fuer Agent '{base_agent}' nicht aktiviert. "
                f"Setze CD_{base_agent.upper()}_SSH_USER in der Env-Datei "
                f"und starte das Backend neu.",
            )
    # Env-profile must exist when set; same semantics as ``create_task``.
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
    start_args = body.start_args.strip()
    try:
        shlex.split(start_args)
    except ValueError as exc:
        raise HTTPException(400, f"Startparameter können nicht geparst werden: {exc}")

    task = Task(
        project_id=body.project_id,
        agent=body.agent,
        prompt=start_args,
        mode="session",
        model=body.model,
        effort=body.effort,
        runner=body.runner,
        env_profile_key=body.env_profile_key,
        is_session=True,
        status="queued",
        chat_history="[]",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        started = await session_manager.start(
            task.id,
            body.project_id,
            body.agent,
            body.model,
            body.effort,
            start_args=start_args,
            runner=body.runner,
            env_profile_key=body.env_profile_key,
        )
    except ValueError as exc:
        with session_scope() as sdb:
            t = sdb.get(Task, task.id)
            if t:
                t.status = "error"
                t.error = str(exc)
                t.finished_at = datetime.now(timezone.utc)
        raise HTTPException(400, str(exc))
    if not started:
        raise HTTPException(500, "Session konnte nicht gestartet werden.")

    return {"task_id": task.id, "status": "running"}


@router.get("/sessions/{task_id}", response_model=dict)
async def get_session(
    task_id: str,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> dict:
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
        "start_args": task.prompt or "",
        "workdir": task.workdir or "",
        "chat_history": json.loads(task.chat_history or "[]"),
        "status": task.status,
        "result_summary": task.result_summary,
        "output": task.output,
        "is_session": task.is_session,
    }


@router.post("/sessions/{task_id}/end")
async def end_session(
    task_id: str,
    body: SessionEndRequest,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> dict:
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
async def ws_session(
    websocket: WebSocket,
    task_id: str,
    token: str = Query(default=""),
    offset: int = Query(default=0, ge=0),
) -> None:
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
        # A client can connect immediately after POST /sessions. Give the
        # just-created background session a short grace period to register its
        # live channel before falling back to persisted replay.
        for _ in range(40):
            with session_scope() as db:
                task = db.get(Task, task_id)
                task_status = task.status if task else ""
            if task_status not in {"queued", "running"}:
                break
            await asyncio.sleep(0.05)
            ch = session_manager.get_channel(task_id)
            if ch is not None:
                break
    if ch is None:
        # Session ended or server restarted: replay state from DB.
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                await websocket.send_json({"type": "error", "message": "Session nicht gefunden"})
                await websocket.close()
                return
            if task.output and offset < len(task.output):
                await websocket.send_json(
                    {"type": "output", "data": task.output[offset:], "offset": offset}
                )
            chat = json.loads(task.chat_history or "[]")
            for msg in chat:
                await websocket.send_json(
                    {"type": "message", "role": msg.get("role"), "content": msg.get("content")}
                )
            await websocket.send_json({"type": "status", "status": task.status})
            await websocket.send_json({"type": "done", "task_id": task_id, "status": task.status})
        await websocket.close()
        return

    queue = ch.subscribe(replay=False)
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task and task.output and offset < len(task.output):
            await websocket.send_json(
                {"type": "output", "data": task.output[offset:], "offset": offset}
            )
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
            elif msg_type == "resize":
                cols = int(data.get("cols") or 0)
                rows = int(data.get("rows") or 0)
                if cols > 0 and rows > 0:
                    await session_manager.resize(task_id, cols=cols, rows=rows)
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
