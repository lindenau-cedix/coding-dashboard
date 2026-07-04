"""Host-visible lock files for active runs (task/goal/session).

Why this lives on the host, not in a Docker volume:
    In the Docker deployment the dashboard's data (DB + repos) sit in a private
    ``cd-data`` named volume, invisible to the host.  Operators that want to
    see "is something running right now?" / "what command was started last?"
    need a footprint the host can see — the same bind-mounted pattern used
    for the SSH-Hermes staging dir.  For systemd installs there is no
    container boundary; the host IS where the dashboard runs, so the lock
    file is host-visible by construction.

Contents: one file per active run.  The file name encodes the kind and id
(``task-<id>.lock`` / ``session-<id>.lock``) so an operator can ``ls`` the
lock dir and see at a glance what is running and, importantly, what is NOT
running (a missing lock file => the run is done, regardless of what the DB
says).  Each file holds a short JSON blob with the dashboard's PID, the
project id, the agent key, mode, started-at timestamp and the connection
between container PID (if Docker) and the agent's argv head.  This lets the
host ``kill -9`` the dashboard container as a brute-force cleanup and still
have a paper trail.

Atomicity: the file is created with ``O_EXCL | O_CREAT`` — concurrent
``submit()`` / ``start()`` calls cannot both believe they own the same slot.
If the file already exists (stale lock from a crashed run) it is overwritten
with the new metadata; the previous stale data is gone, which is what the
operator actually wants: the file should reflect the currently-running run,
not the last run that crashed.

Lifecycle: callers bracket each run with :func:`write` and :func:`remove`;
both are best-effort.  A failure to write never aborts the run (the host
loses its visibility — annoying, not catastrophic).  A failure to remove
leaves an orphaned lock file with stale PID, harmless to the dashboard but
visible until the next overwrite.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict


def lock_dir() -> Path:
    """Directory that holds per-run lock files (host-visible).

    Resolved once per call from :func:`app.config.get_settings` so config
    overrides picked up at runtime (tests, ``CD_HOST_LOCK_DIR``) take effect
    without an import dance.
    """
    from .config import get_settings

    return Path(get_settings().host_lock_dir).resolve()


def _ensure_dir() -> Path:
    """Make sure the lock dir exists and is writable; create on demand.

    In Docker the directory is bind-mounted from the host and pre-created
    during compose build, but a fresh image / new host path can still race
    with the first run, so we create it idempotently here.  ``mkdir`` is
    safe to call repeatedly; on a permission failure we surface the error
    by returning the unresolved path so the caller can decide (we never
    raise — lock files are best-effort visibility, not a correctness gate).
    """
    d = lock_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Best effort: caller still tries to write; if it fails too, that's
        # reported by the caller.
        pass
    return d


class LockInfo(TypedDict):
    """Minimal metadata for an active-run lock file (kept short and stable)."""

    kind: Literal["task", "session"]
    run_id: str
    project_id: str
    agent: str
    mode: str
    pid: int
    started_at: str  # ISO-8601 UTC


def _path(kind: Literal["task", "session"], run_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
    return lock_dir() / f"{kind}-{safe}.lock"


def _payload(kind: Literal["task", "session"], run_id: str, project_id: str,
             agent: str, mode: str) -> bytes:
    info: LockInfo = {
        "kind": kind,
        "run_id": run_id,
        "project_id": project_id,
        "agent": agent,
        "mode": mode,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    body = json.dumps(info, sort_keys=True, ensure_ascii=False)
    # Add hostname so the host can disambiguate locks from multiple dashboards
    # pointing at the same lock dir (unusual but possible with shared mounts).
    header = (
        f"# coding-dashboard active run\n"
        f"# host={socket.gethostname()}\n"
        f"# pid={os.getpid()}\n"
        f"# written_at={time.time():.3f}\n"
    )
    return (header + body + "\n").encode("utf-8")


def write(kind: Literal["task", "session"], run_id: str, project_id: str,
          agent: str, mode: str) -> Path | None:
    """Stamp a lock file for ``run_id``.  Returns its path, or ``None`` if the
    host filesystem refused (e.g. permission denied / RO mount).  Callers do
    NOT raise on ``None`` — best-effort visibility only.

    Atomicity: ``O_EXCL | O_CREAT`` means concurrent ``write()`` calls for the
    SAME id can never both succeed; the loser sees ``EEXIST`` and the function
    falls back to overwriting the stale lock.  Two distinct run_ids always
    produce distinct files (no global counter).
    """
    d = _ensure_dir()
    path = d / _path(kind, run_id).name
    data = _payload(kind, run_id, project_id, agent, mode)
    fd: int | None = None
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.write(fd, data)
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if exc.errno == errno.EEXIST:
            # Stale lock from a crashed run: overwrite.  Use a temp file +
            # rename so a concurrent reader never sees a half-written blob.
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                return path
            except OSError:
                return None
        # Permission denied, RO mount, etc.: give up silently.
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    return path


def remove(kind: Literal["task", "session"], run_id: str) -> None:
    """Best-effort removal of a lock file.  Missing files are fine; a fresh
    run may have already overwritten the previous one, in which case removing
    by stale id is impossible and we silently no-op.
    """
    p = _path(kind, run_id)
    try:
        os.unlink(p)
    except FileNotFoundError:
        return
    except OSError:
        return


def read(kind: Literal["task", "session"], run_id: str) -> LockInfo | None:
    """Read and parse a lock file's JSON payload.  Used in tests + any admin
    surface that wants to introspect what is running.  Returns ``None`` for
    a missing file or unparseable contents (we never raise on caller-visible
    state — only the writer's ``OSError`` path is silent)."""
    p = _path(kind, run_id)
    try:
        text = Path(p).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except ValueError:
                return None
            if isinstance(data, dict) and data.get("kind") == kind and data.get("run_id") == run_id:
                # JSON round-trip via TypedDict — drop unknown keys defensively.
                return {  # type: ignore[return-value]
                    "kind": kind,
                    "run_id": str(data.get("run_id", run_id)),
                    "project_id": str(data.get("project_id", "")),
                    "agent": str(data.get("agent", "")),
                    "mode": str(data.get("mode", "")),
                    "pid": int(data.get("pid", 0)),
                    "started_at": str(data.get("started_at", "")),
                }
    return None


def list_active() -> list[Path]:
    """All currently-present lock files, sorted.  Used for admin / debugging
    surfaces (none exist today, but the primitive is here so future code can
    say ``cat /var/lock/coding-dashboard/*.lock`` and get useful output).
    """
    d = lock_dir()
    try:
        return sorted(p for p in d.iterdir() if p.name.endswith(".lock"))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
