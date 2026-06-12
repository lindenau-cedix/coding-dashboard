"""Task orchestration: run an agent, stream output, auto-commit & push.

One asyncio task per submitted job; a per-project lock serialises jobs that
touch the same git repo while still allowing different projects to run in
parallel.  Live output is delivered through an in-memory pub/sub channel that
the WebSocket endpoint subscribes to (with full replay for late joiners).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import traceback
from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from . import git_ops, uploads
from .agents import run_agent
from .config import get_agents_config, get_settings
from .database import session_scope
from .models import Project, Task
from .schemas import TaskOut


def _now() -> datetime:
    return datetime.now(timezone.utc)


def task_to_dict(task: Task) -> dict:
    return TaskOut.model_validate(task).model_dump(mode="json")


def build_agent_prompt(
    spec,
    prompt: str,
    mode: str,
    context_instruction: str,
    image_paths: Sequence[str] = (),
) -> str:
    """Compose the text handed to the agent CLI.

    In goal mode the user's goal is wrapped with the agent's ``goal_command``
    template (e.g. Claude's ``/goal {prompt}``) so the agent works until the
    goal is reached; otherwise the prompt is used as-is.  Attached images are
    referenced as local file paths the agent opens with its own read tool
    (they live outside the repo, so the auto-commit never picks them up).
    The shared context instruction (AGENTS.md upkeep, no self-commit) is
    appended either way.
    """
    if mode == "goal" and getattr(spec, "goal_command", None):
        base = spec.goal_command.replace("{prompt}", prompt)
    else:
        base = prompt
    if image_paths:
        listing = "\n".join(f"- {p}" for p in image_paths)
        base += (
            "\n\nAngehängte Bilder (lokale Dateien — öffne sie mit deinem"
            " Datei-/Bild-Lese-Tool und beziehe ihren Inhalt in die Aufgabe ein;"
            " sie liegen außerhalb des Repos und dürfen nicht hineinkopiert"
            " werden):\n" + listing
        )
    return f"{base}\n\n---\n{context_instruction}"


class TaskChannel:
    """In-memory pub/sub buffer for one task's live output."""

    MAX_BUFFER = 8000

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.buffer: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.closed = False

    def publish(self, msg: dict) -> None:
        self.buffer.append(msg)
        if len(self.buffer) > self.MAX_BUFFER:
            del self.buffer[: len(self.buffer) - self.MAX_BUFFER]
        for q in list(self.subscribers):
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for msg in self.buffer:
            q.put_nowait(msg)
        if self.closed:
            q.put_nowait({"type": "_eof"})
        else:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def close(self) -> None:
        self.closed = True
        for q in list(self.subscribers):
            q.put_nowait({"type": "_eof"})
        self.subscribers.clear()


class TaskManager:
    def __init__(self, max_channels: int = 100) -> None:
        self._channels: "OrderedDict[str, TaskChannel]" = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._max_channels = max_channels

    def _project_lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        return lock

    def _ensure_channel(self, task_id: str) -> TaskChannel:
        ch = self._channels.get(task_id)
        if ch is None:
            ch = TaskChannel(task_id)
            self._channels[task_id] = ch
        self._channels.move_to_end(task_id)
        while len(self._channels) > self._max_channels:
            old_id, _ = next(iter(self._channels.items()))
            if old_id in self._running:
                break
            self._channels.popitem(last=False)
        return ch

    def get_channel(self, task_id: str) -> TaskChannel | None:
        return self._channels.get(task_id)

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    def submit(self, task_id: str, project_id: str) -> None:
        ch = self._ensure_channel(task_id)
        ch.publish({"type": "status", "status": "queued"})
        t = asyncio.create_task(self._run(task_id, project_id, ch))
        self._running[task_id] = t
        t.add_done_callback(lambda _t: self._running.pop(task_id, None))

    async def stop(self, task_id: str) -> bool:
        t = self._running.get(task_id)
        if t is None:
            return False
        t.cancel()
        return True

    async def _run(self, task_id: str, project_id: str, ch: TaskChannel) -> None:
        try:
            await self._run_inner(task_id, project_id, ch)
        except asyncio.CancelledError:
            self._mark(task_id, status="cancelled", error="Abgebrochen.", finished=True)
            ch.publish({"type": "output", "data": "\n[abgebrochen]\n"})
            ch.publish({"type": "status", "status": "cancelled"})
            self._publish_done(ch, task_id)
            raise
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            self._mark(task_id, status="error", error=f"{exc}\n{tb}", finished=True)
            ch.publish({"type": "output", "data": f"\n[interner Fehler] {exc}\n"})
            ch.publish({"type": "status", "status": "error"})
            self._publish_done(ch, task_id)
        finally:
            ch.close()

    async def _run_inner(self, task_id: str, project_id: str, ch: TaskChannel) -> None:
        settings = get_settings()
        agents = get_agents_config()

        with session_scope() as db:
            task = db.get(Task, task_id)
            project = db.get(Project, project_id)
            if task is None or project is None:
                raise RuntimeError("Task oder Projekt nicht gefunden")
            agent_key = task.agent
            prompt = task.prompt
            mode = task.mode
            model = task.model
            effort = task.effort
            image_names = json.loads(task.images) if task.images else []
            project_dir = project.local_path
            branch = project.default_branch or settings.default_branch
            task.branch = branch
            db.commit()

        spec = agents.agents.get(agent_key)
        if spec is None or not spec.enabled:
            self._mark(
                task_id,
                status="error",
                error=f"Unbekannter oder deaktivierter Agent: {agent_key}",
                finished=True,
            )
            ch.publish({"type": "output", "data": f"[Fehler] Unbekannter Agent: {agent_key}\n"})
            ch.publish({"type": "status", "status": "error"})
            self._publish_done(ch, task_id)
            return

        lock = self._project_lock(project_id)
        if lock.locked():
            ch.publish(
                {"type": "output", "data": "[warten] Ein anderer Task fuer dieses Projekt laeuft noch...\n"}
            )
        async with lock:
            self._mark(task_id, status="running", started=True)
            ch.publish({"type": "status", "status": "running"})

            image_paths = uploads.image_paths(task_id, image_names)
            if image_paths:
                ch.publish(
                    {"type": "output", "data": f"[bilder] {len(image_paths)} Bild(er) angehängt\n"}
                )
            full_prompt = build_agent_prompt(
                spec, prompt, mode, agents.context_instruction, image_paths=image_paths
            )

            async def on_output(chunk: str) -> None:
                ch.publish({"type": "output", "data": chunk})

            result = await run_agent(
                spec, full_prompt, project_dir, on_output, model=model, effort=effort
            )

            status = "success" if not result.is_error else "failed"
            with session_scope() as db:
                task = db.get(Task, task_id)
                if task:
                    task.output = result.transcript
                    task.result_summary = result.summary
                    task.exit_code = result.exit_code
                    task.status = status
            ch.publish({"type": "status", "status": status})

            self._mark(task_id, finished=True)
            self._update_agents_md(project_id, project_dir)

            await self._git_step(task_id, project_dir, branch, settings, spec.display_name, ch)

            self._publish_done(ch, task_id)

    async def _git_step(
        self,
        task_id: str,
        project_dir: str,
        branch: str,
        settings,
        agent_name: str,
        ch: TaskChannel,
    ) -> None:
        if not project_dir or not Path(project_dir).exists():
            ch.publish({"type": "git", "data": "[git] uebersprungen (kein lokales Repo)\n"})
            return
        token = settings.github_token
        try:
            await asyncio.to_thread(
                git_ops.ensure_identity, project_dir, settings.git_author_name, settings.git_author_email
            )
            changes = await asyncio.to_thread(git_ops.has_changes, project_dir)
            if changes:
                first_line = self._summary_line(task_id)
                msg = (
                    f"{first_line}\n\nAutomatischer Commit durch Coding Dashboard "
                    f"({agent_name}).\nTask-ID: {task_id}"
                )
                commit = await asyncio.to_thread(
                    git_ops.commit_all,
                    project_dir,
                    msg,
                    settings.git_author_name,
                    settings.git_author_email,
                )
                ch.publish(
                    {"type": "git", "data": f"[git] commit {commit[:8] if commit else '-'}: {first_line}\n"}
                )
                with session_scope() as db:
                    task = db.get(Task, task_id)
                    if task:
                        task.commit_hash = commit or ""
                        task.commit_message = first_line
                        task.commit_created = bool(commit)
            else:
                ch.publish({"type": "git", "data": "[git] keine Aenderungen zu committen\n"})

            try:
                await asyncio.to_thread(git_ops.push, project_dir, branch, token)
                ch.publish({"type": "git", "data": f"[git] push -> origin/{branch} OK\n"})
                head = await asyncio.to_thread(git_ops.head_commit, project_dir)
                with session_scope() as db:
                    task = db.get(Task, task_id)
                    if task:
                        task.pushed = True
                        if not task.commit_hash:
                            task.commit_hash = head
            except Exception as exc:  # noqa: BLE001
                ch.publish({"type": "git", "data": f"[git] push fehlgeschlagen: {exc}\n"})
        except Exception as exc:  # noqa: BLE001
            ch.publish({"type": "git", "data": f"[git] Fehler: {exc}\n"})

    _LETZTE_TASKS_RE = re.compile(r"(?m)^##\s*Letzte Tasks\s*$.*", re.DOTALL)

    def _update_agents_md(self, project_id: str, project_dir: str) -> None:
        """Strip residual '## Letzte Tasks' block from old Dashboard versions."""
        if not project_dir or not Path(project_dir).exists():
            return
        agents_path = Path(project_dir) / "AGENTS.md"
        if not agents_path.exists():
            return
        content = agents_path.read_text(encoding="utf-8")
        m = self._LETZTE_TASKS_RE.search(content)
        if m:
            content = content[: m.start()] + content[m.end() :]
            agents_path.write_text(content, encoding="utf-8")

    def _summary_line(self, task_id: str) -> str:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                return "update"
            text = (task.prompt or task.result_summary or "update").strip()
        first = text.splitlines()[0] if text.splitlines() else "update"
        return first[:69] + "..." if len(first) > 72 else first

    def _mark(
        self,
        task_id: str,
        *,
        status: str | None = None,
        error: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if status is not None:
                task.status = status
            if error is not None:
                task.error = error
            if started and task.started_at is None:
                task.started_at = _now()
            if finished and task.finished_at is None:
                task.finished_at = _now()

    def _publish_done(self, ch: TaskChannel, task_id: str) -> None:
        with session_scope() as db:
            task = db.get(Task, task_id)
            data = task_to_dict(task) if task else {"id": task_id, "status": "error"}
        ch.publish({"type": "done", "task": data})


# --------------------------------------------------------------------------- #
# Session Mode: interactive agent sessions
# --------------------------------------------------------------------------- #

class SessionChannel:
    """Pub/sub channel for one interactive session's live output."""

    MAX_BUFFER = 2000

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.buffer: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.closed = False

    def publish(self, msg: dict) -> None:
        self.buffer.append(msg)
        if len(self.buffer) > self.MAX_BUFFER:
            del self.buffer[: len(self.buffer) - self.MAX_BUFFER]
        for q in list(self.subscribers):
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for msg in self.buffer:
            q.put_nowait(msg)
        if self.closed:
            q.put_nowait({"type": "_eof"})
        else:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def close(self) -> None:
        self.closed = True
        for q in list(self.subscribers):
            q.put_nowait({"type": "_eof"})
        self.subscribers.clear()


class SessionManager:
    """Manages interactive agent sessions (long-running subprocess, user chats over WS).

    Each session owns a subprocess whose stdout is streamed to all WS subscribers.
    Messages from the user are written to the subprocess's stdin.  When the user ends
    the session, the subprocess is terminated and a final Task is saved:
    - output = full chat_history JSON
    - result_summary = last assistant message (or tail of transcript)
    Then git commit + push runs, and the Task is marked finished.
    """

    def __init__(self) -> None:
        self._channels: dict[str, SessionChannel] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._parsers: dict[str, object] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        # Temp file paths for last-message files (task_id -> path).
        self._last_msg_paths: dict[str, str] = {}

    def _project_lock(self, project_id: str) -> asyncio.Lock:
        lock = self._project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[project_id] = lock
        return lock

    def get_channel(self, task_id: str) -> SessionChannel | None:
        return self._channels.get(task_id)

    def is_running(self, task_id: str) -> bool:
        return task_id in self._procs

    async def start(
        self,
        task_id: str,
        project_id: str,
        agent_key: str,
        model: str,
        effort: str,
    ) -> None:
        """Launch the subprocess for an interactive session."""
        from .agents import _build_env, _build_command, _make_parser, _write_claude_settings

        agents = get_agents_config()
        spec = agents.agents.get(agent_key)
        if spec is None or not spec.enabled:
            raise ValueError(f"Unknown or disabled agent: {agent_key}")

        project_dir = ""
        with session_scope() as db:
            project = db.get(Project, project_id)
            if project:
                project_dir = project.local_path

        ch = SessionChannel(task_id)
        self._channels[task_id] = ch
        ch.publish({"type": "status", "status": "running"})
        ch.publish({"type": "started", "task_id": task_id})

        lock = self._project_lock(project_id)
        self._locks[task_id] = lock

        # Build last-message temp file if the agent supports it.
        last_message_path = ""
        if any("{last_message_file}" in tok for tok in spec.command):
            fd, tmp = tempfile.mkstemp(prefix="cd-sess-last-", suffix=".txt")
            os.close(fd)
            last_message_path = tmp
            self._last_msg_paths[task_id] = tmp

        cmd = _build_command(
            spec,
            "",  # no initial prompt in session mode
            project_dir,
            model=model,
            effort=effort,
            last_message_file=last_message_path,
        )
        env = _build_env(spec)
        cwd = (spec.cwd or "{project_dir}").replace("{project_dir}", project_dir) or project_dir

        if effort and agent_key == "claude":
            _write_claude_settings(effort)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            msg = f"[Fehler] Agent-Binary nicht gefunden: {cmd[0]!r}"
            ch.publish({"type": "output", "data": msg + "\n"})
            ch.publish({"type": "status", "status": "error"})
            ch.publish({"type": "done", "error": msg})
            ch.close()
            return

        self._procs[task_id] = proc
        parser = _make_parser(spec.stream_format)
        self._parsers[task_id] = parser

        async def pump() -> None:
            """Pump subprocess stdout -> channel, forever until process closes."""
            assert proc.stdout is not None
            try:
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    display = parser.feed(raw.decode("utf-8", errors="replace"))
                    if display:
                        ch.publish({"type": "output", "data": display})
            except asyncio.CancelledError:
                pass

        asyncio.create_task(pump())

    async def send_message(self, task_id: str, content: str) -> None:
        """Send a user message to the session's stdin and store in chat_history."""
        proc = self._procs.get(task_id)
        if proc is None or proc.stdin is None:
            raise RuntimeError("Session process not running")
        ch = self._channels.get(task_id)

        ts = datetime.now(timezone.utc).isoformat()
        msg_entry = {"role": "user", "content": content, "timestamp": ts}

        if ch:
            ch.publish({"type": "message", "role": "user", "content": content})

        self._append_chat_history(task_id, msg_entry)

        proc.stdin.write(content.encode("utf-8") + b"\n")
        await proc.stdin.drain()

    async def end_session(
        self,
        task_id: str,
        project_id: str,
        commit_message: str = "",
    ) -> dict:
        """Terminate the subprocess, commit+push, persist Task."""
        from .agents import _final_output

        proc = self._procs.pop(task_id, None)
        parser = self._parsers.pop(task_id, None)
        ch = self._channels.pop(task_id, None)
        lock = self._locks.pop(task_id, None)
        last_msg_path = self._last_msg_paths.pop(task_id, "")

        # Terminate process.
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            await proc.wait()

        # Read last-message file if available.
        summary = ""
        if last_msg_path:
            try:
                summary = Path(last_msg_path).read_text(errors="replace").strip()
            except Exception:
                pass
        if not summary and parser is not None:
            summary = getattr(parser, "summary", lambda: "")() or ""

        # Build full chat history from DB.
        chat_history = self._get_chat_history(task_id)
        chat_history_json = json.dumps(chat_history, ensure_ascii=False)

        # Full output = chat history as JSON string.
        output_text = chat_history_json

        settings = get_settings()
        exit_code = 0
        status = "success"
        if proc is not None and proc.returncode not in (None, 0):
            status = "failed"
            exit_code = proc.returncode if proc.returncode is not None else -1

        # Update Task record.
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.output = output_text
                task.result_summary = summary or "Session beendet"
                task.chat_history = chat_history_json
                task.status = status
                task.exit_code = exit_code
                task.finished_at = _now()

        if ch:
            ch.publish({"type": "status", "status": status})
            ch.publish({"type": "done", "task_id": task_id, "status": status, "summary": summary})
            ch.close()
        else:
            # No channel means this was a fresh join — just close any stray channel.
            pass

        # Git commit + push.
        commit_hash = ""
        pushed = False
        project_dir = ""
        branch = settings.default_branch
        with session_scope() as db:
            proj = db.get(Project, project_id)
            if proj:
                project_dir = proj.local_path
                branch = proj.default_branch or branch
            t = db.get(Task, task_id)
            if t:
                t.branch = branch

        if project_dir and Path(project_dir).exists() and lock:
            async with lock:
                try:
                    await asyncio.to_thread(
                        git_ops.ensure_identity,
                        project_dir,
                        settings.git_author_name,
                        settings.git_author_email,
                    )
                    changes = await asyncio.to_thread(git_ops.has_changes, project_dir)
                    if changes:
                        msg = commit_message or (
                            f"Session: {summary[:72] if summary else 'interaktive Session'}"
                        )
                        commit_hash = await asyncio.to_thread(
                            git_ops.commit_all,
                            project_dir,
                            msg,
                            settings.git_author_name,
                            settings.git_author_email,
                        )
                        if commit_hash:
                            await asyncio.to_thread(
                                git_ops.push, project_dir, branch, settings.github_token
                            )
                            pushed = True
                except Exception as exc:
                    if ch:
                        ch.publish({"type": "git", "data": f"[git] Fehler: {exc}\n"})
                    commit_hash = ""
                    pushed = False

        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.commit_hash = commit_hash or ""
                task.commit_message = commit_message or ""
                task.commit_created = bool(commit_hash)
                task.pushed = pushed

        return {"status": status, "summary": summary, "commit_hash": commit_hash, "pushed": pushed}

    def _append_chat_history(self, task_id: str, msg_entry: dict) -> None:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                history: list = json.loads(task.chat_history or "[]")
                history.append(msg_entry)
                task.chat_history = json.dumps(history, ensure_ascii=False)

    def _get_chat_history(self, task_id: str) -> list[dict]:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                return json.loads(task.chat_history or "[]")
        return []


# --------------------------------------------------------------------------- #
# Module singletons
# --------------------------------------------------------------------------- #

manager = TaskManager()
session_manager = SessionManager()


def reset_interrupted() -> None:
    """On startup, mark any task still 'running'/'queued' as 'interrupted'."""
    with session_scope() as db:
        rows = db.query(Task).filter(Task.status.in_(["running", "queued"])).all()
        for t in rows:
            t.status = "interrupted"
            if t.finished_at is None:
                t.finished_at = _now()
