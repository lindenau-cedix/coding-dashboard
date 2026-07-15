# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Self-hosted dashboard for delegating coding tasks per project to Claude Code,
Hermes, or Codex: create/import a repo → give an agent a task → watch live
output → auto-commit & push → keep history. Web + Android.

For the user-facing deploy / install / Android-build flow, read
[`README.md`](./README.md). Change history for this repo lives in
`git log -p -- AGENTS.md`; this file is the durable, present-tense reference
and intentionally does **not** keep a per-run journal.

## Quick commands

Backend (Python 3.10-3.12, FastAPI) — from `backend/`:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Frontend (React 18 + Vite + TS + Tailwind v4) — from `frontend/`:

```bash
npm install
npm run dev          # http://localhost:5173, proxies /api -> 127.0.0.1:8000
npm run build
npm run typecheck    # tsc --noEmit — run before considering frontend changes done
```

Tests — one self-contained script, **no pytest, no test runner**:

```bash
cd backend && .venv/bin/python tests/smoke.py
```

Each check prints `[PASS]/[FAIL]`. To focus on one area while iterating,
temporarily comment out calls in `smoke.py:main()` — there is no per-test
selector. **No configured linter** (no ruff, no eslint).

Deploy / admin (server) — see `README.md` for the full flow. Quick reference:
`sudo ./deploy/install.sh`, `sudo ./deploy/update.sh`,
`./deploy/build-android.sh https://host`. Runtime config lives at
`/etc/coding-dashboard/config.yaml`; data (SQLite + cloned repos) under
`/var/lib/coding-dashboard/`.

## Repository layout

```
backend/app/
  config.py             Settings (env CD_*) + agent YAML loader + context_instruction
  config_bootstrap.py   First-boot config.yaml generator (extracted from
                        entrypoint.sh so it's unit-testable; emits the
                        hermes/hermes-host/claude/claude-host siblings with
                        the shared-SSH-wiring resolver)
  database.py           Engine / session (SQLite), init_db, session_scope,
                        _SQLITE_COLUMN_ADDITIONS for idempotent ALTERs
  models.py             Project, Task, HeartbeatSeen, EnvProfile (ORM)
  schemas.py            Pydantic I/O
  security.py           pbkdf2 hash + JWT
  auth.py               get_current_user (Bearer), user_from_token (WS)
  github_client.py      GitHub REST (repos, issues, comments, state)
  git_ops.py            clone / commit / push (token as http.extraheader only)
  agents.py             run_agent(): subprocess + streaming, claude-json /
                        raw / codex parser, model + effort argv injection
  env_crypto.py         Fernet wrapper bound to CD_SECRET_KEY (encrypts
                        env-profile tokens; write-only outside the bundled
                        default secret)
  uploads.py            Image attachments: Base64 / data-URL decode + validate
  task_runner.py        TaskManager: per-project lock, WS pub/sub,
                        AGENTS.md maintenance, auto-commit + push.
                        SessionManager for PTY TUI sessions. <base>-host
                        sibling-swap shim for runner="host".
  heartbeat.py          HeartbeatRunner singleton: background loop, auto-poll
                        GitHub issues, auto-spawn Claude Code tasks; runtime
                        agent-key + env-profile overrides
  host_lock.py          One <kind>-<id>.lock file per active run (visibility)
  host_staging.py       Shared staging dir for agents running on the host
  session_dirs.py       Resolves the recorded CWD for `--resume` so an agent
                        finds its previous session even when parallel runs
                        use isolated worktrees
  routers/              auth, projects, tasks, sessions, heartbeat, env_profiles, ws
  main.py               app factory, lifespan (starts/stops heartbeat +
                        auto-init DB), SPA serving, CORS wiring
  cli.py                python -m app.cli (hash-password; rename-github-owner)

frontend/src/
  api.ts                REST client + apiBase/token plumbing
  auth.tsx              Auth context
  types.ts              Shared Task / Agent / Project types
  pages/                Login, Projects, ProjectDetail, SessionPage,
                        AgentWindowPage, Heartbeat, EnvProfiles
  components/           TaskConsole, SessionTerminalModal, WindowManager,
                        TaskImages, FileBrowser, ui, ...

deploy/
  install.sh            systemd install
  update.sh             systemd update
  build-android.sh      Capacitor APK build
  Dockerfile            two-stage (frontend build → backend runtime)
  entrypoint.sh         First-boot config gen + uvicorn exec
  coding-dashboard.docker.env(.example)
                        env_file template; secrets live in the .env copy
```

## Core flows

**Task** — `POST /api/projects/{id}/tasks` (`{agent, prompt, mode, runner,
env_profile_key, model?, effort?, images?}`) → `TaskManager.submit` →
asyncio task. Prompt = user prompt + `context_instruction` (AGENTS.md
maintenance). Output streams over WS `/api/ws/tasks/{id}` with replay from
the in-memory buffer / DB row for late or repeated joins. After run: result
→ DB row → auto-commit + push. Commit hash, push status, merge state all
land back on the row.

**Model / effort per task** — agents with `model_choices` / `effort_choices`
(`claude`, `codex`) get dropdowns in the UI. Validation 400 on invalid.
CLI argv injection via `model_args` / `effort_args`. In addition to argv,
the built-in `claude` / `codex` agents get the value written into their
own dotfile (`~/.claude/settings.json` for effort; `~/.codex/config.toml`
for model + `model_reasoning_effort`) — **keys off `spec.key`, so
renaming those agents silently drops the dotfile write.** Codex uses
`-c model_reasoning_effort={effort}` rather than a `--effort` CLI flag
(Codex has no such flag); the SSH `codex-host` sibling inherits the
same `model_args` / `effort_args` from the base spec via the deep-copy
in `config_bootstrap.py`, so `_build_command` substitutes the user's
selections into the appended `-c model_reasoning_effort=…` argv after
the SSH remote-shell string.

**Image attachments** — `TaskCreate.images = [{name, data}]` (Base64 or
data URLs; ≤6 images, ≤8 MB each, png/jpg/jpeg/gif/webp). Stored outside
the repo at `data_dir/task_images/{task_id}/`, served via
`GET /api/tasks/{id}/images/{name}` (auth required).

**Goal mode (`mode="goal"`)** — same path, `goal_command` substitutes the
prompt; agent works until the goal signal arrives. Whole run counts as one
task; one commit + push at the end.

**Session mode (`mode="session"`)** — real PTY (`os.openpty` + `os.fork`)
runs the agent's `session_command` as a TUI; raw bytes stream both ways
over WS `/api/ws/sessions/{id}`. Bracketed paste (`?2004h` +
`\x1b[200~` / `\x1b[201~`). `keyToBytes()` returns `null` for Ctrl+V /
Ctrl+Shift+V on purpose — the browser default paste event fires, which is
what TUI image-paste shortcuts need.

**Env profiles** — `env_profiles` table + `/api/env-profiles` CRUD router.
Stores `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (Fernet-encrypted via
`CD_SECRET_KEY`). `Task.env_profile_key` (and the session / heartbeat-spawn
equivalents) triggers an overlay at run time: the runner merges the two
vars onto a `model_copy` of the cached spec, defensively writing
`ANTHROPIC_API_KEY=""` so a host-shell inherited Anthropic key cannot leak
through. The runner resolves the env profile key in this order:
per-project override → runtime-global override (POST /api/heartbeat/env-
profile) → env-var default → empty.

**Per-task host runner** — `Task.runner="host"` resolves to the
`<agent>-host` sibling AgentSpec. The sibling is auto-created by the
Docker entrypoint when `CD_<AGENT>_SSH_USER` is set (`hermes`, `claude`,
`codex` — see "Shared SSH wiring" below), or hand-written in
`config.yaml` for systemd (`deploy/install.sh` ships commented templates
for `claude-host`, `hermes-host`, `codex-host`). Sibling has
`host_staging=True`, reusing the existing `host_staging.*` copy/merge
plumbing. The UI exposes host support as a **separate Runner dropdown**
(`Container` / `Host via SSH`) next to the Agent dropdown — the Runner
control is hidden entirely for agents whose selected base has no
enabled host sibling.

**Heartbeat** — `heartbeat.HeartbeatRunner` is one long-lived
`asyncio.Task` started from `main.lifespan()`. Every
`settings.heartbeat_interval_seconds` (default 900s) walks every active
project with a `github_full_name`, dedups against `heartbeat_seen`, and
dispatches one task per *newly-seen* issue through `manager.submit()` (so
it goes through every backend pipeline as a normal task). Per-tick
parallelism gated by an `asyncio.Semaphore`. Default agent is
`CD_HEARTBEAT_AGENT_KEY=claude` (container). The UI can override the
agent key + env profile at runtime (in-memory until restart) via
`POST /api/heartbeat/agent-key` and `POST /api/heartbeat/env-profile`.

**WS reconnect / replay** — WS pub/sub replays the task state from the DB
when the live channel is gone, so reconnects don't lose output.

## Conventions

- Secrets only via env (`CD_*`). Never persist GitHub tokens.
- **No Alembic.** `create_all` runs on startup. Additive columns for
  existing SQLite DBs go into `database._SQLITE_COLUMN_ADDITIONS` as
  idempotent `ALTER TABLE ADD COLUMN` (runs after `create_all` in
  `init_db`).
- Backend endpoints that use `asyncio.create_task` must be `async def`.
- **`git_ops` calls are blocking** — call them from the event loop via
  `asyncio.to_thread`.
- **Routers with WebSocket endpoints must NOT have HTTPBearer / security
  deps at router level.** FastAPI tries to resolve them for
  `@router.websocket` routes, but WS handshakes have no real `Request` →
  `TypeError: HTTPBearer.__call__() missing 1 required positional
  argument: 'request'` → WS closes immediately. HTTP routes in the same
  router declare `Depends(get_current_user)` explicitly in their
  signature; WS auth uses `user_from_token(token)` query-param.
- **`CD_CORS_ORIGINS=*` is reflected, not literal** — `main.py` sets
  `allow_origin_regex='.*'` with `allow_credentials=True` (a literal `*` is
  invalid with credentials).
- Agents run autonomously (`--dangerously-skip-permissions` / `--yolo` /
  Codex non-interactive) and push without confirmation. Intended for
  private repos with a dedicated token; the service runs as a non-root
  user.
- **Hermes is installed into the image by default** (Dockerfile
  `HERMES_INSTALL_CMD` = the upstream one-liner). The install lands
  at `/home/app/.local/bin/hermes` + `/home/app/.hermes/...`, but the
  `cd-home` Docker volume is mounted over `/home/app` and shadows both
  paths. The Dockerfile therefore also copies the install into
  `/usr/local/share/hermes/.` (a non-`/home/app` path that's never
  overlaid), and the entrypoint seeds it into `$HOME` on first boot
  (marker: `~/.hermes/.seeded_from_image`) so `which hermes` works
  inside the running container. On subsequent boots the entrypoint
  regenerates `config.yaml` if `hermes` is on PATH but the YAML's
  `hermes:` entry still shows `enabled: false` — operators don't have
  to delete the file manually after an upgrade. `CD_HERMES_SSH_USER`
  is the opt-in path to ALSO register a `hermes-host` sibling that
  runs the host's Hermes over SSH — useful when the operator wants
  exactly one Hermes process tree on the host for paired channels
  (WhatsApp, Telegram) and cronjobs that should keep firing when the
  dashboard container is down. With `CD_HERMES_SSH_USER` set, both
  `hermes` (in-image) and `hermes-host` (host-SSH) are selectable
  through the per-task "Runner: host" dropdown. To opt out of the
  in-image install entirely: `HERMES_INSTALL_CMD="" docker compose
  build`. The Dockerfile default is the **source of truth** for
  `HERMES_INSTALL_CMD`; the compose file does NOT override it (an
  earlier `docker-compose.yml` line used to forward the shell env
  defaulting to empty, which silently beat the Dockerfile default and
  dropped every install — don't reintroduce that).
- **Shared Hermes/Claude/Codex SSH wiring (Docker entrypoint).** Setting ANY
  one of `CD_{HERMES,CLAUDE,CODEX}_SSH_USER` lights up ALL THREE
  `<agent>-host` siblings at the resolved user/host/port (each sibling
  prefers its own env, falls back to the next agent's in the
  `(hermes, claude, codex)` resolver order). Setting TWO/THREE only
  matters when they point at different hosts. Setting NONE disables all
  three. Container `hermes` / `claude` / `codex` stay as their own
  entries — only the SSH form moves into the sibling, mirroring the
  `claude`/`claude-host` pair. Operator-facing wording lives in
  `deploy/docker/coding-dashboard.docker.env.example`. The single pair
  of `id_hermes` / `id_claude` / `id_codex` keys per agent is honoured
  the same way as user/host/port (next bullet).
- **SSH private-key paths follow the same shared-wiring rule as USER/HOST/PORT**
  in `backend/app/config_bootstrap.py` + `deploy/docker/entrypoint.sh`. Each
  agent defaults to its own key path (`/home/app/.ssh/id_hermes` /
  `id_claude` / `id_codex`). The resolver: honours an explicit
  `CD_{HERMES,CLAUDE,CODEX}_SSH_KEY` env override as a pin (no
  fallback); otherwise inherits the next agent's key path when the
  sibling inherited that agent's SSH user (a Hermes-only setup →
  `claude-host` + `codex-host` use `id_hermes`); finally, if the
  resolved key file is absent on disk but any other agent's key exists,
  falls back to the existing key — unless pinned. The Python generator
  emits the key into `<agent>-host.command` / `session_command`; the
  entrypoint prints the **effective** key (after resolution) in the
  boot log + `Verify connectivity` snippet. The frontend's `/api/agents`
  returns the generator output — never the raw default — so what the
  boot log says matches what runs.
- **`<base>-host` is the universal sibling-swap pattern.** Adding a new
  on-host agent = register a sibling under `<base>-host` with
  `host_staging=True`; the per-task Runner dropdown, the heartbeat
  `available_agent_keys` selector, the SessionManager / TaskManager
  `<base>-host` swap shims, and the front-end `host_agent_key` resolver
  all pick it up with no code change. The Codex variant adds two
  deviations: the SSH `command` must drop `--output-last-message
  {last_message_file}` (the host's codex process can't reach the
  container's tempfile) and inject effort via
  `-c model_reasoning_effort={effort}` instead of `--effort`. Both are
  handled in `config_bootstrap.py`'s codex branch — mirror them when
  adding a fourth on-host agent.
- **Generator emits the base agent BEFORE its `-host` sibling** in
  `backend/app/config_bootstrap.py`. This ordering is load-bearing:
  `/api/agents` preserves dict order, and the frontend defaults to the
  first entry whose `enabled` is true. A `-host` sibling emitted first would
  silently become the default (SSH runner on the first submit even when
  the UI reads "Container"). The invariant is enforced by
  `test_claude_host_reuses_hermes_ssh` in `backend/tests/smoke.py`.

## Gotchas

- **Hermes in the image is build-time only.** A flip of
  `HERMES_INSTALL_CMD` doesn't take effect until `docker compose build`.
  The currently running container keeps its old image; the volume-held
  `config.yaml` may show `hermes: enabled=false` until the image is
  rebuilt and the container is restarted (and the entrypoint re-runs,
  regenerating the config from the now-present binary on PATH).
- **Heartbeat off by default.** `CD_HEARTBEAT_ENABLED=false` is the
  shipped default (systemd `install.sh` and Docker compose). To turn it
  on, either set the env var or use the runtime toggle on the
  `/heartbeat` page.
- **Heartbeat dedup is one-way insert.** `heartbeat_seen` is a composite
  primary key `(project_id, issue_number)`. Once an issue is recorded, it
  is never re-considered. To retry: delete the row by hand, or close +
  reopen the issue (new `number`).
- **`POST /api/heartbeat/trigger` awaits the tick.** Unlike most
  fire-and-forget admin endpoints, the trigger returns only after the
  tick completes (or after a `tick_lock` collision → `status=
  "already_running"`). This makes it the "Run now" button AND a reliable
  test entry point.
- **`CD_HEARTBEAT_AGENT_KEY` must exist in `agents.config`.** Otherwise
  the tick returns `no_agent` and dispatches nothing — does NOT fall
  back. (The runtime override on the `/heartbeat` page is validated
  the same way: 400 on unknown / disabled key.)
- **Env-profile auth tokens are write-only.** The GET response never
  echoes plaintext; only `anthropic_auth_token_set: bool` + an
  anonymised hint like `"sk-…12"`. To rotate, PATCH with a new token.
  To clear, PATCH with `""`. `CD_SECRET_KEY` rotation invalidates all
  stored tokens; setting it back to the bundled default disables token
  writes (CRUD returns 503).
- **`codex-host` cannot use `--output-last-message`.** The container-side
  `codex` spec writes its final answer to a tempfile in
  `tempfile.mkstemp(prefix="cd-last-msg-…")`; the host SSH process can't
  reach that path. `config_bootstrap.py` strips
  `--output-last-message {last_message_file}` from the `codex-host`
  command so `run_agent` skips temp-file creation entirely and the
  summary falls back to `_CodexParser.summary()`. If a fourth on-host
  agent ever needs the same opt, mirror this in its sibling block.
- **When ANY profile field is set, `ANTHROPIC_API_KEY=""` is stamped.**
  Leaving `ANTHROPIC_API_KEY` unset lets the host's shell export a
  leaked upstream token that would silently hit the wrong endpoint.
- **First-boot `config.yaml` is written once, never overwritten — except
  when the Hermes seed lands on PATH, or when a shared-wiring
  SSH-driven sibling is missing.** The Docker entrypoint only writes
  the file when `/etc/coding-dashboard/config.yaml` is absent; upgrading
  the image does NOT re-merge new built-in siblings into the existing
  volume-held file. The exceptions are:
  1. The Hermes-in-image first-boot seed (see the convention above): on
     every container start, if `hermes` is on PATH but the existing YAML's
     `hermes:` entry is `enabled: false` (left over from a pre-seed boot),
     the entrypoint regenerates the file so the UI flips the option
     without operator intervention.
  2. The shared-SSH-wiring backfill (see "Shared Hermes/Claude/Codex SSH
     wiring" above): on every container start, if any
     `CD_{HERMES,CLAUDE,CODEX}_SSH_USER` is set AND the YAML is missing
     one of the three `<base>-host` siblings the generator would emit
     (e.g. an upgrade that adds `codex-host` to a YAML written before it
     existed), the entrypoint regenerates the file so `/api/agents` ships
     the full expected sibling set. The Python loader
     (`app.config.load_agents_config`) also runs an in-memory backfill
     of the same kind, so a running container picks up the missing
     sibling on the next backend start even if the operator has not
     restarted the full image (e.g. only a backend reload). The
     loader-side backfill is gated on `_is_legacy_builtin_config_with_siblings`
     so custom (non-builtin) agents in the YAML are never silently
     mutated; operator-set `enabled: false` on a present `-host` key is
     preserved because the backfill only ADDS missing keys.
  To pick up any other new built-ins on an upgrade, delete the file (or
  the volume) and restart.
- **`shutil.which(cmd, path=…)` in `config_bootstrap.py` is
  load-bearing.** Calling it without `path=` falls back to
  `os.defpath` (`/bin:/usr/bin`), ignoring any caller-supplied PATH —
  which is why the generator threads the resolved PATH through
  explicitly. Don't remove that argument.
- **The Docker entrypoint runs with `set -euo pipefail`** (line 16 of
  `deploy/docker/entrypoint.sh`). Any `${CD_*}` env var referenced in the
  shell script MUST be default-initialised first (`FOO="${FOO:-}"`) or an
  unset value aborts the boot with `unbound variable`. The same rule
  applies to every shell script under `deploy/*.sh` (`install.sh`,
  `update.sh`, `uninstall.sh`, `build-android.sh`). Adding `CD_HERMES_SSH_KEY`
  + `CD_CLAUDE_SSH_KEY` without that initialisation crashed the container
  in a `Recreate` loop until both vars got explicit `:-` defaults. When you
  add a new `set -eu*` script or a new optional env var to an existing
  one, grep for every bare `$CD_FOO` reference and add `:-` defaults.
