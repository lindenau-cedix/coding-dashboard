# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Coding Dashboard is a self-hosted web and Android dashboard for delegating repository tasks to Claude Code, Hermes, or Codex. It creates or imports projects, runs agents with live output, records history, and automatically commits and pushes agent changes.

This repository contains application code and deployment scripts; it is not auto-deployed. Read [`README.md`](./README.md) for the operator-facing systemd, Docker Compose, nginx/TLS, Android, and production configuration procedures. `CLAUDE.md` is a symlink to the tracked [`AGENTS.md`](./AGENTS.md); edit `AGENTS.md` when changing this contributor guidance. Use `git log -p -- AGENTS.md` for its history rather than adding a run journal here. The `AGENTS.md` files inside managed project repositories are runtime handoff documents and are separate from this root guide.

## Common commands

Backend (Python 3.10–3.12, FastAPI) — from `backend/`:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

For local agent definitions, copy `deploy/config.example.yaml` to `backend/config.yaml` before starting the backend. Configure the required `CD_*` environment variables; `python -m app.cli hash-password 'password'` generates an admin password hash when authentication is desired.

Frontend (React 18 + Vite + TypeScript + Tailwind v4) — from `frontend/`:

```bash
npm install
npm run dev          # http://localhost:5173; proxies REST and WebSocket /api to :8000
npm run build        # production bundle in frontend/dist
npm run typecheck    # tsc --noEmit; required for frontend changes
npm run preview      # preview the built bundle
npm run cap:sync     # sync Capacitor Android project
npm run cap:open     # open Android project in Android Studio
```

Tests use one self-contained script, not pytest or another test runner:

```bash
cd backend && .venv/bin/python tests/smoke.py
```

The smoke script prints `[PASS]/[FAIL]` and exercises parsers, subprocess execution, REST/API flows, git integration, tasks, sessions, heartbeat, env profiles, and host-runner behavior. There is no per-test selector; while iterating, temporarily comment out calls in `smoke.py:main()` to focus on a section. There is no configured linter (no Ruff, ESLint, or frontend test script).

For deployment, service management, Docker, and Android commands, use the authoritative examples in [`README.md`](./README.md), rather than duplicating them here. Do not run destructive volume-removal commands unless explicitly intended; `docker compose down -v` deletes application state.

## Architecture

The application has three layers:

- `backend/` is a FastAPI application. `main.py` creates the app, installs CORS and routers, serves the built SPA when available, and starts/stops the heartbeat in its lifespan. Routers translate REST requests into database operations or task/session manager calls.
- `frontend/` is a React SPA. `src/api.ts` owns REST and WebSocket URL/token plumbing, `auth.tsx` owns login state, pages compose the product views, and components render task consoles, PTY terminals, file browsing, and shared UI. `frontend/vite.config.ts` uses a relative bundle base and proxies `/api` (including WebSockets) to the backend during development.
- `deploy/` contains systemd/Docker/Android tooling. Docker builds the frontend and backend into one runtime image; its entrypoint generates the runtime agent configuration and launches Uvicorn. Runtime state is normally under `/var/lib/coding-dashboard/` and runtime configuration under `/etc/coding-dashboard/` in server deployments.

### Request and execution flow

1. Project routes create/import repositories through `github_client.py` and `git_ops.py`, then persist project metadata with SQLAlchemy models in SQLite.
2. `POST /api/projects/{id}/tasks` validates the selected agent, mode, runner, model, effort, env profile, and images, then calls the singleton `TaskManager` in `task_runner.py`.
3. `TaskManager` serializes work per project, creates an isolated worktree or host-staging copy, invokes `agents.run_agent()` as a subprocess, and publishes output through a task WebSocket channel. Late/reconnected clients receive state from the in-memory channel/DB row.
4. On completion, the manager stores the result, updates commit/push/merge metadata, and performs the automatic commit and push. Goal mode is a task-shaped run with a goal wrapper; session mode and interactive task/goal use `SessionManager`, a real PTY, and the session WebSocket.
5. `heartbeat.py` runs as one lifespan-owned background task. It polls eligible GitHub projects, deduplicates issues in `heartbeat_seen`, and dispatches newly seen issues through the same `TaskManager` pipeline. Runtime agent and env-profile overrides are exposed by heartbeat routes.
6. `agents.py` parses each CLI's stream format (`claude-json`, raw, or Codex), while `config.py` loads `AgentSpec` definitions from YAML and `config_bootstrap.py` generates/backfills built-in and `<base>-host` variants. The frontend's agent and effort selectors are data-driven from `/api/agents`.

### Durable state and boundaries

- `database.py` creates tables with SQLAlchemy `create_all()` and performs only additive SQLite upgrades listed in `_SQLITE_COLUMN_ADDITIONS`; there is no Alembic migration system.
- `models.py` holds `Project`, `Task`, `HeartbeatSeen`, and `EnvProfile`. Repository files and task images live outside the database under the configured data directory.
- `env_crypto.py` encrypts env-profile Anthropic tokens with Fernet derived from `CD_SECRET_KEY`; API responses are write-only/redacted.
- `git_ops.py` is synchronous/blocking. Any call from async request or manager code must run via `asyncio.to_thread`.
- GitHub tokens are supplied per operation as an HTTP extra header and must not be persisted in repository git configuration.

## Load-bearing conventions

- Routers that contain WebSocket routes must not have router-level `HTTPBearer`/`Depends(get_current_user)` dependencies. Declare auth on HTTP route signatures; WebSockets authenticate with the token query parameter and `user_from_token()`.
- `CD_CORS_ORIGINS=*` is implemented by reflecting the concrete origin with `allow_origin_regex`, because credentialed CORS cannot return a literal `*`.
- Agents are intentionally autonomous (`--dangerously-skip-permissions`, Hermes headless mode, Codex non-interactive) and can modify, commit, and push private repositories. Run the service as a normal user, not root, and use dedicated credentials.
- The dashboard instructs each managed repository's `AGENTS.md` to carry a short `Letzter Durchlauf` handoff block. The instruction is defined in `backend/app/config.py`; do not confuse those per-project files with this root guide.
- Agent selection is configuration-driven. Existing built-in-style YAML files are backfilled by `load_agents_config()`; custom agent sets are not silently augmented. The generated base agent must precede its `<base>-host` sibling because `/api/agents` order controls the frontend default.
- When adding an optional `${CD_*}` variable to `deploy/*.sh` or `deploy/docker/entrypoint.sh`, initialize it with a `:-` default: those scripts run with `set -euo pipefail`.
- `config.yaml` is generally write-once on first boot. New built-ins or SSH siblings are picked up only through documented generator/loader backfills or by regenerating the file; do not assume an image rebuild rewrites a named-volume config.
- `shutil.which(command, path=...)` in `config_bootstrap.py` must retain the explicit path argument so caller-provided PATH values work.
- A `codex-host` command cannot use the container's `{last_message_file}` tempfile; its generator branch strips that option and preserves Codex's `-c model_reasoning_effort=...` injection.
- In Docker, Hermes installation is build-time behavior controlled by the Dockerfile's `HERMES_INSTALL_CMD`; changing it requires rebuilding the image. Host SSH/staging behavior and the effective defaults are documented in `README.md` and the compose/env examples.

## Change checklist

- Backend behavior/configuration: run the smoke script, or at least the relevant calls in `smoke.py:main()` while iterating, then run the full script before handing off.
- Frontend behavior: run `npm run typecheck` and `npm run build` from `frontend/`.
- Changes to `deploy/`, Docker, systemd, or Android: follow the corresponding verification/build procedure in `README.md` and inspect generated/runtime configuration rather than assuming an existing volume was regenerated.
- For schema changes, update `_SQLITE_COLUMN_ADDITIONS` when an existing SQLite installation needs the new column; do not introduce Alembic.
- For agent changes, trace both task/goal invocation and interactive session/host-sibling paths, then update the smoke coverage that exercises the affected path.

## Further reading

- [`README.md`](./README.md): user-facing behavior, local setup, systemd/Docker deployment, agent configuration, Android build, and security/deployment details.
- [`backend/app/task_runner.py`](./backend/app/task_runner.py): task/worktree/commit pipeline and PTY session manager.
- [`backend/app/config.py`](./backend/app/config.py): settings, agent specs, context instructions, and runtime config loading.
- [`backend/app/config_bootstrap.py`](./backend/app/config_bootstrap.py): first-boot agent YAML generation and SSH sibling wiring.
- [`backend/tests/smoke.py`](./backend/tests/smoke.py): the repository's executable integration coverage.
