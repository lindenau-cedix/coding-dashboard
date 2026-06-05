"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .config import get_agents_config, get_settings
from .database import init_db
from .routers import auth as auth_router
from .routers import projects as projects_router
from .routers import tasks as tasks_router
from .routers import ws as ws_router
from .task_runner import reset_interrupted

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("coding-dashboard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    reset_interrupted()
    cfg = get_agents_config()
    log.info("Agents konfiguriert: %s", ", ".join(cfg.agents.keys()) or "(keine)")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router.router, prefix="/api")
    app.include_router(projects_router.router, prefix="/api")
    app.include_router(tasks_router.router, prefix="/api")
    app.include_router(ws_router.router, prefix="/api")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "app": settings.app_name}

    _mount_spa(app, settings.frontend_dist)
    return app


def _mount_spa(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA as a fallback (handy without nginx)."""
    base = Path(dist)
    if not base.is_dir():
        log.info("Frontend-Dist nicht gefunden (%s) -- SPA wird nicht ausgeliefert.", base)
        return
    base = base.resolve()
    index = base / "index.html"

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> FileResponse:
        target = (base / full_path).resolve()
        if full_path and str(target).startswith(str(base)) and target.is_file():
            return FileResponse(target)
        return FileResponse(index)

    log.info("SPA wird ausgeliefert aus %s", base)


app = create_app()
