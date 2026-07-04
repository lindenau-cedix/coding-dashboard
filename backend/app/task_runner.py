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
import shlex
import shutil
import tempfile
import traceback
from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from . import git_ops, host_lock, host_staging, session_dirs, uploads
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


def _pull_ff_only(repo_dir: str | Path, branch: str, token: str) -> str:
    """``git pull --ff-only`` — refuse to merge or rebase, just fast-forward.

    Used by the auto-pull step before each task.  Plain ``git pull`` will
    auto-merge when fast-forward isn't possible, which would silently make a
    merge commit on the user's behalf; ``--ff-only`` makes the operation a
    strict no-op (or failure) when local has diverged, so the caller can
    decide what to do.  The token is supplied so the fetch half authenticates
    against a private remote.
    """
    import subprocess as _sp

    base = ["git"]
    if token:
        base += git_ops._auth_args(token)
    base += ["pull", "--ff-only", "origin", branch]
    proc = _sp.run(base, cwd=str(repo_dir), capture_output=True, text=True)
    if proc.returncode != 0:
        raise git_ops.GitError(
            (proc.stderr or proc.stdout or "git pull --ff-only failed").strip()
        )
    return proc.stdout.strip()


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


def _worktrees_root() -> Path:
    return get_settings().data_dir / "worktrees"


def _merge_worktree_branch(
    project_dir: str,
    worktree_dir: str,
    branch: str,
    base_branch: str,
    token: str,
    author_name: str,
    author_email: str,
    commit_message: str,
    first_line: str,
) -> dict:
    """Commit the worktree's branch, merge it into the default branch, push, clean up.

    Synchronous (run via ``asyncio.to_thread``) and always invoked under the
    per-project lock so the default-branch checkout in ``project_dir`` is touched
    by only one task/session at a time.  Shared by TaskManager and SessionManager.
    On a merge conflict the feature branch is kept and pushed for manual merge.
    """
    commit_hash = ""
    commit_created = False
    pushed = False
    merge_state = ""
    messages: list[str] = []
    try:
        git_ops.ensure_identity(worktree_dir, author_name, author_email)
        if git_ops.has_changes(worktree_dir):
            commit_hash = git_ops.commit_all(
                worktree_dir, commit_message, author_name, author_email
            ) or ""
            if commit_hash:
                commit_created = True
                messages.append(f"[git] commit {commit_hash[:8]}: {first_line}\n")
        else:
            messages.append("[git] keine Aenderungen zu committen\n")

        merged, _out = git_ops.merge_branch(
            project_dir, branch, f"Merge {branch} (Coding Dashboard)"
        )
        if merged:
            merge_state = "merged"
            messages.append(f"[git] merge {branch} -> {base_branch} OK\n")
            try:
                git_ops.push(project_dir, base_branch, token)
                pushed = True
                messages.append(f"[git] push -> origin/{base_branch} OK\n")
                if not commit_hash:
                    commit_hash = git_ops.head_commit(project_dir)
            except Exception as exc:  # noqa: BLE001
                messages.append(f"[git] push fehlgeschlagen: {exc}\n")
        else:
            merge_state = "conflict"
            messages.append(
                f"[git] Merge-Konflikt: {branch} bleibt erhalten (manueller Merge nötig)\n"
            )
            try:
                git_ops.push_ref(project_dir, branch, branch, token)
                messages.append(f"[git] Branch {branch} -> origin gepusht\n")
            except Exception as exc:  # noqa: BLE001
                messages.append(f"[git] Branch-Push fehlgeschlagen: {exc}\n")
    except Exception as exc:  # noqa: BLE001
        messages.append(f"[git] Fehler: {exc}\n")
    finally:
        git_ops.remove_worktree(project_dir, worktree_dir)
        shutil.rmtree(worktree_dir, ignore_errors=True)
        if merge_state == "merged":
            git_ops.delete_branch(project_dir, branch, force=True)
    return {
        "commit_hash": commit_hash,
        "commit_created": commit_created,
        "pushed": pushed,
        "merge_state": merge_state,
        "messages": messages,
    }


class TaskManager:
    def __init__(self, max_channels: int = 100) -> None:
        self._channels: "OrderedDict[str, TaskChannel]" = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._running: dict[str, asyncio.Task] = {}
        # task_id -> (project_dir, worktree_dir, branch) for isolated runs so a
        # cancel/crash can still detach the worktree.
        self._worktrees: dict[str, tuple[str, str, str]] = {}
        # task_id -> (project_dir, staging_dir, branch) for host-staging runs
        # (agent runs in a copy under hermes_staging_dir) so cancel/crash cleans up.
        self._staging: dict[str, tuple[str, str, str]] = {}
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
        # The host-visible lock file is stamped inside `_run_inner` once we know
        # the agent + project + mode (after the DB lookup).  Cleanup is fanned
        # out to every exit path here, so cancel / error / success all drop it.
        # We still defensively remove in `_run_inner` itself when mode resolution
        # fails early (unknown agent) so an interrupted loop can never leak.
        try:
            await self._run_inner(task_id, project_id, ch)
        except asyncio.CancelledError:
            self._cleanup_worktree(task_id)
            self._cleanup_staging(task_id)
            self._mark(task_id, status="cancelled", error="Abgebrochen.", finished=True)
            ch.publish({"type": "output", "data": "\n[abgebrochen]\n"})
            ch.publish({"type": "status", "status": "cancelled"})
            self._publish_done(ch, task_id)
            raise
        except Exception as exc:  # noqa: BLE001
            self._cleanup_worktree(task_id)
            self._cleanup_staging(task_id)
            tb = traceback.format_exc()
            self._mark(task_id, status="error", error=f"{exc}\n{tb}", finished=True)
            ch.publish({"type": "output", "data": f"\n[interner Fehler] {exc}\n"})
            ch.publish({"type": "status", "status": "error"})
            self._publish_done(ch, task_id)
        finally:
            host_lock.remove("task", task_id)
            ch.close()

    def _cleanup_worktree(self, task_id: str) -> None:
        """Best-effort detach of an isolated run's worktree (cancel/crash path)."""
        info = self._worktrees.pop(task_id, None)
        if not info:
            return
        project_dir, worktree_dir, _branch = info
        try:
            git_ops.remove_worktree(project_dir, worktree_dir)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(worktree_dir, ignore_errors=True)

    def _cleanup_staging(self, task_id: str) -> None:
        """Best-effort removal of a host-staging copy (cancel/crash path)."""
        info = self._staging.pop(task_id, None)
        if not info:
            return
        _project_dir, staging_dir, _branch = info
        shutil.rmtree(staging_dir, ignore_errors=True)

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

        self._mark(task_id, status="running", started=True)
        ch.publish({"type": "status", "status": "running"})
        # Stamp the host-visible lock file.  One per active run; the host sees
        # ``task-<id>.lock`` appear here and disappear again in `_run`'s
        # finally.  Best-effort: failure to write does NOT abort the run.
        host_lock.write("task", task_id, project_id, agent_key, mode)

        # Auto-pull: keep the project's default branch in sync with the remote
        # so the agent works against the latest code (rather than building on a
        # stale local HEAD). Best-effort: a failed fetch/publishes a warning
        # and the run continues — the agent still has a coherent working tree.
        # Skipped for host-staging agents because they run in a copy that the
        # host can't push to; the canonical repo IS the source of truth here.
        if (
            not getattr(spec, "host_staging", False)
            and project_dir
            and Path(project_dir).exists()
            and git_ops.is_git_repo(project_dir)
        ):
            await self._auto_pull(project_id, project_dir, branch, settings, ch)

        # Run in an isolated worktree on a dedicated branch so multiple tasks
        # for the same project run in parallel without clobbering each other's
        # working tree.  The branch is merged back into the default branch (and
        # pushed) once the agent finishes.  If worktree setup is impossible
        # (no repo / git error) we fall back to running directly in project_dir.
        run_dir = project_dir
        run_branch = branch
        isolated = False
        staging = False
        # Host-staging agents (e.g. the SSH-driven Hermes) cannot see the data dir,
        # so they run in a COPY of the project under hermes_staging_dir (shared with
        # the host at an identical path); the copy is merged back afterwards.
        if (
            getattr(spec, "host_staging", False)
            and project_dir
            and git_ops.is_git_repo(project_dir)
        ):
            try:
                run_dir, run_branch = await self._setup_staging(
                    project_id, task_id, project_dir, branch, mode, ch
                )
                staging = True
            except Exception as exc:  # noqa: BLE001
                ch.publish(
                    {
                        "type": "git",
                        "data": f"[git] Host-Arbeitskopie fehlgeschlagen ({exc}); "
                        f"nutze Hauptverzeichnis\n",
                    }
                )
                run_dir, run_branch = project_dir, branch
        elif project_dir and Path(project_dir).exists():
            try:
                run_dir, run_branch = await self._setup_worktree(
                    project_id, task_id, project_dir, branch, mode, ch
                )
                isolated = True
            except Exception as exc:  # noqa: BLE001
                ch.publish(
                    {
                        "type": "git",
                        "data": f"[git] Worktree fehlgeschlagen ({exc}); nutze Hauptverzeichnis\n",
                    }
                )
                run_dir, run_branch = project_dir, branch
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.branch = run_branch

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
            spec, full_prompt, run_dir, on_output, model=model, effort=effort
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
        self._update_agents_md(project_id, run_dir)

        if staging:
            await self._finalize_staging(
                task_id, project_id, project_dir, run_dir, run_branch, branch, settings,
                spec.display_name, ch,
            )
        elif isolated:
            await self._finalize_isolated(
                task_id, project_id, project_dir, run_dir, run_branch, branch, settings,
                spec.display_name, ch,
            )
        else:
            async with self._project_lock(project_id):
                await self._git_step(
                    task_id, project_dir, branch, settings, spec.display_name, ch
                )

        self._publish_done(ch, task_id)

    async def _auto_pull(
        self,
        project_id: str,
        project_dir: str,
        branch: str,
        settings,
        ch: TaskChannel,
    ) -> None:
        """Fetch + fast-forward the project's default branch off the remote.

        Runs under the per-project lock so it serialises against another task's
        finalize step on the same project.  Strategy:
          1. ``git fetch origin`` (always, harmless on a dirty tree).
          2. Skip the merge when nothing new is on the remote.
          3. ``git pull --ff-only`` on the default branch (fast-forward only —
             we never throw away local commits silently).
          4. If the fast-forward is blocked (dirty tree, divergence, conflict,
             off-host agent), publish a one-line warning to the live stream and
             continue.  The agent still gets a coherent working tree — just one
             based on the pre-fetch local HEAD.
        """
        token = settings.github_token

        async with self._project_lock(project_id):
            try:
                fetch_out = await asyncio.to_thread(
                    git_ops.fetch_only, project_dir, token
                )
                if fetch_out:
                    ch.publish({"type": "git", "data": f"[git] fetch: {fetch_out}\n"})
            except Exception as exc:  # noqa: BLE001
                ch.publish(
                    {"type": "git", "data": f"[git] auto-pull: fetch fehlgeschlagen ({exc}); nutze lokales HEAD\n"}
                )
                return

            # Cheap no-op when there's nothing new on the remote.
            try:
                has_update = await asyncio.to_thread(
                    git_ops.has_remote_update, project_dir, branch
                )
            except Exception:  # noqa: BLE001
                has_update = True
            if not has_update:
                ch.publish({"type": "git", "data": "[git] auto-pull: bereits aktuell\n"})
                return

            try:
                pull_out = await asyncio.to_thread(
                    _pull_ff_only, project_dir, branch, token
                )
                if pull_out.strip():
                    ch.publish({"type": "git", "data": f"[git] auto-pull: {pull_out}\n"})
                else:
                    ch.publish({"type": "git", "data": "[git] auto-pull: fast-forward OK\n"})
            except Exception as exc:  # noqa: BLE001
                ch.publish(
                    {
                        "type": "git",
                        "data": (
                            f"[git] auto-pull: fast-forward nicht möglich ({exc}); "
                            "fahre mit lokalem HEAD fort\n"
                        ),
                    }
                )

    async def _setup_worktree(
        self,
        project_id: str,
        task_id: str,
        project_dir: str,
        base_branch: str,
        mode: str,
        ch: TaskChannel,
    ) -> tuple[str, str]:
        """Create a fresh worktree+branch for this task off the current checkout.

        The start point is ``HEAD`` of the project's main checkout (not the
        named default branch): ``git worktree add -b X <dir> <name>`` silently
        DWIMs to ``origin/<name>`` and ignores ``-b`` when no local ref ``<name>``
        exists, which would put every task on the same branch.  ``HEAD`` always
        resolves to a concrete local commit, so the new branch is created cleanly.
        """
        branch = f"cd/{mode}/{task_id[:8]}"
        worktree_dir = _worktrees_root() / task_id

        def setup() -> None:
            if worktree_dir.exists():
                git_ops.remove_worktree(project_dir, worktree_dir)
                shutil.rmtree(worktree_dir, ignore_errors=True)
            if git_ops.branch_exists(project_dir, branch):
                git_ops.delete_branch(project_dir, branch, force=True)
            worktree_dir.parent.mkdir(parents=True, exist_ok=True)
            git_ops.add_worktree(project_dir, worktree_dir, branch, "HEAD")

        async with self._project_lock(project_id):
            await asyncio.to_thread(setup)
        self._worktrees[task_id] = (project_dir, str(worktree_dir), branch)
        ch.publish({"type": "git", "data": f"[git] Branch {branch} (isolierter Worktree)\n"})
        return str(worktree_dir), branch

    async def _finalize_isolated(
        self,
        task_id: str,
        project_id: str,
        project_dir: str,
        worktree_dir: str,
        branch: str,
        base_branch: str,
        settings,
        agent_name: str,
        ch: TaskChannel,
    ) -> None:
        """Commit the worktree branch, merge it into the default branch, push, clean up."""
        token = settings.github_token
        first_line = self._summary_line(task_id)
        msg = (
            f"{first_line}\n\nAutomatischer Commit durch Coding Dashboard "
            f"({agent_name}).\nTask-ID: {task_id}"
        )

        def finish() -> dict:
            return _merge_worktree_branch(
                project_dir, worktree_dir, branch, base_branch, token,
                settings.git_author_name, settings.git_author_email, msg, first_line,
            )

        async with self._project_lock(project_id):
            res = await asyncio.to_thread(finish)
        self._worktrees.pop(task_id, None)
        for message in res["messages"]:
            ch.publish({"type": "git", "data": message})
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.commit_hash = res["commit_hash"] or ""
                task.commit_message = first_line if res["commit_created"] else ""
                task.commit_created = res["commit_created"]
                task.pushed = res["pushed"]
                task.merge_state = res["merge_state"]

    async def _setup_staging(
        self,
        project_id: str,
        task_id: str,
        project_dir: str,
        base_branch: str,
        mode: str,
        ch: TaskChannel,
    ) -> tuple[str, str]:
        """Copy the project into a host-shared staging dir for an off-host agent.

        Returns ``(staging_dir, branch)``.  ``branch`` is the name the staging
        commit is later fetched into and merged from (``cd/<mode>/<id>``).  The
        copy is taken under the per-project lock so it is a consistent snapshot of
        the canonical checkout.
        """
        branch = f"cd/{mode}/{task_id[:8]}"
        staging_dir = host_staging.task_staging_dir(task_id)

        async with self._project_lock(project_id):
            await asyncio.to_thread(host_staging.prepare_copy, project_dir, staging_dir)
        self._staging[task_id] = (project_dir, str(staging_dir), branch)
        ch.publish(
            {
                "type": "git",
                "data": f"[git] Host-Arbeitskopie {staging_dir} "
                f"(Merge als Branch {branch})\n",
            }
        )
        return str(staging_dir), branch

    async def _finalize_staging(
        self,
        task_id: str,
        project_id: str,
        project_dir: str,
        staging_dir: str,
        branch: str,
        base_branch: str,
        settings,
        agent_name: str,
        ch: TaskChannel,
    ) -> None:
        """Integrate the staging copy back into the canonical repo, then remove it."""
        token = settings.github_token
        first_line = self._summary_line(task_id)
        msg = (
            f"{first_line}\n\nAutomatischer Commit durch Coding Dashboard "
            f"({agent_name}).\nTask-ID: {task_id}"
        )

        def finish() -> dict:
            return host_staging.integrate(
                project_dir, staging_dir, branch, base_branch, token,
                settings.git_author_name, settings.git_author_email, msg, first_line,
                cleanup=True,
            )

        async with self._project_lock(project_id):
            res = await asyncio.to_thread(finish)
        self._staging.pop(task_id, None)
        for message in res["messages"]:
            ch.publish({"type": "git", "data": message})
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.commit_hash = res["commit_hash"] or ""
                task.commit_message = first_line if res["commit_created"] else ""
                task.commit_created = res["commit_created"]
                task.pushed = res["pushed"]
                task.merge_state = res["merge_state"]

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

    def subscribe(self, replay: bool = True) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        if replay:
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
    """Manage interactive agent TUIs running in PTYs.

    Each session owns a process attached to a real terminal. Raw keyboard bytes
    from the browser are written to the PTY master; raw PTY output is streamed
    to subscribers and appended to Task.output as the durable transcript. Closing
    the browser only closes the WebSocket, not the process.
    """

    def __init__(self) -> None:
        self._channels: dict[str, SessionChannel] = {}
        # In PTY mode we store {"pid": int, "master_fd": int}.
        self._procs: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._task_projects: dict[str, str] = {}
        self._ending: set[str] = set()

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
        start_args: str = "",
    ) -> bool:
        """Launch the subprocess for an interactive session using a PTY.

        The agent runs in a true interactive shell so keyboard input (Enter,
        arrow keys, Ctrl-C, etc.) is forwarded faithfully. No prompt is
        injected; the configured session_command is only extended with the
        user-supplied start_args parsed as argv.
        """
        import os
        import signal
        import fcntl
        import struct
        import termios

        from .agents import _build_env

        agents = get_agents_config()
        spec = agents.agents.get(agent_key)
        if spec is None or not spec.enabled:
            raise ValueError(f"Unknown or disabled agent: {agent_key}")
        if not spec.session_command:
            raise ValueError(f"Agent {agent_key} does not support session mode")

        project_dir = ""
        base_branch = get_settings().default_branch
        with session_scope() as db:
            project = db.get(Project, project_id)
            if project:
                project_dir = project.local_path
                base_branch = project.default_branch or base_branch

        # Fetch (without merging) so a long-lived session sees fresh code
        # when it next does ``git pull`` or its CLI refetches.  Best-effort:
        # network is not always reachable; never block the session on it.
        # Skipped for host-staging (off-host, no shared remote-tracking ref).
        if (
            not getattr(spec, "host_staging", False)
            and project_dir
            and Path(project_dir).exists()
            and git_ops.is_git_repo(project_dir)
        ):
            try:
                await asyncio.to_thread(
                    git_ops.fetch_only, project_dir, get_settings().github_token
                )
            except Exception:  # noqa: BLE001
                pass

        # Resolve the working directory before anything else. A resume must run
        # in the directory where the session was created (agents key saved
        # sessions by cwd); a new session may get its own isolated worktree so
        # several sessions can run in parallel. ``workdir_note`` is shown to the
        # user so it is obvious which folder the agent is operating in.
        try:
            argv_extra = shlex.split(start_args) if start_args.strip() else []
        except ValueError:
            argv_extra = []
        resume_req = session_dirs.parse_resume_request(agent_key, argv_extra)
        if (
            getattr(spec, "host_staging", False)
            and project_dir
            and git_ops.is_git_repo(project_dir)
        ):
            # Off-host agent (SSH-driven Hermes): run in a stable per-project copy
            # under the shared staging dir so the host can reach it and `--resume`
            # finds the same cwd again. Integrated back (not just pushed) on end.
            workdir, workdir_note = await self._resolve_staging_session_workdir(
                project_id, project_dir, resume_req is not None
            )
        else:
            workdir, workdir_note = await self._resolve_session_workdir(
                task_id, project_id, project_dir, agent_key, resume_req
            )

        ch = SessionChannel(task_id)
        self._channels[task_id] = ch
        ch.publish({"type": "status", "status": "running"})
        ch.publish({"type": "started", "task_id": task_id})
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.status = "running"
                task.workdir = workdir
                if task.started_at is None:
                    task.started_at = _now()

        lock = self._project_lock(project_id)
        self._locks[task_id] = lock
        self._task_projects[task_id] = project_id

        # Build the interactive TUI command. Session mode intentionally does not
        # inject prompt/model/effort args; explicit start parameters are the
        # single source of argv additions and are still executed without a shell.
        # ``{project_dir}`` resolves to the chosen working directory so a session
        # in an isolated worktree references that worktree, not the shared repo.
        cmd = []
        for tok in spec.session_command:
            cmd.append(tok.replace("{project_dir}", workdir))
        if argv_extra:
            cmd += [tok.replace("{project_dir}", workdir) for tok in argv_extra]

        env = _build_env(spec)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        cwd = (spec.cwd or "{project_dir}").replace("{project_dir}", workdir) or workdir

        # Fork a PTY for the subprocess.
        try:
            master_fd, slave_fd = os.openpty()
        except OSError as exc:
            msg = f"[Fehler] PTY konnte nicht erstellt werden: {exc}"
            self._fail_start(task_id, ch, msg)
            return False

        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        except OSError:
            pass

        try:
            pid = os.fork()
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            msg = f"[Fehler] fork() fehlgeschlagen: {exc}"
            self._fail_start(task_id, ch, msg)
            return False

        if pid == 0:
            # Child: become session leader, set controlling TTY, redirect stdio.
            os.close(master_fd)
            try:
                os.setsid()
                # Set slave as controlling terminal.
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            os.close(slave_fd)
            try:
                os.chdir(cwd or project_dir or str(Path.home()))
                # Reset signal handlers, set clean env.
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                os.execvpe(cmd[0], cmd, env)
            except Exception as exc:  # noqa: BLE001
                print(f"[Fehler] Session-Kommando konnte nicht gestartet werden: {exc}", flush=True)
                os._exit(127)
        else:
            # Parent: close slave, keep master.
            os.close(slave_fd)
            try:
                os.set_blocking(master_fd, False)
            except OSError:
                pass

        proc_info = {
            "pid": pid,
            "master_fd": master_fd,
            "project_id": project_id,
            "workdir": workdir,
        }
        self._procs[task_id] = proc_info  # type: ignore[assignment]

        # Stamp the host-visible lock file.  The session lives until
        # `end_session` (manual stop, frontend disconnect-via-stop endpoint,
        # or end-of-process), which always removes the lock in its `finally`.
        # Best-effort: a failure here does not abort the session.
        host_lock.write("session", task_id, project_id, agent_key, "session")

        # Surface the working directory (resume target / isolated worktree) so it
        # is visible in the terminal transcript and persisted with the session.
        if workdir_note:
            offset = self._append_terminal_output(task_id, workdir_note)
            ch.publish({"type": "output", "data": workdir_note, "offset": offset})

        # Enable DEC private mode 2004 (bracketed paste) up front so paste from
        # the browser is treated as a single event by full-screen TUIs (Claude
        # Code, Codex, Hermes, …) — otherwise newlines in pasted text are
        # interpreted as Enter and submit the prompt prematurely. Most modern
        # TUIs enable this themselves; we do it again for any that don't.
        try:
            os.write(master_fd, b"\x1b[?2004h")
        except OSError:
            pass

        async def pump() -> None:
            """Pump raw PTY output -> channel, forever until process closes."""
            master = master_fd
            ch_local = ch
            try:
                while task_id in self._procs:
                    try:
                        raw = os.read(master, 4096)
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                        continue
                    except OSError:
                        break
                    if not raw:
                        break
                    display = raw.decode("utf-8", errors="replace")
                    offset = self._append_terminal_output(task_id, display)
                    ch_local.publish({"type": "output", "data": display, "offset": offset})
            except asyncio.CancelledError:
                pass
            finally:
                if task_id in self._procs and task_id not in self._ending:
                    await self.end_session(task_id, project_id, terminate=False)

        asyncio.create_task(pump())
        return True

    def _fail_start(self, task_id: str, ch: SessionChannel, message: str) -> None:
        chunk = message + "\n"
        offset = self._append_terminal_output(task_id, chunk)
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.status = "error"
                task.error = message
                task.result_summary = message
                task.exit_code = 127
                task.finished_at = _now()
        ch.publish({"type": "output", "data": chunk, "offset": offset})
        ch.publish({"type": "status", "status": "error"})
        ch.publish(
            {
                "type": "done",
                "task_id": task_id,
                "status": "error",
                "summary": message,
            }
        )
        ch.close()
        self._channels.pop(task_id, None)

    # --- session working-directory resolution ------------------------------- #
    async def _resolve_session_workdir(
        self,
        task_id: str,
        project_id: str,
        project_dir: str,
        agent_key: str,
        resume_req: "session_dirs.ResumeRequest | None",
    ) -> tuple[str, str]:
        """Choose the directory a session runs in.

        Returns ``(workdir, note)``; ``note`` is a short human-readable line for
        the terminal (empty when the plain project folder is used).

        The *decision* runs synchronously (fast in-memory / DB / small-file
        reads) so the ``_primary_busy`` check stays atomic, but the only
        genuinely expensive step — a ``git worktree`` checkout, which scales with
        repo size — is off-loaded with :func:`asyncio.to_thread` so it never
        blocks the event loop (and thus other live sessions' PTY pumps). Those
        checkouts always target a *unique* worktree path, so off-loading them
        introduces no race for the shared project folder.
        """
        # 1. Resume of a SPECIFIC session: run where that session lives so the
        #    agent CLI (which keys conversations by cwd) finds it again.
        if resume_req is not None and resume_req.session_id:
            recorded = session_dirs.resolve_recorded_cwd(agent_key, resume_req.session_id)
            if recorded:
                if not Path(recorded).exists():
                    await asyncio.to_thread(self._recreate_worktree, project_dir, recorded)
                if Path(recorded).exists():
                    return recorded, f"[resume] Verzeichnis der Session: {recorded}\n"

        # 2. Resume "last"/"continue" (directory-bound): re-use the most recent
        #    prior session directory of the SAME agent for this project.
        if resume_req is not None:
            prev = self._last_session_workdir(project_id, agent_key)
            if prev:
                return prev, f"[resume] Verzeichnis der letzten Session: {prev}\n"

        # 3. New session. If the project folder is already busy with a live
        #    session, give this one its own worktree so they don't clobber each
        #    other; otherwise use the project folder directly (keeps git history
        #    linear in the common single-session case).
        if (
            project_dir
            and self._primary_busy(project_id, project_dir)
            and git_ops.is_git_repo(project_dir)
        ):
            worktree = await asyncio.to_thread(
                self._make_session_worktree, project_id, task_id, project_dir
            )
            if worktree:
                return worktree, f"[parallel] Isolierte Arbeitskopie: {worktree}\n"

        return project_dir, ""

    async def _resolve_staging_session_workdir(
        self, project_id: str, project_dir: str, resume: bool
    ) -> tuple[str, str]:
        """Working dir for an off-host (host-staging) session: a per-project copy.

        One stable copy per project under the shared staging dir, so the host's
        agent can reach it and ``--resume`` lands in the same cwd.  A resume keeps
        the existing copy (its working tree backs the saved conversation); a fresh
        session re-copies the current project HEAD.  Concurrent host-staging
        sessions for the *same* project share this copy and are not isolated — run
        them sequentially.
        """
        staging = str(host_staging.session_staging_dir(project_id))
        await asyncio.to_thread(
            host_staging.ensure_session_copy, project_dir, staging, resume
        )
        if resume:
            note = f"[resume] Host-Arbeitskopie: {staging}\n"
        else:
            note = f"[host] Arbeitskopie auf dem Host: {staging}\n"
        return staging, note

    def _primary_busy(self, project_id: str, project_dir: str) -> bool:
        """True if a live session already occupies the project's main folder."""
        return any(
            info.get("project_id") == project_id and info.get("workdir") == project_dir
            for info in self._procs.values()
        )

    def _worktrees_root(self) -> Path:
        return get_settings().data_dir.resolve() / "session_worktrees"

    def _make_session_worktree(
        self, project_id: str, task_id: str, project_dir: str
    ) -> str | None:
        path = self._worktrees_root() / project_id / task_id
        try:
            git_ops.add_worktree(project_dir, path)
        except Exception:  # noqa: BLE001
            return None
        return str(path)

    def _is_session_worktree(self, path: str) -> bool:
        return bool(path) and path.startswith(str(self._worktrees_root()))

    async def _cleanup_worktree_if_done(
        self,
        git_dir: str,
        project_local_path: str,
        pushed: bool,
        ch: "SessionChannel | None",
    ) -> None:
        """Remove an isolated session worktree after its work reached the remote.

        No-op for the shared project folder and for worktrees with unpushed work
        (kept for recovery). Best effort: failures never abort end_session.
        """
        if not pushed or not self._is_session_worktree(git_dir):
            return
        if not project_local_path or not Path(project_local_path).exists():
            return
        try:
            await asyncio.to_thread(
                git_ops.remove_worktree, project_local_path, git_dir
            )
            if ch:
                ch.publish(
                    {"type": "git", "data": "[git] Isolierte Arbeitskopie aufgeraeumt\n"}
                )
        except Exception:  # noqa: BLE001
            pass

    def _recreate_worktree(self, project_dir: str, target: str) -> None:
        """Best-effort re-create a pruned session worktree at ``target``."""
        if not target.startswith(str(self._worktrees_root())):
            return
        if project_dir and git_ops.is_git_repo(project_dir):
            try:
                git_ops.add_worktree(project_dir, target)
            except Exception:  # noqa: BLE001
                pass

    def _last_session_workdir(self, project_id: str, agent_key: str) -> str | None:
        with session_scope() as db:
            rows = (
                db.query(Task)
                .filter(
                    Task.project_id == project_id,
                    Task.agent == agent_key,
                    Task.is_session.is_(True),
                    Task.workdir != "",
                )
                .order_by(Task.created_at.desc())
                .limit(20)
                .all()
            )
            for t in rows:
                if t.workdir and Path(t.workdir).exists():
                    return t.workdir
        return None

    async def send_message(self, task_id: str, content: str) -> None:
        """Forward raw data to the PTY master fd (keyboard strokes, etc.)."""
        proc_info = self._procs.get(task_id)
        if proc_info is None:
            raise RuntimeError("Session process not running")
        master_fd = proc_info["master_fd"]

        # Forward raw bytes directly to the PTY master.
        import fcntl
        import os
        import errno

        data = memoryview(content.encode("utf-8"))
        while data:
            try:
                written = os.write(master_fd, data)
                if written <= 0:
                    await asyncio.sleep(0.01)
                    continue
                data = data[written:]
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # PTY slave closed — process is gone
                    return
                raise

    async def resize(self, task_id: str, cols: int, rows: int) -> None:
        """Resize the PTY so full-screen TUIs can lay themselves out."""
        proc_info = self._procs.get(task_id)
        if proc_info is None:
            return
        cols = max(20, min(cols, 300))
        rows = max(5, min(rows, 120))

        import fcntl
        import os
        import signal
        import struct
        import termios

        master_fd = proc_info["master_fd"]
        pid = proc_info["pid"]
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.killpg(pid, signal.SIGWINCH)
        except OSError:
            pass

    async def end_session(
        self,
        task_id: str,
        project_id: str,
        commit_message: str = "",
        terminate: bool = True,
    ) -> dict:
        """End the PTY session, persist transcript, then commit and push."""
        import os
        import signal

        if task_id in self._ending:
            return self._session_result(task_id)
        self._ending.add(task_id)

        if not project_id:
            project_id = self._task_projects.get(task_id, "")
        if not project_id:
            with session_scope() as db:
                task = db.get(Task, task_id)
                if task:
                    project_id = task.project_id

        try:
            return await self._end_session_locked(
                task_id, project_id, commit_message, terminate
            )
        finally:
            # Always drop the host-visible lock file: success, manual stop,
            # backend-restart-detected-via-pump-failure, AND an exception
            # raised by the commit/push step.  Idempotent if the file was
            # already gone (another concurrent end_session removed it).
            host_lock.remove("session", task_id)
            self._ending.discard(task_id)

    async def _end_session_locked(
        self,
        task_id: str,
        project_id: str,
        commit_message: str,
        terminate: bool,
    ) -> dict:
        """Original end_session body, kept verbatim but isolated so the host
        lock removal in the outer ``end_session`` finally runs in every case.
        """
        proc_info = self._procs.pop(task_id, None)
        ch = self._channels.get(task_id)
        lock = self._locks.pop(task_id, None)
        if lock is None and project_id:
            lock = self._project_lock(project_id)

        exit_code = 0
        status = "success"
        if proc_info is not None:
            master_fd = proc_info["master_fd"]
            pid = proc_info["pid"]
            if terminate:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    pass
            for _ in range(10):
                try:
                    pid_ret, code = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pid_ret = pid
                    code = 0
                except OSError:
                    pid_ret = pid
                    code = 0
                if pid_ret != 0:
                    if os.WIFEXITED(code):
                        exit_code = os.WEXITSTATUS(code)
                    elif os.WIFSIGNALED(code):
                        exit_code = -os.WTERMSIG(code)
                    break
                await asyncio.sleep(0.1)
            else:
                if terminate:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        os.waitpid(pid, 0)
                    except (ChildProcessError, OSError):
                        pass
                    exit_code = -signal.SIGKILL
            try:
                os.close(master_fd)
            except OSError:
                pass
            if not terminate and exit_code != 0:
                status = "failed"

        output_text = self._get_terminal_output(task_id)
        summary = "Interaktive TUI-Session beendet"

        settings = get_settings()
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.output = output_text
                task.result_summary = summary or "Session beendet"
                task.status = status
                task.exit_code = exit_code
                task.finished_at = _now()

        commit_hash = ""
        commit_created = False
        pushed = False
        git_dir = ""
        project_local_path = ""
        branch = settings.default_branch
        with session_scope() as db:
            proj = db.get(Project, project_id)
            if proj:
                branch = proj.default_branch or branch
                project_local_path = proj.local_path
            t = db.get(Task, task_id)
            if t:
                t.branch = branch
                # Commit/push from the directory the session actually ran in
                # (an isolated worktree for parallel sessions, else local_path).
                git_dir = t.workdir or project_local_path
            elif proj:
                git_dir = project_local_path

        if git_dir and Path(git_dir).exists() and lock:
            async with lock:
                msg_full = commit_message.strip() or f"Session: {summary}"
                first_line = msg_full.splitlines()[0] if msg_full else summary
                if host_staging.is_staging_dir(git_dir):
                    # Off-host (host-staging) session: integrate the host copy back
                    # into the canonical repo (commit -> merge -> push; conflict
                    # leaves a branch for a manual merge). Keep the copy so a later
                    # `--resume` finds the same cwd again.
                    res = await asyncio.to_thread(
                        host_staging.integrate,
                        project_local_path,
                        git_dir,
                        f"cd/session/{task_id[:8]}",
                        branch,
                        settings.github_token,
                        settings.git_author_name,
                        settings.git_author_email,
                        msg_full,
                        first_line,
                        cleanup=False,
                    )
                    commit_hash = res["commit_hash"]
                    commit_created = res["commit_created"]
                    pushed = res["pushed"]
                    if ch:
                        for message in res["messages"]:
                            ch.publish({"type": "git", "data": message})
                else:
                    (
                        commit_hash,
                        commit_created,
                        pushed,
                        git_messages,
                    ) = await asyncio.to_thread(
                        self._finish_session_git,
                        git_dir,
                        branch,
                        settings.github_token,
                        settings.git_author_name,
                        settings.git_author_email,
                        msg_full,
                    )
                    if ch:
                        for message in git_messages:
                            ch.publish({"type": "git", "data": message})

                    # Reclaim an isolated per-session worktree once its work is
                    # safely on the remote. If the push FAILED we keep the worktree
                    # so the commits are not stranded — a later resume re-enters it
                    # and can retry the push. (The agent's conversation lives in its
                    # own store, so a clean worktree is recreated on resume anyway.)
                    await self._cleanup_worktree_if_done(
                        git_dir, project_local_path, pushed, ch
                    )

        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                task.commit_hash = commit_hash or ""
                task.commit_message = (
                    commit_message.strip() or (f"Session: {summary}" if commit_created else "")
                )
                task.commit_created = commit_created
                task.pushed = pushed

        if ch:
            ch.publish({"type": "status", "status": status})
            ch.publish(
                {
                    "type": "done",
                    "task_id": task_id,
                    "status": status,
                    "summary": summary,
                    "commit_hash": commit_hash,
                    "pushed": pushed,
                }
            )
            ch.close()
        self._channels.pop(task_id, None)
        self._task_projects.pop(task_id, None)

        return {"status": status, "summary": summary, "commit_hash": commit_hash, "pushed": pushed}

    def _finish_session_git(
        self,
        project_dir: str,
        branch: str,
        token: str,
        author_name: str,
        author_email: str,
        commit_message: str,
    ) -> tuple[str, bool, bool, list[str]]:
        commit_hash = ""
        commit_created = False
        pushed = False
        messages: list[str] = []
        try:
            git_ops.ensure_identity(project_dir, author_name, author_email)
            if git_ops.has_changes(project_dir):
                commit_hash = git_ops.commit_all(
                    project_dir,
                    commit_message,
                    author_name,
                    author_email,
                )
                if commit_hash:
                    commit_created = True
                    messages.append(
                        f"[git] commit {commit_hash[:8]}: {commit_message.splitlines()[0]}\n"
                    )
            else:
                messages.append("[git] keine Aenderungen zu committen\n")
            try:
                git_ops.push(project_dir, branch, token)
                pushed = True
                if not commit_hash:
                    commit_hash = git_ops.head_commit(project_dir)
                messages.append(f"[git] push -> origin/{branch} OK\n")
            except Exception as exc:  # noqa: BLE001
                messages.append(f"[git] push fehlgeschlagen: {exc}\n")
        except Exception as exc:  # noqa: BLE001
            messages.append(f"[git] Fehler: {exc}\n")
            commit_hash = ""
            commit_created = False
            pushed = False
        return commit_hash, commit_created, pushed, messages

    def _append_terminal_output(self, task_id: str, chunk: str) -> int:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                current = task.output or ""
                task.output = current + chunk
                return len(current)
        return 0

    def _get_terminal_output(self, task_id: str) -> str:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                return task.output or ""
        return ""

    def _session_result(self, task_id: str) -> dict:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task:
                return {
                    "status": task.status,
                    "summary": task.result_summary,
                    "commit_hash": task.commit_hash,
                    "pushed": task.pushed,
                }
        return {"status": "error", "summary": "", "commit_hash": "", "pushed": False}


# --------------------------------------------------------------------------- #
# Module singletons
# --------------------------------------------------------------------------- #

manager = TaskManager()
session_manager = SessionManager()


def reset_interrupted() -> None:
    """On startup, mark any task still 'running'/'queued' as 'interrupted' and
    clean up worktrees that interrupted runs left behind."""
    with session_scope() as db:
        rows = db.query(Task).filter(Task.status.in_(["running", "queued"])).all()
        for t in rows:
            t.status = "interrupted"
            if t.finished_at is None:
                t.finished_at = _now()
        project_dirs = [
            p.local_path for p in db.query(Project).all() if p.local_path
        ]
    # Drop the on-disk worktree dirs, then prune the now-stale admin entries.
    shutil.rmtree(_worktrees_root(), ignore_errors=True)
    # Drop throwaway per-task host-staging copies (per-project session copies are
    # kept so an interrupted session can still be resumed into the same cwd).
    host_staging.cleanup_task_staging()
    # Drop every stale host-visible lock file too: a crashed dashboard leaves
    # them behind, and on next start we want a clean slate so the host does
    # not see ghost "running" runs.
    for stale in host_lock.list_active():
        try:
            stale.unlink()
        except OSError:
            pass
    for project_dir in project_dirs:
        if Path(project_dir).exists():
            try:
                git_ops.prune_worktrees(project_dir)
            except Exception:  # noqa: BLE001
                pass
