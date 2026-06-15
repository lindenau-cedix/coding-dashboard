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


def push_ref(repo_dir: str | Path, local_branch: str, remote_branch: str, token: str) -> None:
    """Push a named local branch (not HEAD) to a remote branch."""
    _run(["push", "origin", f"{local_branch}:{remote_branch}"], cwd=repo_dir, token=token)


def pull(repo_dir: str | Path, branch: str, token: str) -> str:
    result = _run(["pull", "origin", branch], cwd=repo_dir, token=token, check=False)
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# Worktrees & branch merging
#
# Concurrent tasks/sessions for one project each run in their own git worktree
# on a dedicated branch so they never clobber each other's working tree.  The
# project's main checkout (``local_path``) stays on the default branch and is
# only touched — briefly, under a per-project lock — to merge a finished branch
# back in and push.  Worktrees share the repo object store and refs, so a branch
# created in a worktree can be merged from the main checkout without a fetch.
# --------------------------------------------------------------------------- #

def branch_exists(repo_dir: str | Path, branch: str) -> bool:
    proc = _run(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_dir,
        check=False,
    )
    return proc.returncode == 0


def add_worktree(
    repo_dir: str | Path, worktree_dir: str | Path, branch: str, base_ref: str
) -> None:
    """Create a new worktree at ``worktree_dir`` on a fresh ``branch`` off ``base_ref``."""
    _run(["worktree", "add", "-b", branch, str(worktree_dir), base_ref], cwd=repo_dir)


def remove_worktree(repo_dir: str | Path, worktree_dir: str | Path) -> None:
    """Detach a worktree and prune its admin entry (best effort)."""
    _run(["worktree", "remove", "--force", str(worktree_dir)], cwd=repo_dir, check=False)
    _run(["worktree", "prune"], cwd=repo_dir, check=False)


def prune_worktrees(repo_dir: str | Path) -> None:
    _run(["worktree", "prune"], cwd=repo_dir, check=False)


def delete_branch(repo_dir: str | Path, branch: str, force: bool = False) -> None:
    _run(["branch", "-D" if force else "-d", branch], cwd=repo_dir, check=False)


def merge_branch(repo_dir: str | Path, branch: str, message: str) -> tuple[bool, str]:
    """Merge ``branch`` into the branch currently checked out in ``repo_dir``.

    Fast-forwards when possible (clean history for solo runs); otherwise creates
    a merge commit.  On conflict the merge is aborted and ``(False, output)`` is
    returned so the caller can keep/push the feature branch for manual
    resolution.  ``(True, output)`` on success (incl. "already up to date").
    """
    proc = _run(["merge", "--no-edit", "-m", message, branch], cwd=repo_dir, check=False)
    out = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode != 0:
        _run(["merge", "--abort"], cwd=repo_dir, check=False)
        return False, out
    return True, out
