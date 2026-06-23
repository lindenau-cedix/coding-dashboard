# AGENTS.md - Coding Dashboard

Shared context for Codex / Claude Code / Hermes and contributors. Keep this file short and current.

## Latest Run

### 2026-06-23 - hermes (drop in-tab window + taskbar; popup is the only surface)

**Task:** Remove the duplicate in-page agent window and the bottom taskbar
that appeared next to the popup whenever a Task or Session was started.

**Result:** Only the popup now opens. The in-tab floating window (the
"focused window" overlay) and the bottom tab strip are both gone. The
`/windows/{task,session}/:id` popup route is now the single source of
truth for agent UI - no in-tab tray, no fallback dock, no localStorage
state for open windows.

**What changed:**

- **`WindowManager` slimmed to a popup helper** (`frontend/src/components/WindowManager.tsx`).
  The 428-line default-exported `WindowManager` component, the `OpenWindow`
  type, the `pinAgentWindow` / `cd-open-window` event bus, the `cd_open_windows_v1`
  localStorage cache, the focused-window overlay, and the bottom tab strip
  are all gone. The file now exports only `openAgentWindowInNewTab(task)`
  (which does the `window.open` against `/windows/{kind}/:id`) and a thin
  `openAgentWindow(task, agentLabel)` wrapper that calls it. The `_agentLabel`
  parameter is intentionally unused; popup-based windows render their own
  agent badge from `/api/agents`.
- **`Layout` no longer mounts the tray** (`frontend/src/components/Layout.tsx`).
  Dropped the `useProject` import and the `<WindowManager agents={agents}
  currentProject={project} />` line. The page now just renders
  `<Outlet />` inside the header / main shell. The `ProjectProvider` in
  `projectContext.tsx` is kept because `ProjectDetail` still uses
  `useProject()` for its agent / project cache.
- **No backend or routing changes.** `AgentWindowPage.tsx`, the
  `#/windows/task/:taskId` and `#/windows/session/:taskId` routes, the
  PTY / WS plumbing, and the `SessionTerminalModal` / `TaskConsole` bodies
  are all untouched. The popup still hosts the agent in its own tab; only
  the in-page shadow is removed.

**Verified:** `tsc --noEmit` clean; `vite build` succeeds
(`✓ 36 modules transformed`, same module count as before - the smaller
`WindowManager.tsx` still counts as one module, and the removed mount
in `Layout.tsx` doesn't add a new one). `git status` shows exactly the
two intended files. No `OpenWindow` / `pinAgentWindow` / `cd-open-window`
/ `cd_open_windows_v1` references remain anywhere in `src/`. The
`/windows/{task,session}/:id` route is what the popup still opens, so
resuming / closing / the focused window's `⧉` button / persisted
behavior described in the 2026-06-23 "pop out" run still work.

### 2026-06-23 - hermes (agent windows pop out, single-click session close)

**Task:** Fix the "double-click to close a session" UX and let sessions and
running tasks open in their own browser window.

**Result:** Single-click close on sessions + a dedicated popup-tab view
(`/windows/task/:id`, `/windows/session/:id`) becomes the default surface;
the in-tab floating tray stays as a fallback when popups are blocked.

**What changed:**

- **New route + standalone page**
  (`frontend/src/pages/AgentWindowPage.tsx`,
  `frontend/src/App.tsx`). Two new HashRouter routes
  (`/windows/task/:taskId`, `/windows/session/:taskId`) render an
  agent's UI in a clean fullscreen view, deliberately outside the
  dashboard `Layout` (no header, no project nav, no width cap). A small
  `RequireAuthInline` wrapper gates them so a missing token still
  redirects to `/login`. Closing the popup tab does **not** end the
  underlying task / session — only the browser tab goes away; the
  backend keeps streaming output, so reopening the window from the
  dashboard reconnects to the same WS with the live buffer.
- **WindowManager helpers**
  (`frontend/src/components/WindowManager.tsx`). New
  `openAgentWindowInNewTab(task)` does the actual `window.open(...)`
  against the new route URL; `pinAgentWindow(task, label)` is the
  imperative-bus pin that was previously called `openAgentWindow`. The
  default `openAgentWindow(task, label)` now tries the popup first and
  falls back to the in-tab tray if the browser blocked the popup (so
  silent failures become visible). The focused window's title bar gets
  a new `⧉` button that promotes any pinned window to a popup. The
  tray tab's `✕` and the focused window's "Schließen" / header `✕`
  now all call `closeWindow(taskId)` directly — a single click closes
  any agent window end-to-end, no more "minimise first, then dismiss"
  two-step dance that bit sessions hardest.
- **ProjectDetail cleanup**
  (`frontend/src/pages/ProjectDetail.tsx`). The inline
  `sessionDialogTaskId` modal is gone — `startSession()` and clicking a
  session in history both go straight to `openAgentWindow(...)`, which
  pops the dedicated session tab. The legacy
  `/projects/:id/sessions/:taskId` URL is preserved as a redirect to
  `/windows/session/:taskId` (`SessionPage.tsx`).

**Verified:** `tsc --noEmit` and `vite build` succeed
(`✓ 36 modules transformed`, +1 over the previous run — the new
`AgentWindowPage`). Backend untouched; no smoke-test rerun needed.
No regressions in the existing tray / pinned-window / persistence
behavior — the floating tray still works for the popup-blocked case
and survives reloads via `localStorage` (`cd_open_windows_v1`).

### 2026-06-23 - codex (auto-pull, autoclone, multi-window)

**Task:** Four dashboard improvements in one go:
1. Auto-pull before every agent run so the agent works against the latest
   remote HEAD instead of a stale local checkout.
2. "Sync from GitHub" — bulk-import every repo visible to the token.
3. Multiple floating agent "windows" (live consoles / sessions) opened
   side-by-side, persisted across reloads.
4. Clicking a running agent on the projects page opens its window
   directly — no more routing through the project detail screen.

**Result:** All four landed in backend + frontend.

**What changed:**

- **Backend — auto-pull** (`backend/app/task_runner.py`,
  `backend/app/git_ops.py`).  New `TaskManager._auto_pull()` runs under the
  per-project lock just before the worktree is created: `git fetch origin`
  → check `has_remote_update()` → `git pull --ff-only origin <branch>`.
  On any failure (network, dirty tree, divergence) it publishes a one-line
  warning to the live stream and continues — the agent still gets a
  coherent working tree, just one based on the pre-fetch local HEAD.
  Host-staging agents (the SSH-driven Hermes) skip the auto-pull because
  they run in a copy the host can't push to; the canonical repo IS the
  source of truth there.  Interactive sessions get a fetch-only call at
  `start` so a long-lived TUI sees fresh refs without racing user edits.
- **Backend — bulk GitHub sync**
  (`backend/app/github_client.py`,
  `backend/app/routers/projects.py`).  New
  `GET /api/projects/from-github` paginates every repo visible to the
  token (`/user/repos` with `affiliation=owner,collaborator,organization_member`,
  `/orgs/<owner>/repos` when `CD_GITHUB_OWNER` points at an org) and
  flags already-imported ones.  New
  `POST /api/projects/sync-from-github` clones the missing repos
  one-by-one, returns a per-repo status (`imported` / `skipped` /
  `failed`), and isolates failures so one bad clone doesn't abort the
  batch.  Per-repo errors are returned as `SyncFromGithubResult.detail`
  rather than 5xx-ing the whole call.
- **Frontend — Sync modal**
  (`frontend/src/components/SyncFromGithubModal.tsx`).  Preview of every
  remote repo with checkboxes (preselected: not-yet-imported ones), filter
  input, "include forks / archived" toggles, and a result panel showing
  per-repo status after sync.
- **Frontend — window manager**
  (`frontend/src/components/WindowManager.tsx`,
  `frontend/src/projectContext.tsx`,
  `frontend/src/components/Layout.tsx`,
  `frontend/src/App.tsx`).  A floating bottom tab strip + one focused
  window at a time.  Multiple running agents can be opened simultaneously;
  each tab is a TaskConsole (one-shot) or SessionTerminalModal (PTY TUI).
  State persists in `localStorage` (`cd_open_windows_v1`) so a refresh
  restores the user's open set.  New `openAgentWindow(task, label)` bus
  via `window.dispatchEvent('cd-open-window', ...)`.  Finished windows
  are auto-pruned after one polling cycle so the tray doesn't grow
  forever.  A `ProjectProvider` exposes the agents list + current
  project to descendants so the window manager doesn't refetch data the
  page already has.
- **Frontend — direct open from projects page**
  (`frontend/src/components/RunningAgents.tsx`,
  `frontend/src/pages/Projects.tsx`,
  `frontend/src/pages/ProjectDetail.tsx`).  `RunningAgents` now dispatches
  `openAgentWindow` on click instead of `<Link to={"/projects/..."}>`;
  the project detail page also pins a window when a task or session is
  started locally, so the user can keep watching it after navigating
  away.  Projects page header gets a `⇣ Sync von GitHub` button alongside
  the existing `+ Neues Projekt`.

**Verified:** Backend smoke tests + new `test_auto_pull_helpers` and
`test_sync_from_github_validation` (15 new checks) all pass.
`tsc --noEmit` and `vite build` succeed (`✓ 35 modules transformed`).
All prior smoke tests still pass.

### 2026-06-22 - codex (Docker Compose: host SSH firewall range)

**Task:** Answer which firewall source range is needed so the Docker Compose
`dashboard` container can SSH to the host's sshd for the host-run Hermes flow.

**Result:** The firewall should allow the exact source CIDR of the Compose bridge
network that the running `dashboard` container uses, not a public IP range and not
necessarily all of Docker's private ranges. The range is discoverable with
`docker inspect` + `docker network inspect`; in typical installs it looks like
`172.x.0.0/16`, but the exact CIDR is deployment-specific and may change if the
Compose network is recreated.

**What changed:** README now documents the command to inspect the running
container's Compose network subnet, recommends allowing only that CIDR to TCP/22,
and notes that an explicit Compose network subnet is the clean way to keep a
stable firewall rule.

**Verified:** Documentation-only change; no tests were run.

## Purpose

Self-hosted dashboard for delegating coding tasks per project to Claude Code,
Hermes, or Codex: create/import a repo -> give an agent a task -> watch live output
-> auto-commit & push -> keep history. Web + Android.

## Tech stack

- Backend: Python, **FastAPI** + Uvicorn, **SQLAlchemy 2** over **SQLite**. Sync DB.
- Frontend: **React 18 + Vite + TypeScript + Tailwind v4**, **HashRouter**.
- Android: **Capacitor** wrapper around `frontend/dist`.
- Auth: single-user password login (pbkdf2) -> **JWT** (PyJWT).
- Deploy: **systemd** + **nginx**, scripts in `deploy/`.

## Repository layout

```
backend/app/
  config.py        Settings (env CD_*) + agent config (YAML) + context_instruction
  database.py      Engine / session (SQLite), init_db, session_scope
  models.py        Project, Task
  schemas.py       Pydantic I/O
  security.py      pbkdf2 hash + JWT
  auth.py          get_current_user (Bearer), user_from_token (WS)
  github_client.py GitHub REST (create / get / delete repo)
  git_ops.py       clone / commit / push (token only as http.extraheader, never in config)
  agents.py        run_agent(): subprocess + streaming, claude-json/raw parser,
                   model / effort arg injection, final output extraction
                   (_final_output heuristic or {last_message_file})
  uploads.py       Image attachments: Base64 / data-URL decode + validation,
                   stored under data_dir/task_images/{task_id}/
  task_runner.py   TaskManager: per-project lock, WS pub/sub, AGENTS.md maintenance,
                   auto-commit + push
  routers/         auth, projects, tasks, ws
  main.py          app factory, lifespan, SPA serving (fallback)
frontend/src/
  api.ts (REST + apiBase/token), auth.tsx, types.ts
  pages/ (Login, Projects, ProjectDetail, SessionPage redirect, AgentWindowPage)
  components/ (TaskConsole, SessionTerminalModal, WindowManager, TaskImages, ui, ...)
deploy/            install.sh, update.sh, uninstall.sh, build-android.sh, unit, nginx, *.example
```

## Core flows

- **Task:** `POST /api/projects/{id}/tasks` (body: `agent`, `prompt`, `mode`,
  optional `model`, `effort`) -> `TaskManager.submit` -> asyncio task. Prompt =
  user prompt + `context_instruction` (AGENTS.md maintenance). Output streams over
  WS `/api/ws/tasks/{id}` (with replay from buffer / DB for late or repeated joins).
  After the agent run: result in DB -> set `finished_at` -> `_update_agents_md()`
  -> `git_ops`: `add -A` -> commit (if changed) -> push (always), so the push always
  includes the updated AGENTS.md. Result, commit hash, and push status are stored in
  the DB.
- **Model / effort per task:** agents with `model_choices` / `effort_choices`
  (Claude, Codex) get dropdowns in the UI ("Standard" = empty = CLI default).
  The selection is stored in `tasks.model` / `tasks.effort`, validated against the
  allowed choices (400 otherwise), and injected into argv via `model_args` /
  `effort_args` (before a trailing `-` stdin marker, otherwise appended - so explicit
  `command` lists in `config.yaml` keep working). Claude: `--model {opus|sonnet|haiku}`,
  `--effort {low|medium|high|xhigh|max}`. Codex: `--model ...`,
  `-c model_reasoning_effort={low|medium|high|xhigh}`.
- **Image attachments:** `TaskCreate.images` = list of `{name, data}` (data is Base64,
  data URLs allowed; max 6 images, max 8 MB each, only png/jpg/jpeg/gif/webp;
  validation in `uploads.decode_images`, 400 on error before the task row is created).
  Stored outside the repo under `data_dir/task_images/{task_id}/` so auto-commit never
  picks them up; file names are sanitized and deduplicated, stored as JSON in the
  `tasks.images` TEXT column. The agent gets the absolute paths as an "Attached images"
  block appended to the prompt (`build_agent_prompt(..., image_paths=...)`) and opens
  them with its own read / image tool. The UI fetches them via
  `GET /api/tasks/{id}/images/{name}` (only names registered in `tasks.images` - no
  traversal; auth required, so the frontend loads them through `fetch` + Object URLs
  instead of a direct `<img src>`). UI: file button + Ctrl+V paste into the text field,
  preview with removal, thumbnails in history (`components/TaskImages.tsx`). When a
  project is deleted, the image folders for its tasks are deleted too.
- **Final output (`Task.result_summary`):** priority is
  `{last_message_file}` content (Codex `--output-last-message`, exactly the last agent
  message) -> parser summary (Claude `result` event) -> `_final_output()` heuristic
  (raw / Hermes: last paragraph, with box drawing and session footer text such as
  "Resume this session" / "Session:" / "tokens used" filtered out).
- **Goal mode (`mode="goal"`):** instead of a task, the user submits a goal. The
  backend invokes the agent through its `goal_command` template (Claude:
  `/goal {prompt}`); the entire run until the goal is achieved counts as one task.
  Streaming, AGENTS.md maintenance, commit, push, and history behave the same.
  Prompt assembly lives in `task_runner.build_agent_prompt()`. Only agents with a
  configured `goal_command` offer the mode (`AgentInfo.supports_goal`); the UI shows
  the toggle only then and filters the agent list accordingly.
- **Agent config:** `config.yaml`. Placeholders: `{prompt}`, `{project_dir}`,
  `{last_message_file}` (temporary file for the last agent message). Supported fields:
  `stream_format: claude-json|raw`, `prompt_via: arg|stdin`, `env`, `unset_env`,
  `goal_command` (optional, enables goal mode), `session_command` (optional, enables
  TUI session mode), `model_choices` / `model_args` / `effort_choices` / `effort_args`
  (optional, enable the model / effort dropdowns for task / goal). **Backfill:**
  for built-in agents (`claude`, `hermes`, `codex`), `load_agents_config` fills in
  missing fields from `default_agents()`; `config.yaml` only overrides fields that are
  explicitly set. Old installer-generated configs with `claude` / `hermes` receive the
  new built-in agent `codex` on restart automatically; pure custom configs remain
  explicit. This lets existing installs pick up new optional fields / agents without
  manual edits to `/etc/coding-dashboard/config.yaml` (`update.sh` intentionally leaves
  an existing config untouched). Claude: `claude -p ... stream-json` (the parser shows
  tool calls with detail, e.g. `[tool] Bash: ls -la` / `[tool] Read: path`, instead of
  only the tool name). Both command variants (task + goal) work without
  `--use-auth-token`. Hermes: `hermes chat -q {prompt} --yolo --accept-hooks`
  (non-interactive, streams intermediate steps, loads AGENTS.md from the CWD; with
  `env: HERMES_ACCEPT_HOOKS=1, NO_COLOR=1` and `unset_env: [PYTHONPATH, PYTHONHOME]`).
  Codex: `codex exec --cd {project_dir} --sandbox workspace-write --color never
  --ephemeral --output-last-message {last_message_file} -` with `prompt_via: stdin`
  (no `goal_command`, so no goal mode for Codex). Current Codex versions do not have
  `--ask-for-approval` - the command is non-interactive on its own when given a
  prompt. Raw output is ANSI-filtered in the runner. TUI session defaults:
  Claude `claude`, Hermes `hermes chat`, Codex `codex`; extra flags only through the
  startup parameter field.
- **AGENTS.md update:** after each completed task (success or failure), the agent
  writes a short summary of what happened in the `## Latest Run` block at the very top
  of AGENTS.md (immediately after the title and purpose paragraph) through the
  `context_instruction`. The dashboard no longer writes this block itself - it only
  checks before push whether old legacy task blocks (from dashboards before
  2026-06-12) still exist and removes them if necessary. This keeps the file clean and
  leaves the agent responsible for its own section. Runs before commit / push.
- **Serialization:** one `asyncio.Lock` per project (no Git races); different projects
  run in parallel. Running tasks are marked as `interrupted` after restart.
- **Session mode (`mode="session"`):** shellinabox-style TUI sessions in the browser
  (PTY-based). The agent starts in the project directory through its TUI base command
  without prompt injection; optional startup parameters come from a dedicated UI field
  and are appended server-side with `shlex.split()` as argv (no shell).
  - `Task.is_session=True`, `Task.prompt` stores the startup parameters,
    `Task.output` stores the entire raw TUI transcript continuously.
    `chat_history` remains only for compatibility with old data.
  - Backend: `SessionManager.start()` forks a PTY (`os.openpty` + `os.fork`), sets
    `TERM=xterm-256color`, starts `session_command + start_args`, reads raw bytes from
    the PTY, and appends them to `Task.output` with offsets. Output events are
    `{type:"output", data, offset}`.
  - Agent-specific: `session_command` in `AgentSpec` is the TUI base command.
    Built-ins: Claude `["claude"]`, Hermes `["hermes","chat"]`, Codex `["codex"]`.
    Model / effort dropdowns are not injected in session mode; use explicit startup
    parameters instead.
  - WebSocket `/api/ws/sessions/{task_id}?token=...&offset=N` forwards
    `{type:"message",content}` as raw UTF-8 bytes to the PTY and accepts
    `{type:"resize",cols,rows}` for `TIOCSWINSZ` + `SIGWINCH`.
  - **Bracketed paste:** `SessionManager.start()` enables DEC mode `?2004h` right
    after PTY setup. `SessionTerminalModal.onTerminalPaste` also wraps clipboard text in
    `\x1b[200~ ... \x1b[201~`. Multiline pastes are therefore treated by Claude Code /
    Codex / Hermes as one event instead of a series of Enter submissions.
  - **Ctrl+V (and Ctrl+Shift+V) are NOT sent as raw `\x16` to the TUI.**
    `keyToBytes()` intentionally returns `null` for Ctrl+V so the browser default can
    fire the `paste` event and `onTerminalPaste` can build bracketed paste. Reason: the
    common TUIs (Codex, Claude Code, Hermes) treat Ctrl+V as an image-paste shortcut and
    call `arboard::Clipboard::new()` to read an image from the OS clipboard. In our
    headless PTY without DISPLAY this fails with "X11 server connection timed out" /
    "no image found", and the text paste is lost. Only the bracketed paste via the
    browser `paste` event reaches the TUI reliably.
  - Frontend: `SessionTerminalModal` opens directly inside `ProjectDetail` as a dialog,
    renders the transcript through a small ANSI / cursor emulator, sends arrow keys /
    Enter / Tab / Ctrl+C / paste as raw terminal sequences, and reloads the saved
    `Task.output` when reopened. The old session route is just a wrapper around the
    same dialog.
  - Closing the window / dialog does **not** end the session. As long as the backend
    process lives, the session can be reopened from history.
  - After `end_session`: the PTY process group is terminated with `os.killpg(pid,
    SIGTERM)` (manual stop counts as success), `result_summary =
    "Interactive TUI session ended"`, then Git commit if there were changes and always
    push, same as a normal task.
  - **Limitation:** after `systemctl restart coding-dashboard`, running sessions are
    terminated (server process gone); the transcript persisted up to that point remains
    in `Task.output`.

## Conventions

- Secrets only via env (`CD_*`). Never persist GitHub tokens.
- DB migration-free: `create_all` (no Alembic). Additive columns for existing SQLite
  DBs go into `database._SQLITE_COLUMN_ADDITIONS` (idempotent `ALTER TABLE ADD COLUMN`,
  runs after `create_all` in `init_db`).
- Backend endpoints that use `asyncio.create_task` must be `async def`.
- **Routers with WebSocket endpoints must not have `HTTPBearer` / `HTTP...` security
  dependencies at router level.** FastAPI tries to resolve them for
  `@router.websocket(...)` routes, but WebSocket handshakes have no real `Request`
  -> `TypeError: HTTPBearer.__call__() missing 1 required positional argument:
  'request'` -> WebSocket closes immediately. WebSockets authenticate via
  `user_from_token(token)` (query parameter) inside the route; HTTP routes in the same
  router get `Depends(get_current_user)` explicitly in the signature.
- `git_ops` calls are blocking subprocesses; call them from the event loop via
  `asyncio.to_thread`, as `task_runner` does.
- Do **not** hand-write the `## Latest Run` block in AGENTS.md. The agent maintains it
  via the appended `context_instruction`; the dashboard only strips legacy
  legacy task blocks before pushing. After making changes here, AGENTS.md should
  be the place where future agents record what changed.
- Model / effort is applied twice - as argv (`model_args` / `effort_args`) and, only
  for the built-in `claude` / `codex` keys, by writing the agent's own config before
  launch in `agents.py` (`~/.claude/settings.json` effort; `~/.codex/config.toml`
  model + `model_reasoning_effort`). The dotfile write keys off `spec.key`, so
  renaming those agents in `config.yaml` silently disables it.
- `CD_CORS_ORIGINS=*` is reflected, not literal - `main.py` sets
  `allow_origin_regex='.*'` with `allow_credentials=True`, so it echoes the caller's
  Origin (a literal `*` is invalid with credentials; needed for the Capacitor
  `https://localhost` WebView).
- Secrets only via `CD_*` env; the GitHub token must never be persisted to disk or
  into git config.
- Agents run autonomously (`--dangerously-skip-permissions` / `--yolo` / Codex
  non-interactive) and push without confirmation - intended for private repos with a
  dedicated token; the service runs as a non-root user.

## Quick commands

Backend (Python 3.10-3.12, FastAPI). From `backend/`:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../deploy/config.example.yaml ./config.yaml      # agent command definitions
export CD_SECRET_KEY=dev CD_GITHUB_TOKEN=ghp_...
export CD_ADMIN_USERNAME=admin
export CD_ADMIN_PASSWORD_HASH=$(python -m app.cli hash-password 'dev')
uvicorn app.main:app --reload --port 8000
```

Frontend (React 18 + Vite + TS + Tailwind v4). From `frontend/`:

```bash
npm install
npm run dev          # http://localhost:5173, proxies /api (incl. /api/ws, ws:true) -> 127.0.0.1:8000
npm run build        # -> frontend/dist (what nginx / the SPA fallback serves)
npm run typecheck    # tsc --noEmit (run this before considering frontend changes done)
```

Tests - a single self-contained script, **no pytest / no test runner**, no external services:

```bash
cd backend && .venv/bin/python tests/smoke.py
```

It runs every check in `main()` and prints `[PASS]/[FAIL]` lines (security, parsers,
the agent subprocess runner, a full local git commit / push cycle against a bare repo,
the REST API, a complete task run, and session / worktree logic). To focus on one area
while iterating, temporarily comment out calls in `smoke.py:main()` - there is no
per-test selector. There is **no configured linter** (no ruff / eslint).

Deploy / admin (server, see README for full flow): `sudo ./deploy/install.sh`,
`sudo ./deploy/update.sh`, `./deploy/build-android.sh https://host`. Runtime config
lives at `/etc/coding-dashboard/config.yaml`; data (SQLite + cloned repos) under
`/var/lib/coding-dashboard/`.

Alternatively, **Docker Compose** (`docker-compose.yml` + `deploy/docker/`): one
container - uvicorn serves API + WS + the built SPA (no nginx inside), with
**claude + codex** baked in (override via `CLAUDE_NPM_PKG` / `CODEX_NPM_PKG`).
Runs as non-root `app`; `cd-config` = config.yaml, `cd-home` = claude + codex
logins (`~/.claude` / `~/.codex`) and **`cd-data` = data (DB + repos + worktrees)**
are named volumes — the data volume is PRIVATE to the container. **Hermes does NOT
run in the container: when `CD_HERMES_SSH_USER` is set the entrypoint generates a
`config.yaml` whose `hermes` agent `ssh`es into the host and runs the host's
`hermes` there** (`ssh user@host 'cd {project_dir} && exec hermes …'`) - one
Hermes on the host (no doubled cronjobs / WhatsApp / Telegram replies). Because the
host can't see `cd-data`, the SSH Hermes agent is flagged **`host_staging`**: for
each run the dashboard copies the project into a small host-shared staging dir
(`CD_HERMES_STAGING_HOST_DIR`, default `/tmp/coding-dashboard-hermes`, bind-mounted
at an IDENTICAL path host<->container so `cd {project_dir}` resolves on both sides),
the host's Hermes edits the copy, and the dashboard merges the copy's commit back
into `cd-data` + pushes (conflict ⇒ branch kept for a manual merge + Pull; see
`host_staging.py`). The container reaches the host via
`host.docker.internal` (`extra_hosts: host-gateway`) using a read-only mounted
private key (`CD_HERMES_SSH_KEY_HOST` → `/home/app/.ssh/id_hermes`). `APP_UID` /
`APP_GID` default `1000:1000` and should match the host user that runs Hermes /
owns the staging dir (Dockerfile removes the base `node` user/group first so `1000`
builds cleanly); a self-contained in-image Hermes is still possible via
`HERMES_NPM_PKG` / `HERMES_INSTALL_CMD` with `CD_HERMES_SSH_USER` empty. Auth is
**interactive login only** (`docker compose exec dashboard claude`), no API keys
on disk. `deploy/docker/entrypoint.sh` generates `config.yaml` on first boot from
`default_agents()`, enabling only the CLIs on `PATH` (plus Hermes when SSH is
configured); it is written once and never overwritten (delete it from `cd-config`
to regenerate). Secrets via `deploy/docker/coding-dashboard.docker.env`
(gitignored; `.example` committed).

## Architecture (the parts that span files)

**Agent abstraction.** Every agent is an `AgentSpec` (`config.py`) loaded from
`config.yaml` - a list of argv tokens with placeholders `{prompt}`, `{project_dir}`,
`{last_message_file}`, plus `prompt_via` (arg/stdin), `stream_format`
(claude-json / codex / raw), `goal_command`, `session_command`, and
`model_*` / `effort_*` choices + args. Changing agent behavior is usually config,
not code. `default_agents()` defines the built-ins (claude / hermes / codex);
`load_agents_config` **backfills** missing fields onto built-ins so old installed
configs gain new features on restart without manual edits.

**One-shot task path.** `routers/tasks.py` (`POST /projects/{id}/tasks`) ->
`task_runner.TaskManager.submit` spawns an asyncio task -> `agents.run_agent` runs the
CLI subprocess, streams stdout through a per-task pub/sub `TaskChannel`, and parses it
(`_ClaudeJSONParser` for stream-json, `_CodexParser` for Codex output, otherwise
`_RawParser` + ANSI stripping). After the run: write result to DB ->
`_update_agents_md` -> `git add -A` / commit / push (always pushes, so the updated
AGENTS.md ships). Live output reaches the browser over WS `/api/ws/tasks/{id}`, with
replay from buffer / DB for late or reconnecting clients. A per-**project**
`asyncio.Lock` serializes git; different projects run in parallel.

**Interactive session path.** `routers/sessions.py` + `task_runner.SessionManager`.
A real PTY (`os.openpty` + `os.fork`) runs the agent's `session_command` as a TUI;
raw bytes stream both ways over WS `/api/ws/sessions/{id}`. The browser side
(`components/SessionTerminalModal.tsx`) does light ANSI / cursor emulation and
bracketed-paste handling. Because agent CLIs store saved conversations in their launch
directory, resume runs in the session's recorded cwd while new parallel sessions get
isolated git worktrees - see `session_dirs.py` and the `session-resume-cwd-binding`
memory.

**WS reconnect / replay.** `routers/ws.py` serves a task's live output from its
per-task `TaskChannel` pub/sub; when no channel exists (the run already finished, or
the server restarted), it replays the task state from the DB instead - so WS is not
live-only and clients can connect late or reconnect without losing output.

**Shared infra.** `git_ops.py` (blocking git subprocesses; token injected per call as
an HTTP header, never written to `.git/config`), `github_client.py` (REST repo
create / import / delete), `database.py` (sync SQLAlchemy 2 over SQLite,
`session_scope` context manager), `models.py` (the ORM - just `Project` + `Task`,
where `Task` carries task / goal / session modes), `schemas.py` (Pydantic request /
response models + `field_validator`s - the wire shapes, distinct from the ORM),
`auth.py` (`get_current_user` Bearer for HTTP, `user_from_token` for WS),
`uploads.py` (image attachments stored outside the repo so auto-commit skips them).
`main.py` wires routers under `/api` and serves the built SPA as a fallback when nginx
is not in front.

Frontend is a HashRouter SPA: `api.ts` centralizes REST + base URL / token (base URL is
build-time `VITE_API_BASE`, overridable at runtime on the login screen for the Android
app), pages live in `pages/`, and the task console + session modal are in `components/`.
**Agent windows.** `openAgentWindow(task, label)` in `components/WindowManager.tsx`
opens the agent in its own browser tab via `window.open(...)` against the
`#/windows/task/:id` or `#/windows/session/:id` route (rendered by
`pages/AgentWindowPage.tsx`, deliberately outside the dashboard `Layout` so the popup
fills its viewport with no header / width cap). The popup is the only surface — there
is no in-tab tray or focused-window overlay (see the 2026-06-23 "drop in-tab window"
run). Closing the popup tab never ends the underlying task / session — only the tab
goes away; reopening it from the dashboard reconnects to the same WebSocket with the
live buffer intact.

## Gotchas

- **DB is migration-free** - no Alembic. Schema is `create_all`. New columns on
  existing SQLite DBs go into `database._SQLITE_COLUMN_ADDITIONS` as idempotent
  `ALTER TABLE ADD COLUMN` statements (run after `create_all` in `init_db`).
- **WebSocket routers must not carry router-level `HTTPBearer` / security deps.**
  FastAPI tries to resolve them for `@router.websocket` routes, which have no real
  `Request`, and the handshake crashes. WS routes auth via `user_from_token(token)`
  (query parameter) inside the handler; HTTP routes in the same router take
  `Depends(get_current_user)` explicitly.
- **Endpoints that call `asyncio.create_task` must be `async def`.**
- **`git_ops` functions are blocking** (subprocess) - call them from the event loop via
  `asyncio.to_thread`, as `task_runner` does.
- **Do not hand-write the `## Latest Run` block in AGENTS.md.** The agent maintains it
  via the appended `context_instruction`; the dashboard only strips legacy
  legacy task blocks before pushing. After changes here, expect AGENTS.md to be
  the place where a future agent records what changed.
- **Model / effort is applied twice** - as argv (`model_args` / `effort_args`) *and*,
  gated on the built-in `claude` / `codex` keys only, by writing the agent's own
  config before launch in `agents.py` (`~/.claude/settings.json` effort;
  `~/.codex/config.toml` model + `model_reasoning_effort`). The dotfile write keys off
  `spec.key`, so renaming those agents in `config.yaml` silently drops it.
- **`CD_CORS_ORIGINS=*` is reflected, not literal** - `main.py` sets
  `allow_origin_regex='.*'` with `allow_credentials=True` so it echoes the caller's
  Origin (a literal `*` is invalid with credentials; needed for the Capacitor
  `https://localhost` WebView).
- **Secrets only via `CD_*` env**; the GitHub token must never be persisted to disk or
  into git config.
- Agents run autonomously (`--dangerously-skip-permissions` / `--yolo` / Codex
  non-interactive) and push without confirmation - intended for private repos with a
  dedicated token; the service runs as a non-root user.

## Tests

`backend/tests/smoke.py` (no external services): security, parser, agent runner, full
Git commit / push cycle against a local bare repo, REST, and a complete task run.

## Open items / possible next steps

- **2026-06-13 (Fix):** Session paste via Ctrl+V failed for Codex with
  "Failed to paste image: clipboard unavailable: X11 server connection timed out",
  Claude Code said "no image found", Hermes did nothing. Cause:
  `SessionTerminalModal.keyToBytes()` sent Ctrl+V as raw `\x16` to the TUI; the TUIs
  interpret Ctrl+V as an image-paste shortcut and read the OS clipboard via `arboard`
  - which fails in our headless PTY without X11 / Wayland. Fix: `keyToBytes()` now
  returns `null` for Ctrl+V / Ctrl+Shift+V; browser default survives, the `paste`
  event fires, `onTerminalPaste` sends the text as bracketed paste
  (`\x1b[200~ ... \x1b[201~`). Result: browser pastes work with prompt integrity and
  without the TUI trying to read an image. Effective only after `update.sh` /
  `systemctl restart coding-dashboard`.
- **2026-06-13 (Feature):** Session mode bracketed paste (DEC `?2004h` +
  `\x1b[200~ ... \x1b[201~`) is active - multiline browser pastes are no longer
  interpreted as Enter submissions. The backend enables the PTY mode idempotently on
  start, and `SessionTerminalModal.onTerminalPaste` wraps the text.
  Additionally, the history header line in `ProjectDetail` shows commit hash (as a
  link when the GitHub repo is present) and `pushed ✓` / `not pushed`, matching the
  `SessionTerminalModal` footer line right after `end_session`. Effective after
  `update.sh` / `systemctl restart coding-dashboard`.
- **2026-06-13 (Fix):** Session WebSocket failed immediately with
  `TypeError: HTTPBearer.__call__() missing 1 required positional argument: 'request'`.
  Cause: `routers/sessions.py` defined `dependencies=[Depends(get_current_user)]` at
  router level - FastAPI tries to resolve that for `@router.websocket(...)` routes too,
  which fails without a `Request`. Fix: removed router-level `dependencies=`, HTTP
  routes now get `Depends(get_current_user)` explicitly in the signature, WebSocket
  auth still uses `user_from_token(token)`. Effective after `update.sh` /
  `systemctl restart coding-dashboard`.
- **2026-06-13 (Fix):** Session mode sometimes showed only a black dialog because the
  session WebSocket could connect right after `POST /sessions`, before
  `SessionManager.start()` had registered its channel. Fix: session start is now
  awaited until the PTY / channel exists, the WebSocket briefly waits for channels
  that are starting, start errors are persisted, and the frontend shows connection /
  error states instead of a blank black surface. Effective after
  `systemctl restart coding-dashboard`.
- **2026-06-13:** Session mode reworked: launches agent TUIs in the project directory
  through `session_command` without prompt injection; the startup-parameter field is
  appended with `shlex.split()` as argv. Dialog inside `ProjectDetail` instead of a
  page switch, raw keyboard forwarding including arrow keys / Ctrl+C / paste, resize
  events, ANSI / cursor rendering, and persistent `Task.output` transcript with offset
  replay. After session end: Git commit if changed and always push. Effective only
  after `systemctl restart coding-dashboard`.
- **2026-06-12 (Fix):** `_write_codex_config` stripped quotes from all values when
  reading (`strip('"')`) but only restored them for `model` / `model_reasoning_effort`,
  so `service_tier = "default"` became `service_tier = default` (without quotes),
  which blocked Codex. Fix: store the raw value without quote removal; formatting is
  preserved. Effective after `systemctl restart coding-dashboard`.
- **2026-06-12:** PTY-based session mode: the agent now runs in a real PTY, and all
  keyboard input (arrow keys, Ctrl+C, etc.) is forwarded 1:1. `session_command` in
  `AgentSpec` (new), `os.openpty` + `os.fork` instead of `asyncio.subprocess`, green
  terminal display in the frontend. Effective after `systemctl restart coding-dashboard`.
- **2026-06-12:** Model list updated: Claude Code now has `fable` as a fourth model;
  Codex uses `gpt-5.4`, `gpt-5.5`, `gpt-5.4-mini` (previously `gpt-5.1-*`). For
  Claude Code, when an effort level is set, `~/.claude/settings.json` is also written
  (`effort` key) so the level is definitely used, not just via the `--effort` flag.
  Effective after `systemctl restart coding-dashboard`.
- **2026-06-12 (Fix):** `@vitejs/plugin-react` was upgraded from `^4.3.2` to `^6.0.2`
  (now also requires `@rolldown/plugin-babel@^0.2.3` + `babel-plugin-react-compiler@^1.0.0`
  as peer deps). Fix for the npm 10 + Vite 8 peer dependency conflict. Build succeeded.
- Optional: Clean up old image folders for finished tasks (currently they remain
  indefinitely for history display; only deleted with the project).
- Optional: Token refresh / logout hardening; multi-user support.
- Optional: WS disconnect detection for very long silent tasks (currently detected on
  the next publish).
- Android: launcher icons / splash (Capacitor defaults until then).
- Optional: stash / pull-rebase option for pull conflicts (currently `git pull origin branch`,
  no stash protection).
- **2026-06-11:** Pull button now shows terminal output in a modal dialog.
  `git_ops.pull` returns `result.stdout.strip()` (previously `None`);
  `pull_project` responds with `{ok, branch, output}`; the frontend shows a `Modal`
  dialog with color-coded output (success = slate, failure = red).
- **2026-06-11 (Fix):** `Modal` was used in `ProjectDetail.tsx` but not imported ->
  TypeScript error TS2304, despite the Vite build. Fix: add `Modal` to the ui import.
- **2026-06-11 (Revision of latest tasks / model / effort):**
  1. `_update_agents_md()` now runs **before** commit / push (previously after - the
     AGENTS.md change was only pushed by the next task), and `finished_at` is set first
     (previously the current task often missed the last 3 because `ORDER BY
     finished_at DESC` sorted NULLs last). New format: per run, task + only the final
     output (instead of a 600-character tail with Hermes box / footer noise).
  2. Final output sources: Codex `--output-last-message {last_message_file}`,
     Claude `result` event, raw `_final_output()` heuristic.
  3. Claude parser shows tool calls with detail (`[tool] Bash: ls -la`).
  4. Per-task model / effort selection for Claude Code and Codex (backend fields,
     DB columns `tasks.model` / `tasks.effort`, UI dropdowns).
  **IMPORTANT:** effective only after a service restart (`systemctl restart coding-dashboard`)
  - do not restart from a running task, that kills your own run.
- Codex `model_choices` (gpt-5.1-codex...) are current as of 2026-06; adjust
  `default_agents()` / `config.yaml` when new Codex releases land.
