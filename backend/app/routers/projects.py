"""Project routes: create (new private repo) or import an existing repo."""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from .. import git_ops, github_client, uploads
from ..auth import get_current_user
from ..config import get_settings
from ..database import get_db
from ..models import Project, Task
from ..schemas import ProjectCreate, ProjectDetail, ProjectOut

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
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    return db.query(Project).order_by(Project.updated_at.desc()).all()


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
