"""Run an agent inside a host-shared *copy* of the project, then integrate back.

Some agents do not run on the same machine as the dashboard.  The flagship case
is the Docker deployment's Hermes: to avoid a second Hermes runtime in the
container (which duplicated cronjobs and paired-channel replies), the container
drives the *host's* ``hermes`` over SSH.  But then Hermes runs on the host and
cannot see the repos, which live in the dashboard's data dir (a Docker volume,
invisible to the host).

The bridge is a small *staging* directory bind-mounted into the container at an
IDENTICAL path on host and container (``settings.hermes_staging_dir``, under
``/tmp`` by default).  For each run the dashboard:

1. **copies** the project into a staging dir (``local_clone`` — an independent
   working copy with its own object store);
2. lets the agent edit it (the SSH command does ``cd {project_dir}`` into the
   *same* path on the host);
3. **integrates** the result back: commit in the staging copy, fetch that commit
   into the canonical repo as a feature branch, merge it into the default branch
   and push.  On a merge conflict the feature branch is pushed and kept so the
   user can resolve it manually and then *Pull* — the dashboard never force-pulls
   over a conflict.

One-shot tasks get a throwaway per-task staging dir (removed after integration);
interactive sessions share one stable per-project staging dir so Hermes
``--resume`` finds the same cwd again (Hermes keys saved conversations by the
directory they were started in).

This module is import-light and all functions are blocking (git/​shutil); call
them from a thread via ``asyncio.to_thread``.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import git_ops
from .config import get_settings


def staging_root() -> Path:
    return Path(get_settings().hermes_staging_dir).resolve()


def task_staging_dir(task_id: str) -> Path:
    """Throwaway staging copy for a one-shot task/goal run."""
    return staging_root() / "tasks" / task_id


def session_staging_dir(project_id: str) -> Path:
    """Stable per-project staging copy for interactive sessions.

    Stable so Hermes ``--resume`` lands in the same cwd; the path is recorded in
    ``Task.workdir`` as the durable record of "the folder this session ran in".
    """
    return staging_root() / "sessions" / project_id


def is_staging_dir(path: str | Path) -> bool:
    if not path:
        return False
    try:
        return str(Path(path).resolve()).startswith(str(staging_root()))
    except OSError:
        return False


def prepare_copy(project_dir: str | Path, dest: str | Path) -> None:
    """Create a fresh staging copy of ``project_dir`` at ``dest`` (replacing any)."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    git_ops.local_clone(project_dir, dest)


def ensure_session_copy(project_dir: str | Path, dest: str | Path, resume: bool) -> None:
    """Make sure ``dest`` holds a usable staging copy for a session.

    * ``resume`` and a valid copy already exists -> keep it untouched, so Hermes
      resumes against the working tree its saved conversation refers to.
    * otherwise -> (re)create a fresh copy from the current project HEAD.  Safe
      for a new session because the previous session integrated its work on end.
    """
    dest = Path(dest)
    if resume and git_ops.is_git_repo(dest):
        return
    prepare_copy(project_dir, dest)


def integrate(
    project_dir: str,
    staging_dir: str,
    feature_branch: str,
    base_branch: str,
    token: str,
    author_name: str,
    author_email: str,
    commit_message: str,
    first_line: str,
    *,
    cleanup: bool,
) -> dict:
    """Bring a staging copy's work back into the canonical repo.

    Commits the staging copy, fetches that commit into ``project_dir`` as
    ``feature_branch``, merges it into ``base_branch`` and pushes.  On conflict
    the feature branch is pushed and kept (no auto-pull) for a manual merge.

    Mirrors the shape of ``task_runner._merge_worktree_branch`` so callers handle
    the result identically.  Synchronous; call under the per-project lock so the
    canonical checkout is mutated by one run at a time.
    """
    commit_hash = ""
    commit_created = False
    pushed = False
    merge_state = ""
    messages: list[str] = []
    try:
        git_ops.ensure_identity(staging_dir, author_name, author_email)
        if git_ops.has_changes(staging_dir):
            commit_hash = (
                git_ops.commit_all(staging_dir, commit_message, author_name, author_email)
                or ""
            )
            if commit_hash:
                commit_created = True
                messages.append(
                    f"[git] commit {commit_hash[:8]} (Host-Arbeitskopie): {first_line}\n"
                )
        else:
            messages.append("[git] keine Aenderungen zu committen\n")

        # Pull the staging copy's HEAD into the canonical repo as a feature branch,
        # then reuse the normal merge-into-default-branch path.
        git_ops.fetch_into_branch(project_dir, staging_dir, feature_branch)
        merged, _out = git_ops.merge_branch(
            project_dir, feature_branch, f"Merge {feature_branch} (Coding Dashboard)"
        )
        if merged:
            merge_state = "merged"
            messages.append(f"[git] merge {feature_branch} -> {base_branch} OK\n")
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
                f"[git] Merge-Konflikt: {feature_branch} bleibt erhalten "
                f"(bitte manuell mergen, dann 'Pull')\n"
            )
            try:
                git_ops.push_ref(project_dir, feature_branch, feature_branch, token)
                messages.append(f"[git] Branch {feature_branch} -> origin gepusht\n")
                if not commit_hash:
                    commit_hash = git_ops.head_commit(staging_dir)
            except Exception as exc:  # noqa: BLE001
                messages.append(f"[git] Branch-Push fehlgeschlagen: {exc}\n")
    except Exception as exc:  # noqa: BLE001
        messages.append(f"[git] Fehler: {exc}\n")
    finally:
        if merge_state == "merged":
            git_ops.delete_branch(project_dir, feature_branch, force=True)
        if cleanup:
            shutil.rmtree(staging_dir, ignore_errors=True)
    return {
        "commit_hash": commit_hash,
        "commit_created": commit_created,
        "pushed": pushed,
        "merge_state": merge_state,
        "messages": messages,
    }


def cleanup_task_staging() -> None:
    """Drop all throwaway per-task staging dirs (startup recovery)."""
    shutil.rmtree(staging_root() / "tasks", ignore_errors=True)
