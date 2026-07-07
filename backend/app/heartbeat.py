"""Dashboard heartbeat: auto-poll GitHub issues, auto-spawn Claude Code tasks.

The heartbeat is a single long-lived ``asyncio.Task`` started by ``lifespan``
in ``main.py``. Every ``settings.heartbeat_interval_seconds`` it wakes up,
walks every **active (non-archived)** project that has a GitHub repo,
asks GitHub for its open issues, and dispatches one task per issue the
dashboard has not already dispatched an agent for. Dispatched tasks go
through the same ``manager.submit()`` path as user-submitted tasks, so
they get the full automatic pipeline: host lock, auto-pull, run,
auto-commit, push, AGENTS.md maintenance, result summary.

Design notes
------------

- **Trigger scope:** per-project cooldown only counts SUCCESSFULLY
  finished heartbeat tasks. A failed/error/cancelled run does NOT start
  the cooldown, so a misfiring agent gets another chance next tick.
- **Dedup:** the ``heartbeat_seen`` table records every (project_id,
  issue_number) the heartbeat has CONSIDERED. A new row means "first
  time seen -> dispatch a task"; an existing row means "skip". The
  table is created by ``create_all()`` and lives in the dashboard DB.
- **Per-project opt-out:** ``Project.heartbeat_enabled`` defaults to
  True; the UI / API can flip it off without touching env vars.
- **Failure handling:** errors are recorded on the project
  (``last_heartbeat_status='error'`` + ``last_heartbeat_error``) and the
  loop advances to the next tick. No exponential backoff in v1.

The module exposes a single module-level singleton ``heartbeat``
(``HeartbeatRunner``) used by both ``main.lifespan`` and the
``/api/heartbeat/...`` router.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import github_client
from .config import get_agents_config, get_settings
from .database import session_scope
from .models import HeartbeatSeen, Project, Task
from .task_runner import manager

log = logging.getLogger("coding-dashboard.heartbeat")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(text: str, max_len: int = 40) -> str:
    """Tiny ASCII slug for the suggested branch name. Just enough to be
    human-readable; not a full slug library."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:max_len] or "issue"


# --------------------------------------------------------------------------- #
# HeartbeatRunner
# --------------------------------------------------------------------------- #
class HeartbeatRunner:
    """Owns the heartbeat background task + the in-process toggle."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._enabled_override: bool | None = None  # None = use settings
        self._running = False
        # ``asyncio.Lock`` so two simultaneous ticks can't double-spawn.
        self._tick_lock = asyncio.Lock()
        # Per-project semaphore: caps parallel GitHub polls per tick.
        self._project_semaphore: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        """Effective global enable flag: settings.heartbeat_enabled AND
        not explicitly disabled via set_enabled(False)."""
        if self._enabled_override is not None:
            return self._enabled_override
        return get_settings().heartbeat_enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled_override = value

    def set_enabled(self, value: bool) -> None:
        self._enabled_override = value

    async def start(self) -> None:
        """Spawn the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        settings = get_settings()
        self._project_semaphore = asyncio.Semaphore(settings.heartbeat_max_concurrent)
        self._task = asyncio.create_task(self._loop(), name="cd-heartbeat")
        log.info(
            "heartbeat: started (enabled=%s, interval=%ss, max_concurrent=%d)",
            self.enabled,
            settings.heartbeat_interval_seconds,
            settings.heartbeat_max_concurrent,
        )

    async def stop(self) -> None:
        """Cancel the background loop. Safe to call even if not started."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        log.info("heartbeat: stopped")

    # ------------------------------------------------------------------ #
    # Tick (synchronous trigger + background loop)
    # ------------------------------------------------------------------ #
    async def tick_now(self) -> dict[str, Any]:
        """Run one heartbeat tick. Returns a small summary dict so the
        HTTP trigger endpoint and the background loop share the same
        logic and report shape."""
        if self._tick_lock.locked():
            # Re-entry guard: a tick is already in flight. Return a hint.
            return {"status": "already_running"}
        async with self._tick_lock:
            return await self._tick()

    async def _loop(self) -> None:
        """The background loop. Sleeps between ticks; honors CancelledError."""
        try:
            while True:
                settings = get_settings()
                try:
                    if self.enabled and settings.github_token:
                        await self.tick_now()
                    else:
                        log.debug(
                            "heartbeat: tick skipped (enabled=%s, token=%s)",
                            self.enabled,
                            "set" if settings.github_token else "empty",
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("heartbeat: tick crashed: %s", exc)
                await asyncio.sleep(settings.heartbeat_interval_seconds)
        except asyncio.CancelledError:
            log.info("heartbeat: loop cancelled")
            raise

    async def _tick(self) -> dict[str, Any]:
        settings = get_settings()
        agents = get_agents_config()
        agent_key = settings.heartbeat_agent_key
        if agent_key not in agents.agents:
            log.warning(
                "heartbeat: configured agent_key=%r not in agents config; skipping tick",
                agent_key,
            )
            return {"status": "no_agent", "agent_key": agent_key}

        # List active projects in a thread (sync DB).
        projects = await asyncio.to_thread(self._list_active_projects)
        if not projects:
            return {"status": "no_projects", "dispatched": 0}

        # Fan out _process_project with a per-tick semaphore.
        if self._project_semaphore is None:
            self._project_semaphore = asyncio.Semaphore(settings.heartbeat_max_concurrent)

        async def _one(p_id: str) -> dict[str, Any]:
            assert self._project_semaphore is not None
            async with self._project_semaphore:
                return await self._process_project(p_id, agent_key)

        results = await asyncio.gather(
            *[_one(p.id) for p in projects], return_exceptions=True
        )

        dispatched = 0
        for r in results:
            if isinstance(r, dict) and r.get("dispatched"):
                dispatched += int(r["dispatched"])
            elif isinstance(r, Exception):
                log.warning("heartbeat: project tick raised: %s", r)

        log.info(
            "heartbeat: tick finished (projects=%d, dispatched=%d)",
            len(projects),
            dispatched,
        )
        return {"status": "ok", "projects": len(projects), "dispatched": dispatched}

    # ------------------------------------------------------------------ #
    # DB helpers (sync; called via to_thread)
    # ------------------------------------------------------------------ #
    def _list_active_projects(self) -> list[Project]:
        """Active (not archived) projects with a GitHub full name."""
        with session_scope() as db:
            rows = (
                db.query(Project)
                .filter(Project.archived.is_(False))
                .filter(Project.github_full_name != "")
                .all()
            )
            # Detach so the caller can use them outside the session.
            for p in rows:
                db.expunge(p)
            return rows

    def _in_cooldown(self, project_id: str, cooldown_minutes: int) -> bool:
        """True if a heartbeat-spawned task for this project reached
        ``success`` within the cooldown window."""
        cutoff = _now() - timedelta(minutes=cooldown_minutes)
        with session_scope() as db:
            row = (
                db.query(Task)
                .filter(Task.project_id == project_id)
                .filter(Task.heartbeat_spawned.is_(True))
                .filter(Task.status == "success")
                .filter(Task.finished_at.isnot(None))
                .filter(Task.finished_at >= cutoff)
                .order_by(Task.finished_at.desc())
                .first()
            )
            return row is not None

    def _claim_issue(
        self, project_id: str, issue_number: int, issue_title: str, issue_url: str
    ) -> bool:
        """Atomically insert into heartbeat_seen. Returns True iff the
        row was actually inserted (caller should dispatch)."""
        with session_scope() as db:
            existing = (
                db.query(HeartbeatSeen)
                .filter(HeartbeatSeen.project_id == project_id)
                .filter(HeartbeatSeen.issue_number == issue_number)
                .first()
            )
            if existing is not None:
                return False
            db.add(
                HeartbeatSeen(
                    project_id=project_id,
                    issue_number=issue_number,
                    issue_title=issue_title[:512],
                    issue_url=issue_url[:512],
                )
            )
            db.commit()
            return True

    def _record_dispatch(
        self, project_id: str, issue_number: int, task_id: str
    ) -> None:
        """Stamp the heartbeat_seen row with the dispatched task id."""
        with session_scope() as db:
            row = (
                db.query(HeartbeatSeen)
                .filter(HeartbeatSeen.project_id == project_id)
                .filter(HeartbeatSeen.issue_number == issue_number)
                .first()
            )
            if row is not None:
                row.dispatched_task_id = task_id
                db.commit()

    def _set_project_status(
        self,
        project_id: str,
        status: str,
        error: str = "",
        poll_at: datetime | None = None,
    ) -> None:
        """Stamp project-level heartbeat fields after a tick on it."""
        with session_scope() as db:
            p = db.get(Project, project_id)
            if p is None:
                return
            p.last_heartbeat_at = _now()
            p.last_heartbeat_status = status[:32]
            p.last_heartbeat_error = error
            if poll_at is not None:
                p.last_issue_poll_at = poll_at
            db.commit()

    # ------------------------------------------------------------------ #
    # Prompt
    # ------------------------------------------------------------------ #
    def _build_prompt(
        self, project: Project, issue: dict[str, Any], template: str
    ) -> str:
        labels = issue.get("labels") or []
        if isinstance(labels, list):
            labels_str = ", ".join(
                (l.get("name") if isinstance(l, dict) else str(l)) for l in labels
            ) or "(keine)"
        else:
            labels_str = str(labels)

        body = (issue.get("body") or "").strip() or "(keine Beschreibung)"
        user = (issue.get("user") or {}).get("login") or "unknown"
        number = issue.get("number")
        title = issue.get("title") or ""
        created_at = issue.get("created_at") or ""
        html_url = issue.get("html_url") or ""
        repo = project.github_full_name or ""
        slug = _slugify(title)

        try:
            return template.format(
                number=number,
                repo=repo,
                title=title,
                user=user,
                labels=labels_str,
                created_at=created_at,
                body=body,
                html_url=html_url,
                slug=slug,
            )
        except (KeyError, IndexError):
            # Template had unknown placeholders; fall back to the raw issue.
            return (
                f"{title}\n\nIssue #{number} ({html_url})\n\n{body}"
            )

    # ------------------------------------------------------------------ #
    # Per-project tick
    # ------------------------------------------------------------------ #
    async def _process_project(self, project_id: str, agent_key: str) -> dict[str, Any]:
        settings = get_settings()
        result: dict[str, Any] = {"project_id": project_id, "dispatched": 0}

        # Re-load the project (cheap, sync via to_thread).
        def _load() -> Project | None:
            with session_scope() as db:
                p = db.get(Project, project_id)
                if p is None:
                    return None
                db.expunge(p)
                return p

        project = await asyncio.to_thread(_load)
        if project is None:
            return result
        if project.archived or not project.github_full_name:
            return result
        if not project.heartbeat_enabled:
            await asyncio.to_thread(
                self._set_project_status, project_id, "disabled"
            )
            return result

        # Cooldown check (success-only).
        if await asyncio.to_thread(
            self._in_cooldown, project_id, settings.heartbeat_cooldown_minutes
        ):
            await asyncio.to_thread(
                self._set_project_status, project_id, "cooldown"
            )
            return result

        # Decide the ``since`` cutoff for GitHub.
        def _since_cutoff() -> str | None:
            with session_scope() as db:
                p = db.get(Project, project_id)
                if p is None or p.last_issue_poll_at is None:
                    return (
                        _now() - timedelta(hours=settings.heartbeat_lookback_hours)
                    ).isoformat()
                return p.last_issue_poll_at.isoformat()

        since = await asyncio.to_thread(_since_cutoff)

        # Fetch open issues (network).
        try:
            issues = await github_client.list_issues(
                project.github_full_name,
                state="open",
                labels=settings.heartbeat_labels_list or None,
                since=since,
            )
        except github_client.GitHubError as exc:
            log.warning(
                "heartbeat: %s GitHub error: %s", project.github_full_name, exc
            )
            await asyncio.to_thread(
                self._set_project_status, project_id, "error", error=str(exc)[:500]
            )
            return result
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "heartbeat: %s poll crashed: %s", project.github_full_name, exc
            )
            await asyncio.to_thread(
                self._set_project_status, project_id, "error", error=str(exc)[:500]
            )
            return result

        poll_at = _now()
        # Filter out PRs (issues API returns both).
        real_issues = [i for i in issues if not i.get("pull_request")]
        # Label filter (GitHub does OR, we additionally require at least one
        # match when labels are configured).
        if settings.heartbeat_labels_list:
            wanted = {l.lower() for l in settings.heartbeat_labels_list}
            real_issues = [
                i
                for i in real_issues
                if any(
                    (lbl.get("name", "").lower() in wanted)
                    for lbl in (i.get("labels") or [])
                    if isinstance(lbl, dict)
                )
            ]

        # For each unseen issue, claim + spawn.
        for issue in real_issues:
            number = issue.get("number")
            title = issue.get("title") or ""
            html_url = issue.get("html_url") or ""
            if number is None:
                continue
            new = await asyncio.to_thread(
                self._claim_issue, project_id, int(number), title, html_url
            )
            if not new:
                continue
            try:
                task_id = await self._spawn_task(project, issue, agent_key)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "heartbeat: spawn failed for %s#%s: %s",
                    project.github_full_name,
                    number,
                    exc,
                )
                continue
            await asyncio.to_thread(
                self._record_dispatch, project_id, int(number), task_id
            )
            result["dispatched"] += 1
            log.info(
                "heartbeat: dispatched task=%s for %s#%s (%s)",
                task_id,
                project.github_full_name,
                number,
                title[:80],
            )

        await asyncio.to_thread(
            self._set_project_status,
            project_id,
            "success" if result["dispatched"] else "no_issues",
            poll_at=poll_at,
        )
        return result

    # ------------------------------------------------------------------ #
    # Spawn
    # ------------------------------------------------------------------ #
    async def _spawn_task(
        self, project: Project, issue: dict[str, Any], agent_key: str
    ) -> str:
        settings = get_settings()
        prompt = self._build_prompt(project, issue, settings.heartbeat_prompt_template)

        def _create() -> Task:
            with session_scope() as db:
                t = Task(
                    project_id=project.id,
                    agent=agent_key,
                    prompt=prompt,
                    mode="task",
                    status="queued",
                    heartbeat_spawned=True,
                    heartbeat_issue_number=int(issue["number"]),
                )
                db.add(t)
                db.commit()
                db.refresh(t)
                # Detach so we can read .id outside the session.
                db.expunge(t)
                return t

        task = await asyncio.to_thread(_create)
        # Fire-and-forget submit; the task runs in the background.
        manager.submit(task.id, project.id)
        return task.id


# --------------------------------------------------------------------------- #
# Module singleton
# --------------------------------------------------------------------------- #
heartbeat = HeartbeatRunner()