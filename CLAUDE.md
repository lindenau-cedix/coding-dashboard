# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted dashboard that delegates per-project coding tasks to agent CLIs (**Claude Code**, **Hermes**, **Codex**): create/import a GitHub repo → send a task/goal/interactive session to an agent → stream live output → auto-commit & push → keep history. FastAPI backend + React SPA, also packaged as an Android app via Capacitor. Single-user (password → JWT).

`AGENTS.md` is the detailed, **living** architecture doc — it is far more exhaustive than this file and is auto-maintained (see "Gotchas"). Read it for deep per-flow detail; treat this file as the orientation map.

## Commands

Backend (Python 3.10–3.12, FastAPI). From `backend/`:
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../deploy/config.example.yaml ./config.yaml      # agent command definitions
export CD_SECRET_KEY=dev CD_GITHUB_TOKEN=ghp_…
export CD_ADMIN_USERNAME=admin
export CD_ADMIN_PASSWORD_HASH=$(python -m app.cli hash-password 'dev')
uvicorn app.main:app --reload --port 8000
```

Frontend (React 18 + Vite + TS + Tailwind v4). From `frontend/`:
```bash
npm install
npm run dev          # http://localhost:5173, proxies /api (incl. /api/ws, ws:true) → 127.0.0.1:8000
npm run build        # → frontend/dist (what nginx / the SPA fallback serves)
npm run typecheck    # tsc --noEmit  (run this before considering frontend changes done)
```

Tests — a single self-contained script, **no pytest / no test runner**, no external services:
```bash
cd backend && .venv/bin/python tests/smoke.py
```
It runs every check in `main()` and prints `[PASS]/[FAIL]` lines (security, parsers, the agent subprocess runner, a full local git commit/push cycle against a bare repo, the REST API, a complete task run, and session/worktree logic). To focus on one area while iterating, temporarily comment out calls in `smoke.py:main()` — there is no per-test selector. There is **no configured linter** (no ruff/eslint).

Deploy/admin (server, see README for full flow): `sudo ./deploy/install.sh`, `sudo ./deploy/update.sh`, `./deploy/build-android.sh https://host`. Runtime config lives at `/etc/coding-dashboard/config.yaml`; data (SQLite + cloned repos) under `/var/lib/coding-dashboard/`.

## Architecture (the parts that span files)

**Agent abstraction.** Every agent is an `AgentSpec` (config.py) loaded from `config.yaml` — a list of argv tokens with placeholders `{prompt}`, `{project_dir}`, `{last_message_file}`, plus `prompt_via` (arg/stdin), `stream_format` (claude-json/raw), `goal_command`, `session_command`, and `model_*`/`effort_*` choices+args. Adding/altering agent behavior is usually config, not code. `default_agents()` defines the built-ins (claude/hermes/codex); `load_agents_config` **backfills** missing fields onto built-ins so old installed configs gain new features on restart without hand-editing.

**One-shot task path:** `routers/tasks.py` (`POST /projects/{id}/tasks`) → `task_runner.TaskManager.submit` spawns an asyncio task → `agents.run_agent` runs the CLI subprocess, streams stdout through a per-task pub/sub `TaskChannel`, and parses it (`_ClaudeJSONParser` for stream-json, else raw+ANSI-strip). After the run: write result to DB → `_update_agents_md` → `git add -A`/commit/push (always pushes, so the updated AGENTS.md ships). Live output reaches the browser over WS `/api/ws/tasks/{id}`, with replay from buffer/DB for late or reconnecting clients. Per-**project** `asyncio.Lock` serializes git; different projects run in parallel.

**Interactive session path:** `routers/sessions.py` + `task_runner.SessionManager`. A real PTY (`os.openpty` + `os.fork`) runs the agent's `session_command` as a TUI; raw bytes stream both ways over WS `/api/ws/sessions/{id}`. The browser side (`components/SessionTerminalModal.tsx`) does a small ANSI/cursor emulation and bracketed-paste handling. Because agent CLIs key saved conversations to their launch directory, resume runs in the session's recorded cwd while new parallel sessions get isolated git worktrees — see `session_dirs.py` and the `session-resume-cwd-binding` memory.

**Shared infra:** `git_ops.py` (subprocess git; token injected per-call as an HTTP header, never written to `.git/config`), `github_client.py` (REST repo create/import/delete), `database.py` (sync SQLAlchemy 2 over SQLite, `session_scope` context manager), `models.py` (just `Project` + `Task` — `Task` carries task/goal/session modes), `auth.py` (`get_current_user` Bearer for HTTP, `user_from_token` for WS), `uploads.py` (image attachments stored outside the repo so auto-commit skips them). `main.py` wires routers under `/api` and serves the built SPA as a fallback when nginx isn't in front.

Frontend is a HashRouter SPA: `api.ts` centralizes REST + base-URL/token (base URL is build-time `VITE_API_BASE`, overridable at runtime on the login screen for the Android app), pages in `pages/`, the task console + session modal in `components/`.

## Gotchas (these will bite if ignored)

- **DB is migration-free** — no Alembic. Schema is `create_all`. New columns on existing SQLite DBs go in `database._SQLITE_COLUMN_ADDITIONS` as idempotent `ALTER TABLE ADD COLUMN` (runs after `create_all` in `init_db`).
- **WebSocket routers must not carry router-level `HTTPBearer`/security deps.** FastAPI tries to resolve them for `@router.websocket` routes, which have no real `Request`, and the handshake crashes. WS routes auth via `user_from_token(token)` (query param) inside the handler; HTTP routes in the same router take `Depends(get_current_user)` explicitly.
- **Endpoints that call `asyncio.create_task` must be `async def`.**
- **`git_ops` functions are blocking** (subprocess) — call them from the event loop via `asyncio.to_thread`, as `task_runner` does.
- **Don't hand-write the `## Letzter Durchlauf` block in AGENTS.md.** The agent maintains it via the appended `context_instruction`; the dashboard only strips legacy `## Letzte Tasks` blocks before pushing. After making changes here, expect AGENTS.md to be the place a future agent records what changed.
- **Secrets only via `CD_*` env**; the GitHub token must never be persisted to disk/git config.
- Agents run autonomously (`--dangerously-skip-permissions` / `--yolo` / Codex non-interactive) and push without confirmation — intended for private repos with a dedicated token; the service runs as a non-root user.
