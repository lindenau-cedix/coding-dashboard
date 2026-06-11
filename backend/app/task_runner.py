"""Task orchestration: run an agent, stream output, auto-commit & push.

One asyncio task per submitted job; a per-project lock serialises jobs that
touch the same git repo while still allowing different projects to run in
parallel.  Live output is delivered through an in-memory pub/sub channel that
the WebSocket endpoint subscribes to (with full replay for late joiners).
"""
from __future__ import annotations

import asyncio
import json
import re
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

    # -- channel / lock bookkeeping ---------------------------------------- #
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

    # -- submission -------------------------------------------------------- #
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

    # -- execution --------------------------------------------------------- #
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

            # Goal mode hands the agent the goal via its goal_command template
            # (e.g. Claude's "/goal {prompt}") and lets it work until reached.
            # Everything else (streaming, AGENTS.md upkeep, commit, push) is
            # identical to a normal task.
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
                task.output = result.transcript
                task.result_summary = result.summary
                task.exit_code = result.exit_code
                task.status = status
            ch.publish({"type": "status", "status": status})

            # Finish timestamp BEFORE the AGENTS.md rewrite so the current run
            # is the newest of the "Letzte Tasks" entries; the rewrite happens
            # BEFORE the git step so commit & push include the updated file.
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
                    task.pushed = True
                    if not task.commit_hash:
                        task.commit_hash = head
            except Exception as exc:  # noqa: BLE001
                ch.publish({"type": "git", "data": f"[git] push fehlgeschlagen: {exc}\n"})
        except Exception as exc:  # noqa: BLE001
            ch.publish({"type": "git", "data": f"[git] Fehler: {exc}\n"})

    # -- AGENTS.md upkeep ---------------------------------------------------- #
    _LETZTE_TASKS_RE = re.compile(r"(?m)^##\s*Letzte Tasks\s*$")

    def _update_agents_md(self, project_id: str, project_dir: str) -> None:
        """Rewrite the trailing "## Letzte Tasks" section of AGENTS.md.

        Contains the last 3 finished runs (incl. the one that just ended):
        for each the task/prompt the agent received and ONLY its final
        output.  Existing entries are replaced wholesale.  Runs before the
        git step so commit & push include the updated file.
        """
        if not project_dir or not Path(project_dir).exists():
            return
        agents_path = Path(project_dir) / "AGENTS.md"
        with session_scope() as db:
            tasks = (
                db.query(Task)
                .filter(
                    Task.project_id == project_id,
                    Task.status.in_(["success", "failed"]),
                )
                .order_by(Task.finished_at.desc(), Task.created_at.desc())
                .limit(3)
                .all()
            )

        if not tasks:
            return

        parts = [
            "## Letzte Tasks",
            "",
            "_Automatisch vom Dashboard gepflegt: die letzten 3 Agentenläufe"
            " (Aufgabe + Endausgabe). Wird nach jedem Task überschrieben._",
        ]
        for t in tasks:  # newest first (finished_at DESC, current run is first)
            ts = t.finished_at.strftime("%Y-%m-%d %H:%M") if t.finished_at else "?"
            extras = " · ".join(x for x in (t.model, t.effort) if x)
            head = f"### {ts} — {t.agent}" + (f" ({extras})" if extras else "")
            head += " — fehlgeschlagen" if t.status == "failed" else ""
            aufgabe = _embed_md(t.prompt, 600) or "(keine Aufgabe)"
            ausgabe = _embed_md(t.result_summary, 2000) or "(keine Endausgabe)"
            parts += ["", head, "", "**Aufgabe:**", "", aufgabe, "", "**Endausgabe:**", "", ausgabe]
        section = "\n".join(parts).rstrip() + "\n"

        if agents_path.exists():
            content = agents_path.read_text(encoding="utf-8")
            m = self._LETZTE_TASKS_RE.search(content)
            if m:  # replace the existing section (everything from the marker on)
                content = content[: m.start()]
            content = content.rstrip() + "\n\n" + section
        else:
            content = "# AGENTS.md\n\n" + section

        agents_path.write_text(content, encoding="utf-8")

    # -- small DB helpers -------------------------------------------------- #
    def _summary_line(self, task_id: str) -> str:
        with session_scope() as db:
            task = db.get(Task, task_id)
            # The user's prompt makes a cleaner, agent-agnostic commit subject
            # than an output fragment (esp. for raw-streaming agents like Hermes).
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


def _embed_md(text: str, max_chars: int) -> str:
    """Prepare task text for embedding in AGENTS.md.

    Escapes line-leading '#' so embedded content cannot spawn headings (which
    would break the next "## Letzte Tasks" replacement) and clips long text at
    a line boundary instead of mid-word.
    """
    text = (text or "").strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        nl = cut.rfind("\n")
        if nl > max_chars // 2:
            cut = cut[:nl]
        text = cut.rstrip() + "\n[... gekürzt ...]"
    return re.sub(r"(?m)^(\s{0,3})#", r"\1\\#", text)


def reset_interrupted() -> None:
    """On startup, any task still 'running'/'queued' was killed by a restart."""
    with session_scope() as db:
        rows = db.query(Task).filter(Task.status.in_(["running", "queued"])).all()
        for t in rows:
            t.status = "interrupted"
            if t.finished_at is None:
                t.finished_at = _now()


manager = TaskManager()
