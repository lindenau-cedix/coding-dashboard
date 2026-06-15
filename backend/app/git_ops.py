"""Local git operations via subprocess.

The GitHub token is never written to ``.git/config``.  For network operations
(clone / push) it is injected for that single invocation as an HTTP auth header.
All functions here are blocking; call them from a thread (``asyncio.to_thread``).
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _auth_args(token: str) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=Authorization: Basic {basic}"]


def _run(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    token: str | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if token:
        cmd += _auth_args(token)
    cmd += args
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "git command failed").strip())
    return proc


def clone(clone_url: str, dest: str | Path, token: str, branch: str | None = None) -> None:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    args += [clone_url, str(dest)]
    _run(args, token=token)


def ensure_identity(repo_dir: str | Path, name: str, email: str) -> None:
    _run(["config", "user.name", name], cwd=repo_dir)
    _run(["config", "user.email", email], cwd=repo_dir)


def current_branch(repo_dir: str | Path) -> str:
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir).stdout.strip()


def has_changes(repo_dir: str | Path) -> bool:
    return bool(_run(["status", "--porcelain"], cwd=repo_dir).stdout.strip())


def head_commit(repo_dir: str | Path) -> str:
    proc = _run(["rev-parse", "HEAD"], cwd=repo_dir, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def short_status(repo_dir: str | Path) -> str:
    return _run(["status", "--porcelain"], cwd=repo_dir, check=False).stdout.strip()


def commit_all(
    repo_dir: str | Path, message: str, author_name: str, author_email: str
) -> str | None:
    """Stage everything and commit. Returns the new commit hash, or None if clean."""
    if not has_changes(repo_dir):
        return None
    _run(["add", "-A"], cwd=repo_dir)
    _run(
        [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            message,
        ],
        cwd=repo_dir,
    )
    return head_commit(repo_dir)


def push(repo_dir: str | Path, branch: str, token: str) -> None:
    _run(["push", "origin", f"HEAD:{branch}"], cwd=repo_dir, token=token)


def pull(repo_dir: str | Path, branch: str, token: str) -> str:
    result = _run(["pull", "origin", branch], cwd=repo_dir, token=token, check=False)
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# Worktrees — isolated checkouts so several interactive sessions can run in
# parallel on the same repo without clobbering each other's files / index.
# --------------------------------------------------------------------------- #
def is_git_repo(path: str | Path) -> bool:
    """True if ``path`` is inside a git work tree (and has a valid HEAD)."""
    if not path or not Path(path).exists():
        return False
    try:
        inside = _run(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False
        # A repo with no commits has no HEAD to base a worktree on.
        return _run(["rev-parse", "--verify", "HEAD"], cwd=path, check=False).returncode == 0
    except OSError:
        return False


def add_worktree(repo_dir: str | Path, worktree_path: str | Path, ref: str = "HEAD") -> None:
    """Create a detached worktree of ``ref`` at ``worktree_path``.

    Detached on purpose: parallel session worktrees never own a branch, so git
    never refuses a second worktree for "branch already checked out", and the
    auto-commit step can still ``push HEAD:<default_branch>`` on exit. If a
    stale registration for the same path exists it is pruned and retried.
    """
    worktree_path = Path(worktree_path)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists() and any(worktree_path.iterdir()):
        # Already populated (e.g. resuming into a kept worktree): nothing to do.
        return
    args = ["worktree", "add", "--detach", str(worktree_path), ref]
    proc = _run(args, cwd=repo_dir, check=False)
    if proc.returncode != 0:
        # Most common cause: a previously-removed worktree dir still registered.
        _run(["worktree", "prune"], cwd=repo_dir, check=False)
        proc = _run(args, cwd=repo_dir, check=False)
        if proc.returncode != 0:
            raise GitError((proc.stderr or proc.stdout or "git worktree add failed").strip())


def remove_worktree(repo_dir: str | Path, worktree_path: str | Path) -> None:
    """Remove a worktree (force, since it may hold uncommitted/ignored files)."""
    _run(["worktree", "remove", "--force", str(worktree_path)], cwd=repo_dir, check=False)
    _run(["worktree", "prune"], cwd=repo_dir, check=False)


def prune_worktrees(repo_dir: str | Path) -> None:
    """Drop registrations for worktrees whose directories no longer exist."""
    _run(["worktree", "prune"], cwd=repo_dir, check=False)
