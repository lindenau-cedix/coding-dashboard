# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Orientation note.** The original orientation doc was a long running
> journal of every change ("AGENTS.md"). It was deprecated — every prior
> run is still available via `git log -p -- AGENTS.md` if you need
> archaeology. This file now distills only the durable conventions and
> the latest run so a fresh session is productive immediately.

## Purpose

Self-hosted dashboard for delegating coding tasks per project to Claude Code,
Hermes, or Codex: create/import a repo → give an agent a task → watch live
output → auto-commit & push → keep history. Web + Android.

## Letzter Durchlauf

### 2026-07-14 — claude (Shared Hermes/Claude SSH wiring + `hermes-host` sibling)

**Aufgabe:** Zwei Dockerfile-/Entrypoint-Verbesserungen — (1) wenn der
Operator heute `CD_HERMES_SSH_USER` setzt, läuft `claude` weiter im
Container (erst `CD_CLAUDE_SSH_USER` aktiviert die Host-Variante). Beide
Agents zeigen in 99% der Setups auf denselben Host, also soll ab jetzt
**eine** SSH-Env-Var-Paarung beide `-host`-Siblings aktivieren. Nur wenn
beide gesetzt sind, sollen sie unabhängig sein; keiner → keiner.
(2) Bisher mutierte der Entrypoint die `hermes` AgentSpec zu einer
SSH-Variante (Container-Hermes nicht mehr erreichbar). Neu: Container-`hermes`
bleibt, `hermes-host` wird als Sibling registriert — exakt wie das
bestehende `claude`/`claude-host`-Paar. Kein literaler `hermes-container`
Sibling nötig (würde drei Hermes-Zeilen für eine CLI-Familie ergeben).

**Was geändert wurde:**
- `backend/app/config_bootstrap.py` (NEU) — der komplette first-boot
  Generator aus dem Entrypoint als importierbare Funktion
  `generate_initial_agents_config(env=None, *, home=None)`. Testbar
  in-process ohne Bash-Spawn. Resolver-Semantik: jede `-host`-Sibling
  bevorzugt die eigenen `CD_<AGENT>_SSH_{USER,HOST,PORT}`; fällt auf die
  des anderen zurück, wenn die eigenen leer sind. Leerer User → kein
  Sibling. Fix: `shutil.which(cmd, path=effective_path)` (ohne expliziten
  `path=` fiel `which` auf `os.defpath` zurück und ignorierte den
  per-Test gesetzten PATH — ein latenter Bug, der sowohl den Eintrag
  selbst als auch realistische Deployment-Szenarien mit reduziertem
  PATH erwischt hätte).
- `deploy/docker/entrypoint.sh` — Python-Heredoc (vorher ~190 Zeilen
  Inline-Logik) schrumpft auf 6 Zeilen, die `config_bootstrap` aufrufen.
  Bash-Prelude: `mkdir -p "$HERMES_STAGING_DIR"` und das
  `~/.ssh_known_hosts`-Touch werden jetzt von
  `[[ -n "$HERMES_SSH_USER" || -n "$CLAUDE_SSH_USER" ]]` getrieben (nicht
  mehr nur vom Hermes-User); redundanter zweiter `mkdir` entfernt.
  Boot-Echo-Block löst die effektiven SSH-Werte genauso auf wie der
  Generator und zeigt sie dem Operator; wenn nur eine Seite gesetzt
  ist, wird das im Log explizit vermerkt.
- `deploy/docker/coding-dashboard.docker.env.example` — neuer
  `Host-over-SSH wiring (shared by Hermes AND Claude Code)`-Absatz
  erklärt die Prioritätsregel; bestehende Hermes/Claude-Blöcke
  verweisen darauf und sind entschlackt.
- `backend/tests/smoke.py` — drei neue Tests:
  `test_hermes_host_sibling_registers` (Sibling + Container-Eintrag
  koexistieren; ohne SSH beides deaktiviert/abwesend),
  `test_claude_host_reuses_hermes_ssh` (4-Fälle-Matrix: nur-Hermes,
  nur-Claude, beide unabhängig, keiner),
  `test_hermes_container_in_image_only` (Container-`hermes` aktiv
  gdw. `command -v hermes` erfolgreich).
- **Keine** Änderungen in `app/task_runner.py`,
  `app/routers/{tasks,sessions,heartbeat}.py`, `app/config.py`,
  `frontend/**` — die `<key>-host`-Sibling-Swap-Mechanik und die
  `available_agent_keys`-Ableitung greifen `hermes-host` automatisch
  auf (generische Lookups).

**Verhalten danach:**
- Operator setzt NUR `CD_HERMES_SSH_USER=foo` → `hermes-host` UND
  `claude-host` registriert, beide zeigen auf
  `foo@host.docker.internal:22`. Per-Task-Runner-Dropdown schaltet
  für beide Agents zwischen Container und Host.
- Operator setzt NUR `CD_CLAUDE_SSH_USER=bar` → beide Siblings
  registriert auf `bar@host.docker.internal:22`.
- Operator setzt BEIDE unabhängig → jeder Sibling nutzt seine eigenen
  Werte (wie bisher).
- Operator setzt KEINEN → kein `-host`-Sibling, Runner-Toggle
  versteckt für beide Agents, Container-CLIs laufen wie bisher.
- Container-`hermes` bleibt IMMER als eigener Eintrag stehen (heilt
  den bisherigen Verlust der Container-Variante in SSH-Mode). Per-Task
  kann zwischen `hermes` (Container, nur wenn `hermes` CLI installiert)
  und `hermes-host` (SSH) gewählt werden — exakt wie es bereits für
  `claude`/`claude-host` funktioniert.

**Verifikation:** `cd backend && .venv/bin/python tests/smoke.py` →
**528 PASS / 2 FAIL**. Alle 25 neuen Assertions PASS. Die 2
Failures (`cors preflight -> 200`, `cors reflects android origin`) sind
die pre-existing CORS-Tests auf `main` und unverändert.

### 2026-07-14 — claude (Sessions: Env-Profil + Runner; Heartbeat: Global Env-Profil + Agent-Auswahl)

**Aufgabe:** Zwei fehlende UI-Bedienelemente nachgerüstet — (1) auf der
Projekt-Detailseite war im Session-Modus weder Env-Profil noch
Runner sichtbar (war hartkodiert auf `mode !== "session"` gegated,
obwohl das Backend beides schon korrekt verdrahtet hatte); (2) auf
der Heartbeat-Seite gab es nur eine Lese-Anzeige des globalen
`CD_HEARTBEAT_ENV_PROFILE_KEY`-Wertes als Chip und keine Möglichkeit,
den Heartbeat zwischen `claude` und `claude-host` umzuschalten.

**Was geändert wurde:**
- `frontend/src/pages/ProjectDetail.tsx` — die `mode !== "session"`
  Gates um den Runner- und Env-Profil-Dropdown entfernt. `changeMode`
  setzt jetzt nicht mehr `runner` und `envProfileKey` zurück, wenn auf
  Session gewechselt wird. Das lokale `sessTask`-Literal in
  `startSession` führt `runner` + `env_profile_key` mit, damit der
  TypeScript-Build sauber bleibt.
- `backend/app/schemas.py` — `HeartbeatEnvProfileIn`,
  `HeartbeatAgentKeyIn` hinzugefügt; `HeartbeatStatus` um
  `available_agent_keys: list[str]` erweitert, damit die UI weiß,
  welche Agent-Keys aktuell umschaltbar sind.
- `backend/app/routers/heartbeat.py` — neue Endpoints
  `POST /api/heartbeat/env-profile` und
  `POST /api/heartbeat/agent-key`. Beide validieren serverseitig
  (404 bei unbekanntem Env-Profil-Key, 400 bei unbekanntem/deaktiviertem
  Agent), sind in-memory (resetten beim Backend-Neustart) und liefern
  den effective Wert im Response. `GET /api/heartbeat` zieht jetzt
  runtime-overrides vor die env-Var-Defaults und berechnet
  `available_agent_keys` aus `agents.agents` (env-Default + alle
  enabled `<key>-host`-Siblings).
- `backend/app/heartbeat.py` — `HeartbeatRunner` um
  `_agent_key_override` + `_env_profile_key_override` plus
  `set_agent_key` / `set_env_profile_key` Properties/Setters erweitert.
  `_tick` liest den Agent-Override, `_resolve_env_profile_key` zieht
  den Env-Override vor die env-Var. Reihenfolge jetzt:
  per-project override → runtime-global → env-var global → leer.
- `frontend/src/pages/Heartbeat.tsx` — neue Zeile unter dem Toggle-
  Bereich mit zwei Selects: "Agent" (alle `available_agent_keys`,
  `claude-host` bekommt einen 🖥 host-Suffix), "Default Env-Profil"
  (Profile aus `/api/env-profiles` + "Standard"-Eintrag der auf den
  env-var zurückfällt).
- `frontend/src/api.ts` — `setHeartbeatEnvProfile`,
  `setHeartbeatAgentKey` hinzugefügt.
- `frontend/src/types.ts` — `HeartbeatStatus.available_agent_keys`
  ergänzt, `env_profile_key` Doc-Kommentar präzisiert (mentiont jetzt
  explizit POST /api/heartbeat/env-profile + den in-memory Charme).
- `backend/tests/smoke.py` — drei neue Tests:
  `test_heartbeat_global_env_profile_endpoint` (404/200/Clear-Pfad),
  `test_heartbeat_agent_key_endpoint` (mit echtem Tick → spawned task
  trägt `agent='fake-host'`), `test_session_env_profile_persists`
  (round-trippt `env_profile_key` + `runner` durch POST /sessions und
  liest sie aus dem Task-Row).

**Verifikation:** `python tests/smoke.py` → alle Pre-existing PASS +
alle 21 neuen Assertions PASS. Die einzigen Failures bleiben die zwei
pre-existing CORS-Failures auf `main` (unverändert, unabhängig).

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
npm run dev          # http://localhost:5173, proxies /api (incl. /api/ws) -> 127.0.0.1:8000
npm run build        # -> frontend/dist (what nginx / the SPA fallback serves)
npm run typecheck    # tsc --noEmit (run this before considering frontend changes done)
```

Tests — single self-contained script, **no pytest / no test runner**:

```bash
cd backend && .venv/bin/python tests/smoke.py
```

Every check prints `[PASS]/[FAIL]`. To focus on one area while iterating,
temporarily comment out calls in `smoke.py:main()` — there is no
per-test selector. There is **no configured linter** (no ruff / eslint).

Deploy / admin (server, see README for full flow): `sudo ./deploy/install.sh`,
`sudo ./deploy/update.sh`, `./deploy/build-android.sh https://host`. Runtime
config lives at `/etc/coding-dashboard/config.yaml`; data (SQLite + cloned
repos) under `/var/lib/coding-dashboard/`.

## Repository layout

```
backend/app/
  config.py        Settings (env CD_*) + agent config (YAML) + context_instruction
  config_bootstrap.py First-boot config.yaml generator (extracted from entrypoint.sh,
                   unit-testable; emits `hermes`/`hermes-host`/`claude`/`claude-host`
                   siblings with shared SSH-wiring resolver)
  database.py      Engine / session (SQLite), init_db, session_scope
  models.py        Project, Task, HeartbeatSeen, EnvProfile
  schemas.py       Pydantic I/O
  security.py      pbkdf2 hash + JWT
  auth.py          get_current_user (Bearer), user_from_token (WS)
  github_client.py GitHub REST (create / get / delete repo / list_issues /
                   create_issue_comment / update_issue_state)
  git_ops.py       clone / commit / push (token only as http.extraheader, never in config)
  agents.py        run_agent(): subprocess + streaming, claude-json/raw parser,
                   model / effort arg injection, final output extraction
  env_crypto.py    Fernet wrapper bound to CD_SECRET_KEY (encrypts env-profile tokens)
  uploads.py       Image attachments: Base64 / data-URL decode + validation
  task_runner.py   TaskManager: per-project lock, WS pub/sub, AGENTS.md maintenance,
                   auto-commit + push; SessionManager for PTY TUI sessions;
                   <base>-host sibling swap shim for runner="host"
  heartbeat.py     HeartbeatRunner: background loop, auto-poll GitHub issues,
                   auto-spawn Claude Code tasks; singleton at `heartbeat`;
                   agent-key + env-profile runtime overrides
  host_lock.py     One <kind>-<id>.lock file per active run (best-effort visibility)
  host_staging.py  Shared staging dir for agents running on the host (Hermes-SSH)
  routers/         auth, projects, tasks, sessions, heartbeat, env_profiles, ws
  main.py          app factory, lifespan (starts/stops the heartbeat + auto-init DB),
                   SPA serving, CORS wiring
frontend/src/
  api.ts (REST + apiBase/token), auth.tsx, types.ts
  pages/ (Login, Projects, ProjectDetail, SessionPage, AgentWindowPage, Heartbeat, EnvProfiles)
  components/ (TaskConsole, SessionTerminalModal, WindowManager, TaskImages, ui, ...)
```

## Core flows

**Task:** `POST /api/projects/{id}/tasks` (body: `agent`, `prompt`, `mode`,
`runner`, `env_profile_key`, optional `model`, `effort`, `images`) →
`TaskManager.submit` → asyncio task. Prompt = user prompt +
`context_instruction` (AGENTS.md maintenance). Output streams over WS
`/api/ws/tasks/{id}` (with replay from buffer / DB for late or repeated
joins). After run: result → DB → commit/push. Result, commit hash, and
push status stored in DB.

**Model / effort per task:** agents with `model_choices`/`effort_choices`
(`claude`, `codex`) get dropdowns in the UI. Validation 400 on invalid.
CLI argv injection via `model_args`/`effort_args`.

**Image attachments:** `TaskCreate.images` = `[{name, data}]` (Base64 /
data URLs; max 6 images, max 8 MB each, png/jpg/jpeg/gif/webp).
Stored under `data_dir/task_images/{task_id}/` outside the repo, served via
`GET /api/tasks/{id}/images/{name}` (auth required).

**Goal mode (`mode="goal"`):** same path with `goal_command` substitution;
agent works until goal is reached.

**Session mode (`mode="session"`):** real PTY (`os.openpty` + `os.fork`)
runs the agent's `session_command` as a TUI; raw bytes stream both ways
over WS `/api/ws/sessions/{id}`. Bracketed paste (`?2004h` + `\x1b[200~ /
\x1b[201~`). Ctrl+V / Ctrl+Shift+V intentionally returns `null` in
`keyToBytes()` so the browser default paste event fires (TUI image-paste
shortcuts would fail without a display).

**Env profiles (NEW 2026-07-14):** `env_profiles` table +
`/api/env-profiles` CRUD router. Stores `ANTHROPIC_BASE_URL` +
`ANTHROPIC_AUTH_TOKEN` (encrypted via Fernet from `CD_SECRET_KEY`).
`Task.env_profile_key` (or session / heartbeat-spawn equivalent) gets the
overlay applied at run time: the runner merges the two vars onto a
`model_copy` of the cached spec, defensively writing
`ANTHROPIC_API_KEY=""` so a host-shell inherited Anthropic key cannot
leak through.

**Per-task host runner (NEW 2026-07-14):** `Task.runner="host"` resolves
to the `<agent>-host` sibling AgentSpec (auto-created by the Docker
entrypoint when `CD_CLAUDE_SSH_USER` is set; hand-written in `config.yaml`
for systemd). Sibling has `host_staging=True`, reusing the existing
`host_staging.*` copy/merge plumbing exactly like `hermes`-over-SSH.

**Heartbeat path:** `heartbeat.HeartbeatRunner` is one long-lived
`asyncio.Task` started from `main.lifespan()`. Every
`settings.heartbeat_interval_seconds` (default 900s = 15 min) walks every
active (non-archived) project with a `github_full_name`, dedups against
`heartbeat_seen`, and dispatches one task per *newly-seen* issue through
`manager.submit()` (which goes through every backend pipeline as a normal
task). Per-tick parallelism gated by an `asyncio.Semaphore`. Resolves env
profile per task from per-project override → global default → empty.
Heartbeat default is container — host-SSH is only opt-in via
`CD_HEARTBEAT_AGENT_KEY=claude-host`.

**WS reconnect / replay:** WS pub/sub replays the task state from the DB
when the live channel is gone, so reconnects don't lose output.

## Conventions

- Secrets only via env (`CD_*`). Never persist GitHub tokens.
- DB migration-free: `create_all` (no Alembic). Additive columns for
  existing SQLite DBs go into `database._SQLITE_COLUMN_ADDITIONS` as
  idempotent `ALTER TABLE ADD COLUMN` (run after `create_all` in
  `init_db`).
- Backend endpoints that use `asyncio.create_task` must be `async def`.
- **Routers with WebSocket endpoints must not have HTTPBearer / security
  deps at router level.** FastAPI tries to resolve them for
  `@router.websocket` routes, but WS handshakes have no real `Request`
  → `TypeError: HTTPBearer.__call__() missing 1 required positional
  argument: 'request'` → WS closes immediately. HTTP routes in the same
  router declare `Depends(get_current_user)` explicitly in their
  signature; WS auth uses `user_from_token(token)` query-param.
- `git_ops` calls are blocking; call them from the event loop via
  `asyncio.to_thread`.
- **Do not hand-write the `## Latest Run` block in AGENTS.md.** The agent
  maintains it via the appended `context_instruction`; the dashboard
  strips legacy "Letzte Tasks" blocks before pushing. After a change,
  AGENTS.md is the place where future agents record what changed.
- Model / effort injected twice: as argv (`model_args` / `effort_args`)
  *and*, only for built-in `claude` / `codex` keys, by writing the
  agent's own config (`~/.claude/settings.json` effort;
  `~/.codex/config.toml` model + `model_reasoning_effort`). Dotfile
  write keys off `spec.key` — renaming those agents silently drops it.
- `CD_CORS_ORIGINS=*` is reflected, not literal — `main.py` sets
  `allow_origin_regex='.*'` with `allow_credentials=True` (a literal `*` is
  invalid with credentials).
- Agents run autonomously (`--dangerously-skip-permissions` / `--yolo` /
  Codex non-interactive) and push without confirmation — intended for
  private repos with a dedicated token; the service runs as non-root user.
- **Shared Hermes/Claude SSH wiring (Docker entrypoint).** Setting EITHER
  `CD_HERMES_SSH_USER` OR `CD_CLAUDE_SSH_USER` lights up BOTH the
  `hermes-host` and `claude-host` siblings at the resolved user/host/port
  (each sibling prefers its own env, falls back to the other agent's).
  Setting BOTH only matters when they point at different hosts. Setting
  NEITHER disables both. Container `hermes` (and `claude`) stay as their
  own entries — only the SSH form moves to the sibling, mirroring the
  existing `claude`/`claude-host` pair. Operator-facing wording lives in
  `deploy/docker/coding-dashboard.docker.env.example`.
- **`<base>-host` is the universal sibling-swap pattern.** Adding a new
  on-host agent = register a sibling under `<base>-host` with
  `host_staging=True`; the per-task Runner dropdown, the heartbeat
  `available_agent_keys` selector, the SessionManager / TaskManager
  `<base>-host` swap shims and the front-end `host_agent_key` resolver
  all pick it up with no code change.

## Gotchas

- **Heartbeat off by default.** `CD_HEARTBEAT_ENABLED=false` is the
  shipped default (systemd `install.sh` and Docker compose).
- **Heartbeat dedup is one-way insert.** `heartbeat_seen` is a composite
  primary key `(project_id, issue_number)`. Once an issue is recorded, it
  is never re-considered. To retry: delete the row by hand, or close +
  reopen the issue (new `number`).
- **`POST /api/heartbeat/trigger` awaits the tick.** Unlike most
  fire-and-forget admin endpoints, the trigger returns only after the
  tick completes (or after a `tick_lock` collision → `status="already_running"`).
  This makes it the "Run now" button AND a reliable test entry point.
- **`CD_HEARTBEAT_AGENT_KEY` must exist in `agents.config`.** Otherwise
  the tick returns `no_agent` and dispatches nothing — does NOT fall
  back.
- **Env-profile auth tokens are write-only.** The GET response never
  echoes plaintext; only `anthropic_auth_token_set: bool` + an anonymised
  hint like `"sk-…12"`. To rotate, PATCH with a new token. To clear,
  PATCH with `""`. `CD_SECRET_KEY` rotation invalidates all stored
  tokens; setting it back to the bundled default disables token writes
  (CRUD returns 503).
- **When ANY profile field is set, `ANTHROPIC_API_KEY=""` is stamped.**
  Leaving `ANTHROPIC_API_KEY` unset lets the host's shell export a
  leaked upstream token that would silently hit the wrong endpoint.

## Latest runs

### 2026-07-14 — claude (env profiles + per-task host runner for Claude Code)

**What:** Two new operator-facing features — (a) Fernet-encrypted
`env_profiles` for `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`, with
a per-task picker on the start form AND per-project + global override
for the heartbeat, and (b) a per-task "Runner: host" toggle for
Claude Code, mirroring the Hermes-over-SSH pattern (when
`CD_CLAUDE_SSH_USER` is set, the Docker entrypoint auto-creates a
`claude-host` sibling with `host_staging=True`; systemd operators
hand-write the same block in `config.yaml`).

**Critical files (new):**
- `backend/app/env_crypto.py` — Fernet wrapper via HKDF-SHA256 bound to
  `Settings.secret_key`. `is_encryption_available()` returns False when
  the literal `"CHANGE-ME-…"` placeholder is still in place → CRUD
  routes 503 on token writes.
- `backend/app/routers/env_profiles.py` — `/api/env-profiles` CRUD,
  write-only token semantics (`***` placeholder rejected with 422),
  `409` on duplicate key.
- `frontend/src/pages/EnvProfiles.tsx` — CRUD page (write-only token
  field with anonymised hint).

**Critical files (modified):**
- `backend/app/agents.py` — `_build_env_for(spec, *, extra)` sibling
  to `_build_env(spec)`. `run_agent` signature unchanged; the overlay
  is pre-baked into a `model_copy` of the cached `AgentSpec` before
  the subprocess is launched.
- `backend/app/task_runner.py` — runner shim in `_run_inner` (swap to
  `<agent>-host` sibling when `runner="host"`) + `_build_env_overlay`
  helper that fetches the EnvProfile, decrypts the token, and stamps
  `ANTHROPIC_API_KEY=""` defensively. `SessionManager.start` gets the
  same two knobs (`runner`, `env_profile_key`); session workdir
  auto-routes through `host_staging.session_staging_dir(...)` because
  the sibling carries `host_staging=True`.
- `backend/app/models.py` — `Task.env_profile_key`, `Task.runner`,
  `Project.heartbeat_env_profile_key` + new `EnvProfile` ORM model.
- `backend/app/database.py` — `_SQLITE_COLUMN_ADDITIONS` extended; the
  `env_profiles` table is created by `Base.metadata.create_all()`.
- `backend/app/heartbeat.py` — `Settings.heartbeat_env_profile_key`
  (env `CD_HEARTBEAT_ENV_PROFILE_KEY`), per-tick resolver
  (`HeartbeatRunner._resolve_env_profile_key`: per-project override →
  global default → empty), `_create(...)` writes the resolved key onto
  the Task row so the existing `_run_inner` overlay path picks it up.
- `backend/app/routers/heartbeat.py` — new
  `POST /api/projects/{id}/heartbeat/env-profile` for the per-project
  override; `GET /api/heartbeat` surfaces both `env_profile_key` at
  the top level and per-project.
- `deploy/docker/entrypoint.sh` — new `CD_CLAUDE_SSH_USER` /
  `CD_CLAUDE_SSH_HOST` / `CD_CLAUDE_SSH_PORT` env reads + a
  `claude-host` sibling generation branch that mirrors the Hermes-SSH
  one, including `exec env -u ANTHROPIC_API_KEY` on the remote shell.
- `frontend/src/pages/ProjectDetail.tsx` — Runner + Env-Profil
  dropdowns (gated on `currentAgent.host_agent_key` and
  `currentAgent.key === "claude"` respectively), reset on
  agent/mode change.
- `frontend/src/pages/Heartbeat.tsx` — per-project Env-Profil select in
  the status table + global chip in the header.
- `frontend/src/pages/EnvProfiles.tsx` (new), `frontend/src/api.ts`,
  `frontend/src/types.ts` — CRUD helpers, `Runner` + `EnvProfile`
  types, `runner` + `env_profile_key` on Task.

**Verified:** `python tests/smoke.py` → 482 checks, 480 PASS. All pre-
existing checks still PASS (incl. archive, heartbeat, host-staging,
host-lock, hb-bypass, hb-assignee, hb-comment). Nine new test
functions add the env-profile + runner coverage: `test_env_crypto`,
`test_env_profiles_crud`, `test_env_profiles_encryption_gated`,
`test_task_runner_env_profile_injection`,
`test_runner_toggle_persistence`,
`test_runner_fallback_when_ssh_not_configured`, `test_session_runner_shim`,
`test_create_task_persists_env_profile_key`,
`test_heartbeat_env_profile_resolution`. The only failures are the 2
pre-existing CORS failures on `main` (unrelated). Effective after
`update.sh` / `systemctl restart coding-dashboard` (or next container
start). No Alembic — additive columns handled by
`_ensure_sqlite_columns()`. New pip dep: `cryptography>=42`.
