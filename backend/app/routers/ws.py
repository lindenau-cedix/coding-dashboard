"""WebSocket endpoint streaming a task's live output."""
from __future__ import annotations

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..auth import user_from_token
from ..database import session_scope
from ..models import Task
from ..task_runner import manager, task_to_dict

router = APIRouter()


@router.websocket("/ws/tasks/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str, token: str = Query(default="")) -> None:
    if not user_from_token(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    channel = manager.get_channel(task_id)
    if channel is None:
        # No live channel (already finished / server restarted): replay from DB.
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                await websocket.send_json({"type": "error", "message": "Task nicht gefunden"})
                await websocket.close()
                return
            data = task_to_dict(task)
            output = task.output
        if output:
            await websocket.send_json({"type": "output", "data": output})
        await websocket.send_json({"type": "status", "status": data["status"]})
        await websocket.send_json({"type": "done", "task": data})
        await websocket.close()
        return

    queue = channel.subscribe()
    try:
        while True:
            msg = await queue.get()
            if msg.get("type") == "_eof":
                break
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    finally:
        channel.unsubscribe(queue)
    try:
        await websocket.close()
    except Exception:
        pass
