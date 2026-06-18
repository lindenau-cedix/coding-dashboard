# AGENTS.md - Coding Dashboard

Shared context for Codex / Claude Code / Hermes and contributors. Keep this file short and current.

## Latest Run

### 2026-06-18 - claude (Docker: run the HOST's Hermes by mirroring it, not relocating it)

**Problem:** With the host `~/.hermes` bind-mounted into the container, Hermes
failed with `/home/app/.local/bin/hermes: line 4:
/home/app/.hermes/hermes-agent/venv/bin/hermes: cannot execute: required file not
found`, plus `~/.cache` Permission denied and a bogus "no python" on reinstall.
Root cause: the host Hermes is the official **git/uv** installer — a Python venv
whose launcher shebang hardcodes `/home/<host>/.hermes/.../python3`, and whose
`venv/bin/python` links to a uv-managed Python under
`~/.local/share/uv/python/cpython-3.11.../bin/python3.11` (OUTSIDE `.hermes`).
Mounting only `~/.hermes` into a container with `HOME=/home/app` leaves both
absolute paths missing → ENOENT on the interpreter. A venv is not relocatable, so
no UID tweak fixes it (`chmod 777` only cleared the permission half). The separate
UID `1001` showed because an earlier rebuild changed `APP_UID`, orphaning the
once-chowned `cd-home` volume.

**Decision (reverses the earlier "self-contained Hermes" memo):** the user wants
the host's `~/.hermes` shared into the container. So we **mirror the host** rather
than relocate the venv.

**What changed in `deploy/docker/` + `docker-compose.yml`:**
- `docker-compose.yml`: bind-mount host `~/.hermes` → `/home/app/.hermes` (rw) AND
  host `~/.local/share/uv` → `/home/app/.local/share/uv` (ro, the uv Python the
  venv needs). New build arg `HERMES_HOST_HOME` (default `/home/debian`),
  `APP_UID`/`APP_GID` default `1000` (match the host `~/.hermes` owner),
  `HERMES_INSTALL_CMD` default `""` (host provides Hermes). New overrides
  `CD_HERMES_HOST_DIR` / `CD_HERMES_UV_DIR`.
- `Dockerfile`: at build time (root) create a symlink `$HERMES_HOST_HOME ->
  /home/app` so the venv's hardcoded `/home/<host>/...` paths resolve, and a
  `/usr/local/bin/hermes` shim that `exec`s `$HOME/.hermes/hermes-agent/venv/bin/hermes`
  (unsets PYTHONPATH/PYTHONHOME, like the host shim). No in-image Hermes install by
  default. `HOME` stays `/home/app`, so Hermes reads its login/config/memory from
  the mounted host `~/.hermes`.
- `entrypoint.sh`, `coding-dashboard.docker.env.example`, `README.md`: updated to
  describe mirror mode (Hermes is host-shared; Claude/Codex stay in `cd-home`).

**Verified** against `node:20-bookworm-slim` (which ships NO system Python — moot,
since Hermes uses the host's uv Python): the host's uv-Python 3.11.15 runs in the
container, `import hermes_cli` works, and a freshly built image running as the
non-root `app` user (uid 1000) with the mounts resolves `hermes` on PATH and prints
`Hermes Agent v0.16.0`.

**Important — deploy on the dest host:** build matched to it and recreate the
orphaned home volume once:
```
APP_UID=$(stat -c %u ~/.hermes) APP_GID=$(stat -c %g ~/.hermes) HERMES_HOST_HOME=$HOME docker compose build
docker compose down && docker volume rm coding-dashboard_cd-home   # clears only Claude/Codex logins
docker compose up -d                                               # cd-data (DB/repos) untouched
```
`HERMES_HOST_HOME` must equal the home of the user that owns `~/.hermes`
(the path baked into the venv launcher). See README "Deploy on the dest server".

### 2026-06-18 - codex (Docker: default app UID now matches host ~/.hermes)

**Problem:** Even after removing the base image's `node` user, a plain
`docker compose build` still created `app` as UID/GID `10001` because the Docker
build args defaulted to `10001`. The default Hermes bind mount points at the
host's `~/.hermes`, which is commonly owned by UID/GID `1000`, so inside the
container it appeared as numeric `1000:1000` instead of `app:app` and Hermes
could not access it.

**What changed in `deploy/docker/`:**
- `Dockerfile`: `APP_UID` / `APP_GID` now default to `1000`, relying on the
  existing `node` user/group removal so `app` can safely take the common host
  UID/GID. Hosts with different owners can still override the build args.
- `docker-compose.yml`: build-arg defaults now pass `1000:1000` instead of
  `10001:10001`, so `docker compose build` fixes the screenshot's `~/.hermes`
  owner mismatch for the common Linux setup.
- `coding-dashboard.docker.env.example` and `README.md`: updated Docker docs to
  explain the new default and when to use `APP_UID=$(id -u) APP_GID=$(id -g)`.

**Important:** Effective after rebuilding the image (`docker compose build` or
`docker compose up -d --build`). If the host `~/.hermes` is not owned by
`1000:1000`, rebuild with matching `APP_UID` / `APP_GID`.

### 2026-06-18 - claude (Docker: ~/.hermes owned by 'node' instead of 'app' -> Permission denied)

**Problem:** In the Docker deployment `~/.hermes` ended up owned by `node` rather
than `app`, so Hermes failed with *Permission denied*. Root cause: the
`node:20-bookworm-slim` base image ships a `node` user/group at **UID/GID 1000**
— the most common host UID. That collides with the documented build workflow
`APP_UID=$(id -u) APP_GID=$(id -g) docker compose build` both ways:
- With `APP_UID=1000`, `groupadd -g 1000 app` / `useradd -u 1000` failed
  ("GID/UID already exists") because `node` already held 1000 -> the build aborted.
- Without the override (default 10001), the bind-mounted host `~/.hermes`
  (host uid 1000) showed up inside the container as owned by `node`, and the
  `app` user (10001) could not read/write it -> Permission denied.

**What changed in `deploy/docker/`:**
- `Dockerfile`: the `app` user-creation step now removes the base image's `node`
  user/group first (`userdel -r node` / `groupdel node`, both best-effort) before
  `groupadd -o`/`useradd -o app`. The `-o` flags keep creation tolerant of any
  other pre-existing id collision. Result: `APP_UID=$(id -u)=1000` now builds
  cleanly and the bind-mounted host `~/.hermes` is owned by `app`, not `node`.
  Verified against `node:20-bookworm-slim` for both `APP_UID=1000` and `10001`:
  the app user is created and owns `~/.hermes` at the requested uid in each case.
- `coding-dashboard.docker.env.example`: documented the node-UID collision and
  that you must build with `APP_UID/APP_GID=$(id -u)/$(id -g)` so `app` owns the
  bind-mounted host `~/.hermes`.

**Important:** Effective after `docker compose build` (rebuild required) +
`docker compose up -d`. Build with `APP_UID=$(id -u) APP_GID=$(id -g)` so the
container's `app` user matches the host owner of `~/.hermes`.

### 2026-06-18 - claude (Docker: Hermes "cannot execute: Permission denied" fixed)

**Problem:** In the Docker container, `hermes` failed with
`/home/app/.hermes/hermes-agent/venv/bin/hermes: cannot execute: Permission
denied` (`EACCES` on `execve`, caused by a missing exec bit on the venv launcher).
There were three causes in the Docker deployment:
1. `HERMES_INSTALL_CMD` ran in `deploy/docker/Dockerfile` as **root** and before
   `ENV HOME=/home/app` / `USER app`, so the HOME-based installer landed in
   `/root/.hermes` instead of the app user's home.
2. `/home/app/.local/bin` (where the Hermes launcher shim lives) was **not on PATH**,
   so even a correct install was not found by the backend subprocess.
3. Some install / volume-restore paths lose the exec bit on the venv launcher.

**What changed in `deploy/docker/`:**
- `Dockerfile`: moved `HERMES_INSTALL_CMD` out of the root npm `RUN` step and ran it
  after user creation as the app user with the correct HOME
  (`runuser -u app -- env HOME=/home/app sh -c "$HERMES_INSTALL_CMD"`).
  `HERMES_NPM_PKG` remains a global npm install (-> `/usr/local/bin`). PATH was
  extended with `/home/app/.local/bin` so both the shell and the backend subprocess
  can resolve `hermes` by name.
- `entrypoint.sh`: added a self-heal step before the availability check - `chmod u+rx`
  on `~/.local/bin/hermes` and `chmod -R u+rx` on `~/.hermes/hermes-agent/venv/bin`,
  idempotent on every boot. Since the entrypoint is in the image, not the volume, a
  rebuild plus `docker compose up -d` can repair an already broken installation in the
  `cd-home` volume without rerunning the installer. The login hint also mentions Hermes
  when present.
- `coding-dashboard.docker.env.example`: documented the Hermes install paths
  (build args vs. manual install into the home volume), including the note that a
  pre-Hermes-generated `config.yaml` must have `agents.hermes.enabled: true` because
  the YAML `merged.update(spec)` path in `load_agents_config` preserves an explicit
  `enabled: false`.

**Important:** Effective after `docker compose build` + `docker compose up -d`.
The backend only exposes Hermes when `agents.hermes.enabled: true` is set in the
`config.yaml` inside the `cd-config` volume. Not committed/pushed yet.

### 2026-06-14 - claude (6 features: Codex output, fullscreen, AGENTS refresh, file browser, parallel branches, agent dashboard)

**Implemented six features.**
1. **Cleaner Codex output:** new `stream_format: "codex"` with `_CodexParser` in
   `agents.py`. It strips `[ISO timestamp]` prefixes, the startup banner
   (workdir/model/provider/approval/sandbox/reasoning/session/version/`---`) only at
   the head (`_past_header` gate so answer text that happens to start with `model`
   or `session` survives), the `tokens used` footer, and the `User instructions:`
   prompt echo. `bash -lc 'cmd' in /dir` becomes `$ cmd`. Registered in `_make_parser`;
   `config.py` sets Codex to `stream_format="codex"`, and the literal enum now includes
   `"codex"`.
2. **Fullscreen for live / history / session output:** added `FullscreenShell`
   (portal + Esc-to-close) and `IconButton` in `ui.tsx`. `TaskConsole` gained a
   fullscreen toggle (+ `title` / `onDismiss`); `SessionTerminalModal` got an
   `expanded` state (container class swap); `ProjectDetail` now has a fullscreen
   overlay (`fsOutput`) for history output.
3. **AGENTS.md refreshes automatically after a run:** `ProjectDetail` keeps an
   `agentsMdLoaded` ref + `reloadAgentsMd()`. `onTaskDone()` triggers the reload as
   soon as a task completes.
4. **Per-project file browser with side preview:** new routes
   `GET /projects/{id}/files` (directory listing, hides `.git`) and
   `GET /projects/{id}/file` (text content, NUL -> binary, latin-1 fallback, 512 KB
   cap, buffered read instead of `read_bytes()`, OSError -> 403). Added traversal
   guard `_resolve_within`. New schemas `FileEntry` / `DirListing` / `FileContent`,
   `api.listFiles` / `readFile`, and a new `FileBrowser.tsx` (side-by-side, breadcrumb,
   reqId race guard, fullscreen viewer) embedded in `ProjectDetail`.
5. **Multiple tasks / goals / sessions in parallel on their own branches + merge:**
   major refactor in `task_runner.py`. Each isolated run gets a git worktree on branch
   `cd/<mode>/<task_id[:8]>` (start point `"HEAD"`, not `main`, otherwise DWIM can
   accidentally create `main`). The agent run happens **outside** the project lock
   (real parallelism); only worktree add and merge are short critical sections under
   the lock. `_merge_worktree_branch`: commit in the worktree -> merge into the project
   checkout -> push `HEAD:base_branch` -> cleanup. A conflict aborts the merge but
   keeps and pushes the feature branch (default branch stays clean); solo runs
   fast-forward (no merge commit noise). New git helpers (`add_worktree`,
   `remove_worktree`, `merge_branch`, `branch_exists`, `push_ref`, `delete_branch`,
   `prune_worktrees`). Added `Task.merge_state` (append-only). `SessionManager` works
   the same way (worktree CWD + merge-back in `end_session`). `reset_interrupted`
   cleans the worktrees root.
6. **Running agents dashboard on the home page:** `GET /tasks/running`
   (`RunningTaskOut` with project_name/slug), new `RunningAgents.tsx` (polls every
   3 seconds), embedded above the grid in `Projects.tsx`.

**Result:** all six features are in place. `read_file` now reads only a bounded chunk
(no full reads of large files) and maps OSError to 403. `smoke.py` was extended with
`test_codex_parser`, `test_worktree_merge` (clean FF, second merge, conflict path),
and file browser / `/running` checks. Python compile, **120 smoke checks**,
frontend typecheck, and Vite build are green. Effective after `update.sh` /
`systemctl restart coding-dashboard`. Not committed/pushed yet.

### 2026-06-13 - codex (Session paste: Ctrl+V no longer treated as TUI bytes)

**What changed:** In session mode, Ctrl+V (and Ctrl+Shift+V) triggered an image-paste
attempt in every TUI, which fails in our headless PTY without X11 / Wayland. Codex
reported *"Failed to paste image: clipboard unavailable: Unknown error while
interacting with the clipboard: X11 server connection timed out because it was
unreachable"*, Claude Code replied *"no image found"*, and Hermes ignored the paste.
- `frontend/src/components/SessionTerminalModal.tsx` `keyToBytes()` now returns `null`
  for Ctrl+V (lowercase and Shift-uppercase) **before** the Ctrl+X branch converts the
  raw `\x16` and sends it to the TUI. `onTerminalKeyDown` returns early on `null`
  without `preventDefault()`, so the browser default survives, the `paste` event fires,
  `onTerminalPaste` handles it, and bracketed paste is sent.
- `onTerminalPaste` now reads `text/plain` (with `text` fallback) instead of only
  `text` and drops anything that is not text, instead of forwarding a paste that the
  TUI cannot process without an OS clipboard.

**Result:** Ctrl+V in the browser terminal dialog sends the clipboard text as a
DEC bracketed paste to the TUI; the TUI no longer attempts an image paste and the
prompt receives plain text normally. Python compile, TypeScript typecheck, Vite build,
and smoke tests are green.

### 2026-06-13 - codex

**What changed:** Session mode paste support (clipboard, including multiline) and
commit / push status in the history header line.
- Backend `task_runner.SessionManager.start()` writes `\x1b[?2004h` to the TUI right
  after PTY setup to enable DEC bracketed paste mode (mode 2004). TUIs that do not
  enable it themselves still treat pastes as one event; TUIs that already enabled it
  are unaffected.
- Frontend `SessionTerminalModal.onTerminalPaste` wraps clipboard text in
  `\x1b[200~ ... \x1b[201~`. That keeps Claude Code, Codex, Hermes, etc. from
  interpreting multiline pastes as a series of Enter submissions. Without those
  sequences, every `\n` in the paste submitted the partially completed input.
- Frontend `ProjectDetail` now shows `⎇ <commit hash>` (as a GitHub link when the
  repo has one) plus `pushed ✓` or `not pushed` in the header line of each history
  entry. Running / queued tasks show a subtle `—` placeholder. This applies to all
  tasks, not just sessions, matching the footer line in `SessionTerminalModal` after
  `end_session`.

**Result:** pasting from the browser into an active TUI session works with multiline
content and prompt integrity, and the Git status of every finished task is visible
without expanding the entry. Python compile, frontend typecheck, frontend build, and
the full smoke test (96 checks) passed.

### 2026-06-13 - codex

**What changed:** Fixed the black screen in session mode.
- `POST /api/sessions` now waits until the `SessionManager` has created the PTY and
  live channel, so the browser WebSocket cannot fall into the replay / done path
  before the channel exists.
- `/api/ws/sessions/{task_id}` now waits briefly for just-started sessions before
  treating them as non-live.
- PTY / fork start failures are persisted in `Task.output`, status, and summary
  instead of only being lost inside the channel.
- `SessionTerminalModal` now shows connecting / empty / error states explicitly;
  WebSocket / Cloudflare errors are no longer just a black terminal surface. When
  the mini renderer cannot produce a visible screen from TUI control sequences, it
  falls back to ANSI-stripped text.

**Result:** the session start race is closed and the terminal UI is hardened against
empty output, closed WebSockets, and a missing `ResizeObserver`. Python compilation,
frontend typecheck, frontend build, and an isolated fake-PTY session check succeeded.
The full smoke test is still blocked by the known FastAPI / Starlette `TestClient`
hang (`timeout 70s`).

### 2026-06-13 - codex (Session WebSocket: HTTPBearer crash fixed)

**What changed:** Fixed "Terminal WebSocket could not be opened" and "Terminal
connection closed" in session mode.
- `routers/sessions.py` had `dependencies=[Depends(get_current_user)]` at router
  level. `get_current_user` internally calls `HTTPBearer`, which needs a real
  `Request` with a Bearer header. During WebSocket handshakes the request is `None`
  -> `TypeError: HTTPBearer.__call__() missing 1 required positional argument:
  'request'` -> WebSocket closes immediately without data -> the browser saw `onerror`
  and `onclose`.
- Fix: removed router-level `dependencies=`. The three HTTP routes
  (`POST /sessions`, `GET /sessions/{task_id}`, `POST /sessions/{task_id}/end`) now
  attach `Depends(get_current_user)` explicitly in the function signature. The
  `@router.websocket("/ws/sessions/{task_id}")` route still authenticates manually via
  `user_from_token(token)` (query parameter).
- Verified: the patch routing tree for the WebSocket route is now empty (no
  HTTPBearer), HTTP routes keep auth, all 47 smoke tests passed.

**Result:** session mode is fully interactive again after the next
`update.sh` / `systemctl restart coding-dashboard`. Before the fix, the PTY kept
running in the background and collecting output in `Task.output`, but the browser
could only display the persisted transcript because the WebSocket immediately failed
with the TypeError.

### 2026-06-12 - codex (Codex config quoting bug fixed)

**What changed:** `_write_codex_config` stripped quotes from all values when reading
(`strip('"')`), but only added them back for `model` / `model_reasoning_effort`, so
`service_tier = "default"` became `service_tier = default` (without quotes), which
blocked Codex.
- Fix: store the raw value without quote stripping; formatting is preserved.

**Result:** effective after `systemctl restart coding-dashboard`.

### 2026-06-12 - codex

**What changed:** PTY-based session mode - the agent now runs in a real PTY, and all
keyboard input (arrows, Ctrl+C, etc.) is forwarded 1:1. Added `session_command` to
`AgentSpec`, switched from `asyncio.subprocess` to `os.openpty` + `os.fork`, and the
frontend now has a green terminal display.

**Result:** effective after `systemctl restart coding-dashboard`.

### 2026-06-12 - codex

**What changed:** Updated the model list. Claude Code now has `fable` as a fourth
model; Codex uses `gpt-5.4`, `gpt-5.5`, and `gpt-5.4-mini` (instead of the old
`gpt-5.1-*`). When an effort level is set for Claude Code, `~/.claude/settings.json`
is also written (`effort` key) so the selected level is definitely used, not only via
the `--effort` CLI flag.

**Result:** effective after `systemctl restart coding-dashboard`.

### 2026-06-12 - codex (Fix)

**What changed:** Upgraded `@vitejs/plugin-react` from `^4.3.2` to `^6.0.2`
(now also requires `@rolldown/plugin-babel@^0.2.3` and
`babel-plugin-react-compiler@^1.0.0` as peer deps).

**Result:** fixes the npm 10 + Vite 8 peer dependency conflict. Build succeeded.

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
  pages/ (Login, Projects, ProjectDetail, SessionPage wrapper)
  components/ (TaskConsole, SessionTerminalModal, TaskImages, ui, ...)
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
self-contained container - uvicorn serves API + WS + the built SPA (no nginx inside),
with the agent CLIs baked in (claude + codex + hermes by default - hermes via its
official HOME-based installer as `app`, placing the shim under `/home/app/.local/bin`
and the venv under `/home/app/.hermes`; override / disable via the `HERMES_NPM_PKG` /
`HERMES_INSTALL_CMD` build args). Runs as non-root `app`; state in
named volumes (`cd-data` = DB + repos, `cd-config` = config.yaml, `cd-home` = claude
+ codex logins `~/.claude` / `~/.codex`). **Hermes is the exception: `~/.hermes` is a
bind mount of the *host's* `~/.hermes`** (overridable via `CD_HERMES_HOST_DIR`),
nested over the `cd-home` volume so only that subdir comes from the host - so Hermes
shares its login / data with the host. Because bind mounts keep host uid/gid, the
image defaults `APP_UID` / `APP_GID` to `1000:1000` (safe because the Dockerfile
removes the base `node` user/group first); override those build args to
`$(id -u)` / `$(id -g)` when the host Hermes dir has a different owner. Auth is
**interactive login only**
(`docker compose exec dashboard claude`), no API keys on disk. `deploy/docker/entrypoint.sh`
generates `config.yaml` on first boot from `default_agents()`, enabling only the CLIs
found on `PATH` (so a missing agent is not a dead UI entry). Secrets via
`deploy/docker/coding-dashboard.docker.env` (gitignored; `.example` committed).

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
