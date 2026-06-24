"""Project routes: create (new private repo) or import an existing repo."""
from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Literal

from .. import git_ops, github_client, uploads
from ..auth import get_current_user
from ..config import get_settings
from ..database import get_db
from ..models import Project, Task
from ..schemas import (
    DirListing,
    FileContent,
    FileEntry,
    ProjectCreate,
    ProjectDetail,
    ProjectOut,
)

router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    dependencies=[Depends(get_current_user)],
)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip().lower()).strip("-._")
    return s or "project"


def _unique_slug(db: Session, base: str) -> str:
    slug = base
    i = 2
    while db.query(Project).filter(Project.slug == slug).first() is not None:
        slug = f"{base}-{i}"
        i += 1
    return slug


def _parse_full_name(repo: str) -> str:
    repo = repo.strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo.startswith("http://") or repo.startswith("https://"):
        if "github.com/" in repo:
            repo = repo.split("github.com/", 1)[1]
    elif repo.startswith("git@"):
        if ":" in repo:
            repo = repo.split(":", 1)[1]
    return repo.strip("/")


@router.get("", response_model=list[ProjectOut])
def list_projects(
    archived: Literal["all", "true", "false"] = Query("false"),
    db: Session = Depends(get_db),
) -> list[Project]:
    """List projects.

    - ``archived=false`` (default) returns only ACTIVE projects; archived
      ones are hidden to keep the start page focused on live work.
    - ``archived=true`` returns only ARCHIVED projects (the "Archiv"
      view of the start page).
    - ``archived=all`` returns everything (handy for admin / debugging).

    Order is by ``updated_at DESC`` so freshly-touched projects surface first.
    """
    q = db.query(Project).order_by(Project.updated_at.desc())
    if archived == "true":
        q = q.filter(Project.archived.is_(True))
    elif archived == "false":
        q = q.filter(Project.archived.is_(False))
    return q.all()


# --------------------------------------------------------------------------- #
# GitHub browse + bulk import (autoclone all of the user's repos)
# --------------------------------------------------------------------------- #

class GithubRepoOut(BaseModel):
    """A repo from GitHub (subset of fields the UI needs)."""

    full_name: str
    name: str
    description: str = ""
    private: bool
    clone_url: str
    html_url: str
    default_branch: str
    fork: bool
    archived: bool
    already_imported: bool = False


class GithubListResponse(BaseModel):
    repos: list[GithubRepoOut]
    user: str = ""


@router.get("/from-github", response_model=GithubListResponse)
async def list_from_github(db: Session = Depends(get_db)) -> GithubListResponse:
    """List every repo visible to the GitHub token.

    The frontend uses this to render the "Sync from GitHub" preview: each
    entry is flagged with ``already_imported`` so the UI can preselect the
    not-yet-imported ones for bulk clone.
    """
    settings = get_settings()
    if not settings.github_token:
        raise HTTPException(503, "GitHub-Token nicht konfiguriert (CD_GITHUB_TOKEN).")
    existing_full_names = {
        (p.github_full_name or "").lower() for p in db.query(Project).all()
    }
    owner = settings.github_owner.strip()
    try:
        user = await github_client.get_authenticated_user() if not owner else {}
        repos = await github_client.list_user_repos(
            org=owner if owner.lower() != str(user.get("login", "")).lower() else ""
        )
    except github_client.GitHubError as exc:
        code = exc.status_code if 400 <= exc.status_code < 500 else 502
        raise HTTPException(code, f"GitHub: {exc.message}")
    out = [
        GithubRepoOut(
            full_name=r.get("full_name", ""),
            name=r.get("name", ""),
            description=r.get("description") or "",
            private=bool(r.get("private")),
            clone_url=r.get("clone_url", ""),
            html_url=r.get("html_url", ""),
            default_branch=r.get("default_branch") or settings.default_branch,
            fork=bool(r.get("fork")),
            archived=bool(r.get("archived")),
            already_imported=r.get("full_name", "").lower() in existing_full_names,
        )
        for r in repos
    ]
    return GithubListResponse(repos=out, user=str(user.get("login", "")) if user else "")


class SyncFromGithubRequest(BaseModel):
    """Selective bulk-import: ``full_names`` lists the repos to clone.

    An empty list = "everything not already imported" (the default behaviour
    the "Sync all" button triggers).
    """

    full_names: list[str] = Field(default_factory=list)
    include_forks: bool = True
    include_archived: bool = True


class SyncFromGithubResult(BaseModel):
    full_name: str
    status: Literal["imported", "skipped", "failed"]
    detail: str = ""
    project_id: str = ""


class SyncFromGithubResponse(BaseModel):
    results: list[SyncFromGithubResult]
    imported: int = 0
    skipped: int = 0
    failed: int = 0


@router.post("/sync-from-github", response_model=SyncFromGithubResponse)
async def sync_from_github(
    body: SyncFromGithubRequest, db: Session = Depends(get_db)
) -> SyncFromGithubResponse:
    """Clone every (or every selected) GitHub repo that is not yet imported.

    Idempotent: already-imported repos are reported as ``skipped`` rather
    than erroring, so re-running the sync after a partial outage picks up
    where it left off.  Per-repo failures are isolated — one bad clone does
    not abort the rest of the batch.
    """
    settings = get_settings()
    if not settings.github_token:
        raise HTTPException(503, "GitHub-Token nicht konfiguriert (CD_GITHUB_TOKEN).")
    existing_full_names = {
        (p.github_full_name or "").lower(): p for p in db.query(Project).all()
    }

    owner = settings.github_owner.strip()
    try:
        me = await github_client.get_authenticated_user() if not owner else {}
        org = (
            owner
            if owner.lower() != str(me.get("login", "")).lower()
            else ""
        )
        remote_repos = await github_client.list_user_repos(
            org=org, include_forks=body.include_forks
        )
    except github_client.GitHubError as exc:
        code = exc.status_code if 400 <= exc.status_code < 500 else 502
        raise HTTPException(code, f"GitHub: {exc.message}")

    wanted = {n.strip().lower() for n in body.full_names if n.strip()}
    results: list[SyncFromGithubResult] = []
    imported = skipped = failed = 0

    for r in remote_repos:
        full_name = (r.get("full_name") or "").strip()
        if not full_name:
            continue
        if not body.include_archived and r.get("archived"):
            results.append(SyncFromGithubResult(full_name=full_name, status="skipped", detail="archived"))
            skipped += 1
            continue
        if wanted and full_name.lower() not in wanted:
            continue
        if full_name.lower() in existing_full_names:
            proj = existing_full_names[full_name.lower()]
            results.append(
                SyncFromGithubResult(
                    full_name=full_name,
                    status="skipped",
                    detail="bereits importiert",
                    project_id=proj.id,
                )
            )
            skipped += 1
            continue

        result = await _import_single_repo(db, settings, r)
        results.append(result)
        if result.status == "imported":
            imported += 1
            # Refresh local cache so a duplicate full_name later in the
            # batch is recognised as skipped rather than racing the DB.
            existing_full_names[full_name.lower()] = db.get(Project, result.project_id)
        else:
            failed += 1

    return SyncFromGithubResponse(
        results=results, imported=imported, skipped=skipped, failed=failed
    )


async def _import_single_repo(
    db: Session, settings, repo_meta: dict
) -> SyncFromGithubResult:
    """Clone one repo into ``data_dir/projects/<slug>`` and persist a Project row.

    Mirrors the create/import flow used by ``POST /projects`` so the result
    is indistinguishable from a manual import.  All errors are caught and
    returned as ``SyncFromGithubResult(status="failed", detail=...)`` — the
    caller iterates over many repos and one bad apple must not abort the
    batch.
    """
    full_name = repo_meta.get("full_name", "")
    name = repo_meta.get("name") or full_name.split("/")[-1]
    try:
        slug = _unique_slug(db, _slugify(name))
        local_path = settings.projects_dir / slug
        if local_path.exists() and any(local_path.iterdir()):
            return SyncFromGithubResult(
                full_name=full_name,
                status="skipped",
                detail=f"lokales Verzeichnis existiert bereits: {local_path}",
            )
        clone_url = repo_meta.get("clone_url") or ""
        default_branch = repo_meta.get("default_branch") or settings.default_branch
        if not clone_url:
            return SyncFromGithubResult(
                full_name=full_name, status="failed", detail="keine clone_url von GitHub"
            )
        try:
            await asyncio.to_thread(
                git_ops.clone, clone_url, local_path, settings.github_token, default_branch
            )
            await asyncio.to_thread(
                git_ops.ensure_identity,
                local_path,
                settings.git_author_name,
                settings.git_author_email,
            )
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(local_path, ignore_errors=True)
            return SyncFromGithubResult(
                full_name=full_name, status="failed", detail=f"Clone fehlgeschlagen: {exc}"
            )
        project = Project(
            name=name,
            slug=slug,
            description=repo_meta.get("description") or "",
            github_full_name=full_name,
            github_url=repo_meta.get("html_url") or "",
            clone_url=clone_url,
            local_path=str(local_path),
            default_branch=default_branch,
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        return SyncFromGithubResult(
            full_name=full_name, status="imported", project_id=project.id
        )
    except Exception as exc:  # noqa: BLE001
        return SyncFromGithubResult(
            full_name=full_name, status="failed", detail=str(exc)
        )


@router.post("", response_model=ProjectDetail, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    settings = get_settings()
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name darf nicht leer sein.")
    if not settings.github_token:
        raise HTTPException(503, "GitHub-Token nicht konfiguriert (CD_GITHUB_TOKEN).")

    slug = _unique_slug(db, _slugify(name))
    local_path = settings.projects_dir / slug
    if local_path.exists() and any(local_path.iterdir()):
        raise HTTPException(409, f"Zielverzeichnis existiert bereits: {local_path}")

    try:
        if body.mode == "create":
            owner = settings.github_owner.strip()
            org = ""
            if owner:
                me = await github_client.get_authenticated_user()
                if owner.lower() != str(me.get("login", "")).lower():
                    org = owner
            repo = await github_client.create_repo(
                slug,
                private=body.private,
                description=body.description,
                auto_init=True,
                org=org,
            )
        else:
            full = _parse_full_name(body.repo)
            if "/" not in full:
                raise HTTPException(400, "Bitte 'owner/repo' oder eine GitHub-URL angeben.")
            repo = await github_client.get_repo(full)
    except github_client.GitHubError as exc:
        code = exc.status_code if 400 <= exc.status_code < 500 else 502
        detail = f"GitHub: {exc.message}"
        if exc.status_code in (401, 403):
            if body.mode == "create":
                detail += (
                    " - Der GitHub-Token darf keine Repositories anlegen. Benoetigt wird "
                    "'repo'-Scope (klassischer Token) bzw. fein-granular die Berechtigung "
                    "'Administration: Read and write' fuer den passenden Owner mit Zugriff "
                    "auf 'All repositories'. Token mit diesen Rechten neu erstellen und "
                    "CD_GITHUB_TOKEN aktualisieren."
                )
            else:
                detail += (
                    " - Der GitHub-Token hat keinen Zugriff auf dieses Repository "
                    "(fein-granular: 'Contents: Read' und Zugriff auf das Repo noetig)."
                )
        raise HTTPException(code, detail)

    full_name = repo.get("full_name", "")
    clone_url = repo.get("clone_url", "")
    html_url = repo.get("html_url", "")
    default_branch = repo.get("default_branch") or settings.default_branch
    if not clone_url:
        raise HTTPException(502, "GitHub lieferte keine clone_url zurueck.")

    try:
        await asyncio.to_thread(
            git_ops.clone, clone_url, local_path, settings.github_token, default_branch
        )
        await asyncio.to_thread(
            git_ops.ensure_identity, local_path, settings.git_author_name, settings.git_author_email
        )
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(local_path, ignore_errors=True)
        raise HTTPException(502, f"Clone fehlgeschlagen: {exc}")

    project = Project(
        name=name,
        slug=slug,
        description=body.description or repo.get("description") or "",
        github_full_name=full_name,
        github_url=html_url,
        clone_url=clone_url,
        local_path=str(local_path),
        default_branch=default_branch,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    return project


@router.get("/{project_id}/agents-md")
def get_agents_md(project_id: str, db: Session = Depends(get_db)) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    path = Path(project.local_path) / "AGENTS.md"
    if not path.exists():
        return {"exists": False, "content": ""}
    return {"exists": True, "content": path.read_text(encoding="utf-8", errors="replace")}


# --------------------------------------------------------------------------- #
# File browser
# --------------------------------------------------------------------------- #

# Directory entries never shown in the browser.
_FILE_BROWSER_HIDDEN = {".git"}
# Read endpoint limits: refuse to inline anything larger, clip text at this size.
_MAX_TEXT_BYTES = 512 * 1024


def _project_root(db: Session, project_id: str) -> Path:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if not project.local_path:
        raise HTTPException(409, "Kein lokales Repo vorhanden.")
    root = Path(project.local_path).resolve()
    if not root.is_dir():
        raise HTTPException(409, "Projektverzeichnis nicht gefunden.")
    return root


def _resolve_within(root: Path, rel: str) -> Path:
    """Resolve a client-supplied relative path inside ``root`` (no traversal)."""
    rel = (rel or "").strip().lstrip("/")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "Pfad liegt außerhalb des Projekts.")
    return target


@router.get("/{project_id}/files", response_model=DirListing)
def list_files(
    project_id: str, path: str = Query(default=""), db: Session = Depends(get_db)
) -> DirListing:
    """List one directory of the project's working tree (relative to the root)."""
    root = _project_root(db, project_id)
    target = _resolve_within(root, path)
    if not target.is_dir():
        raise HTTPException(404, "Verzeichnis nicht gefunden.")
    entries: list[FileEntry] = []
    try:
        children = sorted(
            target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except OSError as exc:
        raise HTTPException(500, f"Verzeichnis nicht lesbar: {exc}")
    for child in children:
        if child.name in _FILE_BROWSER_HIDDEN:
            continue
        is_dir = child.is_dir()
        try:
            size = 0 if is_dir else child.stat().st_size
        except OSError:
            size = 0
        entries.append(
            FileEntry(
                name=child.name,
                path=child.relative_to(root).as_posix(),
                is_dir=is_dir,
                size=size,
            )
        )
    return DirListing(path=target.relative_to(root).as_posix() if target != root else "", entries=entries)


@router.get("/{project_id}/file", response_model=FileContent)
def read_file(
    project_id: str, path: str = Query(...), db: Session = Depends(get_db)
) -> FileContent:
    """Return the (text) content of one file for the side-by-side viewer."""
    root = _project_root(db, project_id)
    target = _resolve_within(root, path)
    if not target.is_file():
        raise HTTPException(404, "Datei nicht gefunden.")
    rel = target.relative_to(root).as_posix()
    try:
        size = target.stat().st_size
        # Read at most one chunk + 1 byte; never pull a huge file into memory.
        with target.open("rb") as fh:
            raw = fh.read(_MAX_TEXT_BYTES + 1)
    except OSError as exc:
        raise HTTPException(403, f"Datei nicht lesbar: {exc}")
    truncated = size > _MAX_TEXT_BYTES or len(raw) > _MAX_TEXT_BYTES
    chunk = raw[:_MAX_TEXT_BYTES]
    # A NUL byte in the first chunk is a reliable binary signal.
    if b"\x00" in chunk:
        return FileContent(path=rel, size=size, is_binary=True, truncated=truncated, content="")
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = chunk.decode("latin-1")
        except UnicodeDecodeError:
            return FileContent(path=rel, size=size, is_binary=True, truncated=truncated, content="")
    return FileContent(path=rel, size=size, is_binary=False, truncated=truncated, content=text)


# --------------------------------------------------------------------------- #
# Archive / unarchive (hide from the start page without losing history)
# --------------------------------------------------------------------------- #

@router.post("/{project_id}/archive", response_model=ProjectDetail)
def archive_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    """Mark a project as archived.

    The repo, its worktrees and the entire task history stay intact. The
    project simply disappears from the default project list (and from
    ``GET /api/running``-style overviews) until unarchived again.
    Running tasks / open sessions are NOT stopped — archiving is a UI
    concern, not a teardown. ``archived_at`` is set so the UI can show
    "Archiviert am …" in the archive view.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if project.archived:
        # Idempotent: archiving an already-archived project is a no-op.
        return project
    project.archived = True
    project.archived_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/unarchive", response_model=ProjectDetail)
def unarchive_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    """Reverse of archive: the project reappears in the default list."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if not project.archived:
        return project
    project.archived = False
    project.archived_at = None
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    delete_remote: bool = Query(False),
    db: Session = Depends(get_db),
) -> Response:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if delete_remote and project.github_full_name:
        try:
            await github_client.delete_repo(project.github_full_name)
        except github_client.GitHubError as exc:
            raise HTTPException(502, f"GitHub: {exc.message}")
    if project.local_path:
        shutil.rmtree(project.local_path, ignore_errors=True)
    # Remove any isolated per-session worktrees created for parallel sessions.
    worktrees_dir = get_settings().data_dir.resolve() / "session_worktrees" / project_id
    shutil.rmtree(worktrees_dir, ignore_errors=True)
    for (task_id,) in db.query(Task.id).filter(Task.project_id == project_id):
        uploads.delete_images(task_id)
    db.delete(project)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/pull", status_code=status.HTTP_200_OK)
async def pull_project(project_id: str, db: Session = Depends(get_db)) -> dict:
    """Fetch and merge remote changes into the local repo."""
    settings = get_settings()
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Projekt nicht gefunden.")
    if not project.local_path:
        raise HTTPException(409, "Kein lokales Repo vorhanden.")
    try:
        output = await asyncio.to_thread(git_ops.pull, project.local_path, project.default_branch, settings.github_token)
    except git_ops.GitError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "branch": project.default_branch, "output": output}
