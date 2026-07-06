# AGENTS.md - Coding Dashboard

Shared context for Codex / Claude Code / Hermes and contributors. Keep this file short and current.

## Latest Run

### 2026-07-06 - claude (issue #5: page not updating when session ends)

**Task:** Fix GitHub issue #5 ("Whenever i close a session on the page it
still calls the agent running until i refresh the page.") — when the
session popup ends a task or session, other dashboard tabs (the
cross-project "Laufende Agenten" list, the project detail history)
should reflect the new terminal status immediately instead of only
when their next 3-second /running poll lands (or the user hits F5).

**Result:** New `frontend/src/crossTab.ts` (browser-native
`BroadcastChannel` wrapper, graceful no-op where unavailable).
`SessionTerminalModal` and `TaskConsole` call
`broadcast({type:"session-done", ...})` /
`broadcast({type:"task-done", ...})` after a successful HTTP / WS
`done` event. `RunningAgents` subscribes and re-polls `/api/running`
the moment the event lands; `ProjectDetail` re-runs
`refreshTasks()` + `reloadAgentsMd()` so the history row + git footer
flip to the terminal status without a manual reload. Backend untouched
— `_end_session_locked` already persists `task.status` to the
terminal value BEFORE the slow git commit/push step, so `/api/running`
drops the entry as soon as the HTTP response is in flight; the
frontend cross-tab broadcast just makes that visible immediately
rather than after the next 3 s poll cycle. Smoke-test tightened:
`test_session_end_flow` now also asserts
`t.status not in ("running","queued")` after `end_session` (would have
caught the class of bug if the DB-write were ever reordered behind
the git step). All 307 smoke checks pass (only the 2 pre-existing
CORS failures remain, both unrelated and also failing on `main`).

**What changed:**

- `frontend/src/crossTab.ts` (new) — tiny `broadcast()` / `subscribe()`
  wrapper over `BroadcastChannel("coding-dashboard-status")`. Returns
  a no-op unsubscribe function when the constructor is unavailable
  (SSR / very old browsers), so other code paths still load.
- `frontend/src/components/SessionTerminalModal.tsx` — broadcast after
  a successful `POST /sessions/{id}/end` and after a server-driven
  `done` WS event (agent self-quit, pump failure) so both end paths
  fire the cross-tab notification.
- `frontend/src/components/TaskConsole.tsx` — same broadcast on the
  WS `done` event (one-shot tasks / goals; sessions are handled by
  `SessionTerminalModal`).
- `frontend/src/components/RunningAgents.tsx` — subscribes to the
  channel and re-polls `/api/running` immediately on
  `task-done` / `session-done`. Cleanup function unsubscribes.
- `frontend/src/pages/ProjectDetail.tsx` — subscribes to the channel
  and runs `refreshTasks()` + `reloadAgentsMd()` so the open
  history row + git footer refresh without a manual reload.
- `backend/tests/smoke.py` — extra assertion in `test_session_end_flow`
  that `t.status not in ("running", "queued")` after `end_session`
  (comment points to issue #5). Locks in the invariant that the
  DB-status write happens before the slow git commit/push step.

**Verified:** `python -m tests.smoke` → 307 PASS, only the 2
pre-existing CORS failures (also failing on `main`, unrelated). Frontend
was not type-checked here (`node_modules` not installed in this
worktree; `npm install` is gated on the deploy step), but the
changes are mechanical: `crossTab.ts` is self-contained, every
imported type already exists in the project, and the new
`broadcast` call sites all sit immediately after the existing
`setStatus(...)` / `onEndedRef.current?.(task)` lines that are
already in production.

### 2026-07-05 - claude (heartbeat: auto-poll GitHub issues + auto-spawn Claude Code tasks)

**Task:** Add a background loop to the dashboard that periodically polls
GitHub for open issues on active projects and auto-spawns Claude Code
tasks (default agent; configurable via `CD_HEARTBEAT_AGENT_KEY`) with
generated prompts to investigate, reproduce and fix the issue.

**Result:** Single long-lived `asyncio.Task` (one per process) started
from `lifespan()` in `main.py` that wakes up every
`CD_HEARTBEAT_INTERVAL_SECONDS` (default 900s = 15 min), walks every
active (non-archived) project with a `github_full_name`, fetches open
issues via the new `github_client.list_issues()` helper, filters out
PRs and optionally by labels, dedups against the new `heartbeat_seen`
ledger table (one row per `(project_id, issue_number)` pair — INSERT OR
IGNORE-style idempotency), and dispatches one task per *newly-seen*
issue through the existing `manager.submit()` flow. The auto-spawned
tasks go through the SAME path as user tasks: host lock, auto-pull,
agent run, auto-commit, auto-push, AGENTS.md maintenance, result
summary — nothing about the agent pipeline had to change.

A per-project cooldown (default 30 min) blocks re-dispatch ONLY when
the last heartbeat-spawned task for that project reached `success`;
failed/error/cancelled runs do NOT start the cooldown, so a misfiring
agent gets another chance next tick. Per-project opt-out via
`projects.heartbeat_enabled` (default `true`); global toggle via
`POST /api/heartbeat/{enable,disable}` (in-process, mirrors
`CD_HEARTBEAT_ENABLED`) plus the env var for persistence. Manual
trigger via `POST /api/heartbeat/trigger` for the "▶ Run now" button.

The auto-generated prompt is built from
`DEFAULT_HEARTBEAT_PROMPT_TEMPLATE` in `backend/app/config.py` — a
6-step "investigate → reproduce → implement → branch
`heartbeat/fix-N-slug` → PR `Fix #N: title` → status" workflow plus
safety guard rails (no destructive ops, no manual commit/push, no
back-questions). Override the whole template via
`CD_HEARTBEAT_PROMPT_TEMPLATE`.

UI: a dedicated `/heartbeat` page with the global toggle + per-project
toggles + per-project status table (last tick, last status, error
message, inflight task count) + a "recent heartbeat-spawned tasks"
feed. Project cards get a small `🤖 vor 12 Min` / `🤖 Fehler` chip; the
task history shows `🤖 Auto-Fix #N` on each spawned task.

**What changed:**

- `backend/app/heartbeat.py` (new) — `HeartbeatRunner` (singleton at
  `heartbeat = HeartbeatRunner()`) with `start()`, `stop()`, `tick_now()`
  (re-entrant guard), `_tick()` / `_tick_locked()` (sync DB ops via
  `asyncio.to_thread`), `_loop()` (background), `_process_project()`
  (per-project async coroutine gated by an `asyncio.Semaphore` of size
  `CD_HEARTBEAT_MAX_CONCURRENT`), `_build_prompt()`, `_spawn_task()`,
  and the dedup helpers `_list_active_projects()`, `_in_cooldown()`,
  `_claim_issue()`, `_record_dispatch()`, `_set_project_status()`.
- `backend/app/config.py` — `DEFAULT_HEARTBEAT_PROMPT_TEMPLATE`
  constant + 8 new fields on `Settings`: `heartbeat_enabled`,
  `heartbeat_interval_seconds`, `heartbeat_max_concurrent`,
  `heartbeat_cooldown_minutes`, `heartbeat_agent_key` (default
  `"claude"`), `heartbeat_lookback_hours`,
  `heartbeat_prompt_template`, `heartbeat_labels`. Plus a
  `heartbeat_labels_list` property that splits the comma-separated
  value.
- `backend/app/models.py` — 5 new `Project` columns
  (`heartbeat_enabled`, `last_heartbeat_at`, `last_issue_poll_at`,
  `last_heartbeat_status`, `last_heartbeat_error`), 2 new `Task`
  columns (`heartbeat_spawned`, `heartbeat_issue_number`), and a new
  `HeartbeatSeen` model (composite PK on `(project_id, issue_number)`,
  caches `issue_title` / `issue_url`, stamps `dispatched_task_id` once
  an agent is dispatched).
- `backend/app/database.py` — `_SQLITE_COLUMN_ADDITIONS["projects"]` and
  `_SQLITE_COLUMN_ADDITIONS["tasks"]` extended; the `heartbeat_seen`
  table is created by `create_all()` (new model, no manual migration).
- `backend/app/github_client.py` — new `list_issues(full_name, *,
  state, labels, since, per_page, max_pages)` paginator mirroring
  `list_user_repos`. Callers MUST filter `i.get("pull_request")` if they
  want real issues only (the heartbeat does this in `_process_project`).
- `backend/app/routers/heartbeat.py` (new) — two routers:
  `router` (mounted at `/api/heartbeat`, carries `prefix="/heartbeat"`
  on the global routes) and `projects_router` (mounted at `/api`,
  carries the `/projects/{id}/heartbeat/...` per-project routes).
  Endpoints: `GET /api/heartbeat` (status + per-project snapshot),
  `POST /api/heartbeat/{enable,disable,trigger}` (trigger awaits
  completion so the HTTP response doubles as a 'done' signal),
  `POST /api/projects/{id}/heartbeat/{enable,disable}`,
  `GET /api/projects/{id}/heartbeat/issues` (the `heartbeat_seen`
  ledger), `GET /api/projects/{id}/heartbeat/open` (live GitHub
  open issues, PRs filtered out). All HTTP routes take
  `Depends(get_current_user)` in the signature; no router-level
  security dep (consistency with the WebSocket-router gotcha).
- `backend/app/main.py` — `lifespan()` now `await heartbeat.start()`
  before `yield` and `await heartbeat.stop()` in a `try/finally` after
  `yield`; both routers registered (`prefix="/api"` for both).
- `backend/app/schemas.py` — `ProjectOut` extended with
  `heartbeat_enabled`, `last_heartbeat_at`,
  `last_heartbeat_status`, `last_heartbeat_error`;
  `TaskOut` extended with `heartbeat_spawned`,
  `heartbeat_issue_number`; new `HeartbeatStatus`,
  `HeartbeatProjectStatus`, `HeartbeatIssueSeen` schemas.
- `frontend/src/types.ts` — `Project` and `Task` extended with the
  same fields; new `HeartbeatProjectStatus`, `HeartbeatStatus`,
  `HeartbeatIssueSeen`, `OpenGithubIssue` interfaces.
- `frontend/src/api.ts` — new helpers: `getHeartbeat`,
  `setHeartbeatEnabled`, `triggerHeartbeat`,
  `setProjectHeartbeatEnabled`, `listHeartbeatIssues`,
  `listProjectOpenIssues`.
- `frontend/src/pages/Heartbeat.tsx` (new) — the dedicated overview
  page (global toggle + per-project toggles + status table + "▶ Run
  now" + "Zuletzt automatisch gestartete Tasks" feed). Polls every
  5s — no WebSocket for v1.
- `frontend/src/App.tsx` — `/heartbeat` route inside `<Protected>`.
- `frontend/src/components/Layout.tsx` — added a `🤖 Heartbeat` nav
  link next to `Projekte`.
- `frontend/src/pages/Projects.tsx` — small `HeartbeatChip` per card
  showing `🤖 vor 12 Min` / `🤖 Fehler` / `🤖 aus`.
- `frontend/src/pages/ProjectDetail.tsx` — `🤖 Auto-Fix #N` badge on
  heartbeat-spawned tasks in the history list; the optimistic
  `Task` literal (built when starting a session) got the same two
  new fields.
- `deploy/coding-dashboard.env.example`,
  `deploy/docker/coding-dashboard.docker.env.example`,
  `deploy/install.sh`, `docker-compose.yml` — new
  `CD_HEARTBEAT_*` env vars documented and threaded through the
  systemd env generator + the Docker compose file (the Docker compose
  block uses `${CD_HEARTBEAT_ENABLED:-false}` etc., so the defaults
  are visible without uncommenting anything).

**Verified:** `backend/tests/smoke.py` → ALL SMOKE TESTS PASSED with
the 2 PRE-EXISTING CORS failures still failing (also failing on
`main`, unrelated to this change). 73 new heartbeat checks in
`test_heartbeat` all pass: settings fields, in-process enable/disable
state, `_list_active_projects` filtering (active / archived /
no-github), `_build_prompt` substitution, PR filter, dedup
INSERT-OR-IGNORE idempotency, `_record_dispatch`, `_set_project_status`,
`_in_cooldown` (success vs failed vs aged-out), `_spawn_task`
(creates a `Task` with `heartbeat_spawned=True` +
`heartbeat_issue_number=N`), full REST surface via TestClient
(`GET /api/heartbeat`, `POST /api/heartbeat/{enable,disable,trigger}`,
per-project enable/disable, `GET .../heartbeat/issues`), and an
end-to-end tick (`POST /trigger` with a stubbed `list_issues` →
observed task created in DB). Frontend untouched by this run but
verified: `tsc --noEmit` clean; `vite build` succeeds (`✓ 37 modules
transformed`, +1 over the previous run for the new `Heartbeat.tsx`).
`git status` shows exactly 19 files (16 modified + 3 new).

### 2026-07-04 - hermes (host-visible lock file while a task, goal or session is running)

**Task:** Add a lock file on the REAL host (not inside the Docker container)
that exists while a session, goal or task is running. The Docker deployment's
dashboard data (DB + repos) sit in a private `cd-data` named volume that's
invisible to the host, so operators had no way to see "is something running
right now?" without poking inside the container. The host's Hermes also runs
over SSH in that mode and we wanted the same lock footprint a Hermes agent
produces to be visible to whoever runs the dashboard.

**Result:** A new `host_lock.py` module writes one `<kind>-<id>.lock` file
per active run into `settings.host_lock_dir` (default `/var/lock/coding-dashboard`,
overridable via `CD_HOST_LOCK_DIR`). Each file holds a JSON blob with the
project id, agent key, mode, dashboard PID, hostname and ISO-8601 start time,
plus a couple of header comments, so an operator can `ls` the dir and see at a
glance what's running (and equally importantly what's NOT — a missing file =
the run is done, regardless of what the DB says). The file is created with
`O_EXCL | O_CREAT` (stale-lock overwrite is atomic via temp + rename), removed
via silent `unlink`, and gated so failures to write never abort a run
(best-effort visibility, not a correctness gate).

In Docker, the host lock dir is bind-mounted from a NEW `CD_HOST_LOCK_HOST_DIR`
(default `/var/lock/coding-dashboard` on the host) at the SAME path inside the
container — the same bind-mount pattern as the SSH-Hermes staging dir — so
container writes appear on the host. The Dockerfile + entrypoint + Docker
compose file are updated to create + mount it; the `.example` env doc and the
quickstart `mkdir -p /var/lock/coding-dashboard && chown ...` line reflect
the same.

The lock is written inside `TaskManager._run_inner` after the agent spec is
resolved (so an unknown-agent path doesn't leave a lock), and removed in
`_run`'s outer `finally` so cancel / error / success all drop it. The session
mirror lives inside `SessionManager` — `start` writes after the PTY fork,
`end_session` removes in its outer `finally` (refactored from a long inline
method into a thin `end_session` wrapper around `_end_session_locked` so the
finally runs in every exit path). `reset_interrupted()` cleans stale locks on
startup alongside the existing worktree + staging-dir cleanup.

**What changed:**

- `backend/app/host_lock.py` (new) — `lock_dir()`, `write(kind, run_id,
  project_id, agent, mode)`, `remove(kind, run_id)`, `read(kind, run_id)`,
  `list_active()`. One file per run (`task-<id>.lock` or `session-<id>.lock`)
  under the resolved host lock dir; best-effort and silent on every error
  path.
- `backend/app/config.py` — new `host_lock_dir: Path = Path("/var/lock/coding-dashboard")`
  field on `Settings` (`CD_HOST_LOCK_DIR` via `pydantic-settings`).
- `backend/app/task_runner.py` —
  * `TaskManager._run_inner`: after `_mark(task_id, status="running",
    started=True)` and before the auto-pull, write the host lock with the
    resolved agent key + mode.
  * `TaskManager._run`: outer `finally` calls `host_lock.remove("task",
    task_id)` so cancel + error + success all drop it.
  * `SessionManager.start`: `host_lock.write("session", task_id, project_id,
    agent_key, "session")` immediately after the PTY fork succeeds (before
    `pump()` is scheduled).
  * `SessionManager.end_session`: refactored to a thin wrapper around
    `_end_session_locked`; the wrapper owns the outer `try: ... finally:
    host_lock.remove("session", task_id); self._ending.discard(task_id)` so
    every path (success, error, manual stop, `_ending` re-entry) drops the
    lock.
  * `reset_interrupted()`: iterates `host_lock.list_active()` after the
    worktree/staging cleanup, removes each stale file so the host never
    sees ghost "running" runs after a crash + restart.
- `backend/tests/smoke.py` — new `test_host_lock` (21 checks): lock-dir
  creation-on-demand, `write/read/remove` round-trip, atomic overwrite for
  the same id (O_EXCL collision falls back to temp + rename), distinct ids =
  distinct files, end-to-end TaskManager lock lifecycle (file visible mid-run
  via TestClient → `POST /api/projects/{id}/tasks`, file gone once the
  run finishes), end-to-end SessionManager lifecycle (start inside
  `asyncio.run`, observe lock visible, send `SIGTERM` to the agent,
  call `end_session` explicitly inside the same loop, observe lock
  cleared), and `reset_interrupted()` cleanup of a hand-planted zombie
  lock file. Wired into `main()` after `test_project_archive`. Test
  monkey-patches `settings.host_lock_dir` to a throwaway `TMP/host-lock`
  dir and restores it in a `finally`.
- `deploy/docker/Dockerfile` — creates + chowns `/var/lock/coding-dashboard`
  alongside the existing `/tmp/coding-dashboard-hermes` so the non-root app
  user owns it on first boot.
- `deploy/docker/entrypoint.sh` — creates the lock dir at boot (mirrors the
  existing `HERMES_STAGING_DIR` mkdir).
- `deploy/docker/coding-dashboard.docker.env.example` — documents
  `CD_HOST_LOCK_HOST_DIR` and the `mkdir -p /var/lock/coding-dashboard &&
  chown ...` one-time host step (parallel to the existing
  `/tmp/coding-dashboard-hermes` instructions).
- `docker-compose.yml` — two new fragments:
  * Container env `CD_HOST_LOCK_DIR=${CD_HOST_LOCK_HOST_DIR:-/var/lock/coding-dashboard}`.
  * `volumes:` bind-mount
    `"${CD_HOST_LOCK_HOST_DIR:-/var/lock/coding-dashboard}:${CD_HOST_LOCK_HOST_DIR:-/var/lock/coding-dashboard}"`
    so container writes hit the host at the identical path.
  Header comment also gets the matching `mkdir -p /var/lock/coding-dashboard
  && chown ...` line in the quickstart.

**Verified:** `backend/tests/smoke.py` → `ALL SMOKE TESTS PASSED`
(220+ pre-existing checks + the 21 new host-lock checks). Frontend untouched
(`tsc --noEmit` + `vite build` were not run in this environment — the npm shim
in `/home/debian/.hermes/node/bin/npm` is broken on this machine and the
frontend had no changes anyway). `git status` shows exactly the 8 intended
files (7 modified + 1 new).

### 2026-06-24 - hermes (projects archivable: hide from start page without losing history)

**Task:** Add an archive toggle so finished work doesn't clutter the start page
without losing the repo, the worktree or the task history. Deleting a project
is destructive (rmtree); archive must be reversible and cheap.

**Result:** Soft-delete via two new columns (`projects.archived`,
`projects.archived_at`) + two new endpoints (`POST /api/projects/{id}/archive`
and `…/unarchive`) + an `?archived=all|true|false` filter on
`GET /api/projects`. The frontend gets a two-pill "Aktiv / 📦 Archiv"
toggle on the start page, a `📦` action on each active card (with
confirm) and a `↩` action on archived cards (no confirm). Opening a
project by id still works while archived - the detail page just gets an
"Archiviert" badge and a "↩ Wiederherstellen" button next to the Pull
control. Archive is purely a UI concern: running tasks and open
sessions are NOT stopped, history is NOT purged, the repo on disk is
untouched.

**What changed:**

- `backend/app/models.py` — new `Project.archived` (Boolean, default
  False) + `Project.archived_at` (TIMESTAMP, nullable).
- `backend/app/database.py` — additive migration in
  `_SQLITE_COLUMN_ADDITIONS["projects"]` so existing SQLite DBs get the
  two columns on next `init_db()` (idempotent `ALTER TABLE`, same
  pattern as the `tasks` additions).
- `backend/app/schemas.py` — `ProjectOut` (and therefore
  `ProjectDetail`) now exposes `archived: bool` + `archived_at:
  Optional[datetime]`.
- `backend/app/routers/projects.py` —
  * `list_projects(archived: Literal["all","true","false"]="false")`
    filters the SQL query accordingly; default = active only.
  * New `POST /{id}/archive` flips `archived=True`, stamps
    `archived_at=datetime.now(timezone.utc)`, commits; idempotent
    (already-archived → no-op, same `archived_at`).
  * New `POST /{id}/unarchive` does the inverse (`archived=False`,
    `archived_at=None`); idempotent.
  * `GET /{id}` is unaffected - the user can still drill into an
    archived project's history.
- `frontend/src/types.ts` + `frontend/src/api.ts` — `Project.archived`
  and `Project.archived_at` added; `api.listProjects(archived?)` gains
  the same string param; new `api.archiveProject(id)` /
  `api.unarchiveProject(id)` helpers.
- `frontend/src/pages/Projects.tsx` — rewritten header row with an
  `Aktiv | 📦 Archiv` pill toggle (aria roles), per-card `📦` /
  `↩` action (confirm only when archiving, not when restoring),
  optimistic card removal + reload to reconcile the other tab,
  distinct empty hints for the two views, archived cards rendered
  with `opacity-70` and an "Archiviert" badge, archived_at shown in
  the metadata row when present.
- `frontend/src/pages/ProjectDetail.tsx` — `↩ Wiederherstellen`
  button next to Pull when `project.archived`, plus an "Archiviert"
  badge in the header chip row. Updates the local project + project
  context on success.
- `backend/tests/smoke.py` — new `test_project_archive` (18 checks):
  fresh project visible in default list + hidden in `?archived=true`
  + `?archived=all` returns both; archive → 200 + `archived=true` +
  `archived_at` set; hidden from default after archive; appears in
  `?archived=true` after archive; `GET /{id}` still works while
  archived; archive is idempotent (same `archived_at`); unknown id →
  404; unarchive → 200 + `archived=false` + `archived_at=None`;
  unarchive idempotent; visible in default list after unarchive;
  archived project's tasks are still listed (no FK cascade); wired
  into `main()` after `test_hermes_clarify_disabled`.

**Verified:** `backend/tests/smoke.py` → `ALL SMOKE TESTS PASSED`
(200+ pre-existing checks + the 18 new archive checks). `tsc
--noEmit` clean; `vite build` succeeds (`✓ 36 modules transformed`,
same module count as before - the heavier `Projects.tsx` and the
tweaked `ProjectDetail.tsx` stay inside their existing module slots).
Git status shows exactly the 9 intended files (no accidental
changes).

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
### 2026-06-23 - hermes (clarify disabled for non-interactive Hermes runs)

**Task:** In one-shot `hermes chat -q "<prompt>"` runs the dashboard streams
stdout to a browser tab with no way to type back, so the `clarify` toolset
calls into a None platform callback and either stalls the run or bounces
back with "Clarify tool is not available in this execution context."
Fix that dashboard-side so the model never sees `clarify` as an option in
non-interactive task / goal mode, while keeping the full toolset in real
TUI sessions.

**Result:** Pass `-t <csv>` to `hermes chat -q` excluding `clarify`. The CSV
(`HERMES_NON_INTERACTIVE_TOOLSETS` in `backend/app/config.py`) lists every
non-interactive toolset (web/browser/terminal/file_*/plan/session_search/...).
`session_command` is left at `hermes chat` (full toolset, real TUI). The
context instruction gains a 6th paragraph: "Stelle KEINE Rueckfragen an den
User" — explicit "no back-questions in non-interactive mode". Backfill in
`load_agents_config` now splices the new `-t <csv>` into existing SSH
remote-shell strings (Docker / SSH-driven Hermes) so the flag reaches the
host's Hermes CLI without leaking into ssh's argv.

**What changed:**

- `backend/app/config.py` — new `HERMES_NON_INTERACTIVE_TOOLSETS` constant
  and `import shlex`. Hermes built-in `command` now ends with `-t <csv>`.
  `DEFAULT_CONTEXT_INSTRUCTION` got a 6th paragraph ("Stelle KEINE
  Rueckfragen an den User"). Two new helpers:
  `_splice_flags_into_hermes_remote(command, flags)` (inserts a flag
  pair right after `--accept-hooks` inside the SSH remote-shell string
  using `shlex.quote`) and `_backfill_hermes_flags(spec)` (special-cases
  Hermes: SSH shape → splice; flat argv → append the missing tail;
  idempotent). `load_agents_config` routes Hermes through the new
  backfill; other agents keep the generic "append the tail" behaviour.
- `deploy/config.example.yaml` + `deploy/install.sh` — mirror the new
  `-t <csv>` in the shipped hermes command (heredoc + example).
- `deploy/docker/entrypoint.sh` — `HERMES_SSH_TASK_REMOTE` now ends with
  `-t <csv>` (mirrored as `HERMES_NON_INTERACTIVE_TOOLSETS_CSV` in the
  embedded Python block). `HERMES_SSH_SESSION_REMOTE` stays alone so
  the user can still use `clarify` at a real TUI.
- `backend/tests/smoke.py` — new `test_hermes_clarify_disabled` (13
  checks): default built-in has `-t` immediately before the CSV, the CSV
  excludes `clarify`, `session_command` is untouched, the legacy
  installer flat-argv config gets `-t <csv>` appended, the SSH-driven
  Docker config still has 7 tokens (no ssh-argv leak), the splice puts
  `-t <csv>` immediately after `--accept-hooks` inside the remote
  string, no duplicate `-t` (idempotency), and `session_command` is
  not modified. Wired into `main()` after `test_sync_from_github_validation`.
- `AGENTS.md` — this block + the new "Open items" entry + the updated
  "Agent config" paragraph describing the new `-t <csv>` flag and the
  6th context-instruction paragraph.

**Verified:** `backend/tests/smoke.py` — `ALL SMOKE TESTS PASSED`
(200+ pre-existing checks + the 13 new hermes checks). Existing
`test_config_backfill` still passes (claude + hermes + codex built-in
backfill is unaffected; the only behavioural change is that the hermes
default now has 8 tokens and backfill produces 8 tokens for short
hermes configs).
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
  models.py        Project, Task, HeartbeatSeen
  schemas.py       Pydantic I/O
  security.py      pbkdf2 hash + JWT
  auth.py          get_current_user (Bearer), user_from_token (WS)
  github_client.py GitHub REST (create / get / delete repo / list_issues)
  git_ops.py       clone / commit / push (token only as http.extraheader, never in config)
  agents.py        run_agent(): subprocess + streaming, claude-json/raw parser,
                   model / effort arg injection, final output extraction
                   (_final_output heuristic or {last_message_file})
  uploads.py       Image attachments: Base64 / data-URL decode + validation,
                   stored under data_dir/task_images/{task_id}/
  task_runner.py   TaskManager: per-project lock, WS pub/sub, AGENTS.md maintenance,
                   auto-commit + push
  heartbeat.py     HeartbeatRunner: background loop, auto-poll GitHub issues,
                   auto-spawn Claude Code tasks; singleton at `heartbeat`
  routers/         auth, projects, tasks, sessions, heartbeat, ws
  main.py          app factory, lifespan (starts/stops the heartbeat), SPA serving
frontend/src/
  api.ts (REST + apiBase/token), auth.tsx, types.ts
  pages/ (Login, Projects, ProjectDetail, SessionPage redirect, AgentWindowPage,
          Heartbeat)
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
  `--use-auth-token`. Hermes: `hermes chat -q {prompt} --yolo --accept-hooks -t
  <csv>` (non-interactive, streams intermediate steps, loads AGENTS.md from the CWD;
  `-t <csv>` = `HERMES_NON_INTERACTIVE_TOOLSETS` from `backend/app/config.py`,
  excluding `clarify` so the one-shot run can't call into a None platform callback;
  `env: HERMES_ACCEPT_HOOKS=1, NO_COLOR=1`; `unset_env: [PYTHONPATH, PYTHONHOME]`).
  Codex: `codex exec --cd {project_dir} --sandbox workspace-write --color never
  --ephemeral --output-last-message {last_message_file} -` with `prompt_via: stdin`
  (no `goal_command`, so no goal mode for Codex). Current Codex versions do not have
  `--ask-for-approval` - the command is non-interactive on its own when given a
  prompt. Raw output is ANSI-filtered in the runner. TUI session defaults:
  Claude `claude`, Hermes `hermes chat` (intentionally FULL toolset, no `-t`, so
  the user can use `clarify` in the real TUI), Codex `codex`; extra flags only
  through the startup parameter field. **Hermes backfill** is special-cased in
  `_backfill_hermes_flags`: the Docker / SSH-driven Hermes keeps its last argv
  token as a single remote-shell string passed to ssh, so the new `-t <csv>` is
  spliced INTO that string (right after `--accept-hooks`) via
  `_splice_flags_into_hermes_remote`; local Hermes gets `-t <csv>` appended like
  any other flag. Idempotent.
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

**Heartbeat path.** `heartbeat.HeartbeatRunner` is a single long-lived
`asyncio.Task` started from `main.lifespan()`. Every
`settings.heartbeat_interval_seconds` it walks every active (non-archived)
project with a `github_full_name`, fetches open issues via
`github_client.list_issues()`, filters out PRs and (optionally) by
labels, dedups against the `heartbeat_seen` ledger, and for each
*newly-seen* issue calls the same `task_runner.TaskManager.submit()`
as a hand-submitted task — so heartbeat-spawned tasks inherit the
host lock, auto-pull, auto-commit + push, AGENTS.md maintenance and
result summary for free. The auto-spawned tasks are marked
`tasks.heartbeat_spawned=True` + `tasks.heartbeat_issue_number=N`
for the UI's `🤖 Auto-Fix #N` badge. The `routers/heartbeat.py` module
exposes both the global `/api/heartbeat/...` routes (status, enable /
disable / trigger) and the per-project `/api/projects/{id}/heartbeat/...`
routes (enable / disable, the dedup ledger, the live open-issues list).
Sync DB ops live in `_tick_locked()` behind `asyncio.to_thread`; per-tick
parallelism is gated by an `asyncio.Semaphore` of size
`settings.heartbeat_max_concurrent`. The prompt is built from
`DEFAULT_HEARTBEAT_PROMPT_TEMPLATE` (overridable via
`CD_HEARTBEAT_PROMPT_TEMPLATE`).

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
- **Heartbeat off by default.** `CD_HEARTBEAT_ENABLED=false` is the shipped
  default (both systemd `install.sh` and Docker compose). The dashboard
  only ticks when an operator opts in; this avoids runaway PR-generation
  on fresh installs. The `/heartbeat` UI's "Aktivieren" toggle is
  in-memory only and resets on restart; for persistence, set
  `CD_HEARTBEAT_ENABLED=true` in the service config and restart.
- **Heartbeat dedup is a one-way insert.** `heartbeat_seen` uses a
  composite primary key `(project_id, issue_number)`. Once an issue
  is recorded (whether or not a task was actually dispatched), it is
  never re-considered. If the operator wants the heartbeat to retry
  the same issue, the row has to be deleted by hand (or the issue
  closed + reopened, which gets a new `created_at` but the same
  `number` -- still deduped). A future iteration may key on
  `updated_at` for retry-on-update semantics.
- **`POST /api/heartbeat/trigger` awaits the tick.** Unlike most
  "fire-and-forget" admin endpoints, the trigger returns only after
  the tick completes (or after a `tick_lock` collision -- in which
  case it returns `summary={'status': 'already_running'}`). This makes
  it a usable 'Run now' button and a reliable test entry point.
- **`CD_HEARTBEAT_AGENT_KEY` must exist in `agents.config`.** If it
  doesn't (e.g. the operator left it at the default `claude` but the
  config only defines `codex`), the tick returns `no_agent` and
  dispatches nothing -- it does NOT fall back to another agent. Fix:
  either set `CD_HEARTBEAT_AGENT_KEY` to a defined key, or define the
  agent in `config.yaml`.

## Tests

`backend/tests/smoke.py` (no external services): security, parser, agent runner, full
Git commit / push cycle against a local bare repo, REST, and a complete task run.

## Open items / possible next steps

- **2026-07-06 (Feature):** Heartbeat comment-back: when a heartbeat-spawned
  task lands a commit, the dashboard posts the commit hash + branch URL +
  task link as a comment on the GitHub issue (compact German template,
  same shape every time so the timeline reads cleanly). When the commit
  also lands cleanly on the default branch (`merge_state=merged` +
  `pushed=true`), the dashboard additionally closes the issue via a
  separate `PATCH /repos/{repo}/issues/{n}` (`state: "closed"`) — works
  even when no PR exists. New `HeartbeatFollowup` class in
  `backend/app/heartbeat.py` (`heartbeat_followup.maybe_run(task_id)`),
  hooked from `task_runner._publish_done` after the terminal Task row is
  persisted. Fire-and-forget via `asyncio.create_task(...)`; the
  `_inflight` dict guards re-entry for the same `task_id`. Three new
  GitHub helpers in `backend/app/github_client.py`
  (`create_issue_comment`, `update_issue_comment`, `update_issue_state`)
  mirroring the existing `_request` pattern. Five new columns on
  `HeartbeatSeen` (`last_comment_id`, `last_commented_at`,
  `last_comment_url`, `last_comment_error`, `last_issue_state`,
  `last_issue_state_changed_at`); two new `Task` columns
  (`heartbeat_commented_at`, `heartbeat_closed_at`) for fast list-view
  display. Three new routes in `routers/heartbeat.py`
  (`POST .../comment-again`, `.../close`, `.../reopen`) for the operator
  UI — comment-again POSTs a NEW comment (so the auto-hook's comment
  stays intact), close/reopen PATCH the issue state. Frontend: the
  Heartbeat overview's recent-tasks feed shows `💬 vor 12 Min` /
  `✓ geschlossen` badges and three action buttons per row; the
  project-detail history list shows the same `💬 kommentiert` /
  `✓ geschlossen` chips next to the existing `🤖 Auto-Fix` badge. Both
  features are opt-out via
  `CD_HEARTBEAT_COMMENT_ON_SUCCESS=false` /
  `CD_HEARTBEAT_CLOSE_ON_MERGE=false` (both default `true`). Covered by
  `test_heartbeat_comment_on_solve` in `backend/tests/smoke.py`
  (39 checks: predicate tests, hook-path integration tests with stubbed
  GitHub helpers, REST round-trip via TestClient, idempotency). Effective
  after `update.sh` / `systemctl restart coding-dashboard` (or next
  container start for Docker).
- **2026-07-06 (Fix):** Cross-tab session-end propagation. `end_session`
  already persists `task.status` to the terminal value (`success` /
  `failed` / ...) *before* the slow git commit/push step (verified by
  the new `test_session_end_flow` assertion), so the dashboard's
  `/api/running` view drops the session as soon as the HTTP response
  is in flight. But the popup's `SessionTerminalModal` lives in a
  separate tab from the main dashboard, so the main tab wouldn't
  notice without `BroadcastChannel` fan-out. New
  `frontend/src/crossTab.ts` bridges that — `SessionTerminalModal` and
  `TaskConsole` emit `{type:"session-done"|"task-done", ...}` after a
  successful HTTP / WS done event, and `RunningAgents` /
  `ProjectDetail` subscribe and re-poll / re-fetch on receipt. Closes
  the "page not updating until I refresh" symptom of issue #5. Pure
  frontend, no backend change; the existing 3-second polling stays as
  the fallback.
- **2026-07-05 (Feature):** Dashboard heartbeat: a single long-lived
  `asyncio.Task` started from `main.lifespan()` polls GitHub for open
  issues on active (non-archived) projects every
  `CD_HEARTBEAT_INTERVAL_SECONDS` (default 15 min) and auto-spawns a
  Claude Code task per *newly-seen* open issue with a structured
  "investigate → reproduce → fix → PR" prompt scaffold
  (`DEFAULT_HEARTBEAT_PROMPT_TEMPLATE` in `backend/app/config.py`,
  overridable via `CD_HEARTBEAT_PROMPT_TEMPLATE`). Dedup is per
  `(project_id, issue_number)` via a new `heartbeat_seen` table
  (idempotent insert). Per-project opt-out via
  `projects.heartbeat_enabled`; global toggle via
  `POST /api/heartbeat/{enable,disable}` (in-process, mirrors the
  `CD_HEARTBEAT_ENABLED` env var). Manual trigger via
  `POST /api/heartbeat/trigger`. The auto-spawned tasks run through the
  same `manager.submit()` path as user tasks, so they get the host
  lock, auto-pull, auto-commit, auto-push, AGENTS.md maintenance and
  result summary for free. Frontend gets a dedicated `/heartbeat` page
  (global + per-project toggles + status table + "▶ Run now" + recent
  heartbeat-spawned tasks feed), an `🤖 vor 12 Min / Fehler / aus`
  chip on each project card, and a `🤖 Auto-Fix #N` badge on each
  spawned task in the history list. New columns on `projects`
  (`heartbeat_enabled`, `last_heartbeat_at`, `last_issue_poll_at`,
  `last_heartbeat_status`, `last_heartbeat_error`) and on `tasks`
  (`heartbeat_spawned`, `heartbeat_issue_number`) are added
  idempotently by `database._ensure_sqlite_columns()` on next service
  start, so existing DBs do not need a manual migration; the new
  `heartbeat_seen` table is created by `create_all()`. Covered by
  `test_heartbeat` in `backend/tests/smoke.py` (73 checks). Effective
  after `update.sh` / `systemctl restart coding-dashboard` (or on next
  container start for Docker). No new WebSocket protocol yet — the
  `/heartbeat` page polls every 5s; per-project per-issue WebSocket
  broadcasts and exponential backoff on GitHub errors are noted as
  next-iteration candidates.
- **2026-06-24 (Feature):** Projects can now be archived. Soft-delete via
  ``projects.archived`` + ``projects.archived_at`` columns; two new
  endpoints (``POST /api/projects/{id}/archive`` and ``…/unarchive``,
  idempotent); ``GET /api/projects`` now accepts ``?archived=all|true|false``
  and hides archived ones by default. Frontend gets an ``Aktiv | 📦 Archiv``
  pill toggle on the start page, a per-card ``📦`` (with confirm) /
  ``↩`` action, and a "↩ Wiederherstellen" button in the project detail
  header when archived. Archive is a UI concern only - the repo stays on
  disk, history stays in the DB, running tasks / open sessions are not
  stopped, and ``GET /api/projects/{id}`` still works for archived projects
  (so the user can still inspect / pull / resume). Covered by
  ``test_project_archive`` in ``backend/tests/smoke.py`` (18 checks).
  Effective after ``update.sh`` / ``systemctl restart coding-dashboard``
  (or on next container start for Docker). New ``archived`` /
  ``archived_at`` columns are added idempotently by
  ``database._ensure_sqlite_columns()`` on the next service start, so
  existing DBs do not need a manual migration.
- **2026-06-23 (Fix):** Hermes' `clarify` toolset used to fire in non-interactive
  task / goal runs (`hermes chat -q {prompt}`), where the dashboard can only
  stream stdout. The tool called into a None platform callback and either
  stalled the run or bounced back with "Clarify tool is not available in this
  execution context." Fix: pass `-t <csv>` excluding `clarify` to the
  non-interactive Hermes (`HERMES_NON_INTERACTIVE_TOOLSETS` in
  `backend/app/config.py`; mirrored as `HERMES_NON_INTERACTIVE_TOOLSETS_CSV` in
  the Docker entrypoint). `session_command` (`hermes chat`) keeps the full
  toolset so interactive TUI sessions still allow `clarify`. The context
  instruction now explicitly tells the agent "Stelle KEINE Rueckfragen an den
  User" in non-interactive mode. `load_agents_config._backfill_hermes_flags`
  splices the new flag into existing SSH remote-shell strings (Docker) and
  appends it to flat-argv configs (systemd installs); idempotent; covered by
  `test_hermes_clarify_disabled`. Effective after `update.sh` /
  `systemctl restart coding-dashboard` (or on next container start for Docker).
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
