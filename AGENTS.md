# AGENTS.md — Coding Dashboard

Gemeinsamer Kontext für KI-Agenten (Claude Code / Hermes) und Mitwirkende.
Kurz halten, aktuell halten.

## Zweck
Self-hosted Dashboard, um Coding-Aufgaben pro Projekt an Claude Code oder Hermes
zu delegieren: Repo anlegen/importieren → Aufgabe an einen Agenten → Live-Output
→ automatischer Commit & Push → Historie. Web + Android.

## Tech-Stack
- Backend: Python, **FastAPI** + Uvicorn, **SQLAlchemy 2** über **SQLite**. Sync-DB.
- Frontend: **React 18 + Vite + TypeScript + Tailwind v4**, **HashRouter**.
- Android: **Capacitor** (wrappt `frontend/dist`).
- Auth: Single-User, Passwort (pbkdf2) → **JWT** (PyJWT).
- Deploy: **systemd** + **nginx**, Install-Scripts in `deploy/`.

## Struktur
```
backend/app/
  config.py        Settings (env CD_*) + Agent-Config (YAML) + context_instruction
  database.py      Engine/Session (SQLite), init_db, session_scope
  models.py        Project, Task
  schemas.py       Pydantic I/O
  security.py      pbkdf2 Hash + JWT
  auth.py          get_current_user (Bearer), user_from_token (WS)
  github_client.py GitHub REST (create/get/delete repo)
  git_ops.py       clone/commit/push (Token nur als http.extraheader, nie in config)
  agents.py        run_agent(): Subprocess + Streaming + claude-json Parser
  task_runner.py   TaskManager: pro-Projekt-Lock, WS-Pub/Sub, Auto-Commit+Push
  routers/         auth, projects, tasks, ws
  main.py          App-Factory, lifespan, SPA-Auslieferung (Fallback)
frontend/src/
  api.ts (REST + apiBase/Token), auth.tsx, types.ts
  pages/ (Login, Projects, ProjectDetail), components/ (TaskConsole, ui, ...)
deploy/            install.sh, update.sh, uninstall.sh, build-android.sh, unit, nginx, *.example
```

## Kernabläufe
- **Task:** `POST /api/projects/{id}/tasks` (Body: `agent`, `prompt`, `mode`) →
  `TaskManager.submit` → asyncio-Task. Prompt = User-Prompt + `context_instruction`
  (AGENTS.md-Pflege). Output streamt über WS `/api/ws/tasks/{id}` (mit Replay aus
  Buffer/DB für späte/erneute Joins). Danach `git_ops`: `add -A` → commit (falls
  Änderungen) → push (immer). Ergebnis + Commit-Hash + Push-Status in DB.
- **Ziel-Modus (`mode="goal"`):** Statt einer Aufgabe gibt man ein Ziel an. Das
  Backend ruft den Agenten über dessen `goal_command`-Template auf (Claude:
  `/goal {prompt}`); der komplette Verlauf bis zum Ziel zählt als ein Task. Alles
  Weitere (Streaming, AGENTS.md-Pflege, Commit, Push, Historie) ist identisch.
  Prompt-Bau zentral in `task_runner.build_agent_prompt()`. Nur Agenten mit
  gesetztem `goal_command` bieten den Modus an (`AgentInfo.supports_goal`); das UI
  blendet den Umschalter dann ein und filtert die Agentenliste entsprechend.
- **Agent-Config:** `config.yaml`. Platzhalter `{prompt}`, `{project_dir}`.
  `stream_format: claude-json|raw`, `prompt_via: arg|stdin`, `env`, `unset_env`,
  `goal_command` (optional, aktiviert Ziel-Modus). **Backfill:** Für eingebaute
  Agenten (`claude`, `hermes`) füllt `load_agents_config` fehlende Felder aus
  `default_agents()` auf; die `config.yaml` überschreibt nur explizit gesetzte
  Felder. So erhalten bestehende (installer-generierte) Configs neue optionale
  Felder wie `goal_command` automatisch beim nächsten Neustart — ohne
  `/etc/coding-dashboard/config.yaml` von Hand editieren zu müssen (`update.sh`
  lässt eine bestehende Config bewusst unangetastet).
  Claude: `claude -p … stream-json`. Hermes: `hermes chat -q {prompt} --yolo
  --accept-hooks` (nicht-interaktiv, streamt Zwischenschritte, lädt AGENTS.md aus
  CWD; dazu `env: HERMES_ACCEPT_HOOKS=1, NO_COLOR=1` und
  `unset_env: [PYTHONPATH, PYTHONHOME]`). Raw-Output wird im Runner ANSI-gefiltert.
- **Serialisierung:** pro Projekt ein `asyncio.Lock` (kein Git-Race); verschiedene
  Projekte laufen parallel. Laufende Tasks werden bei Neustart als `interrupted` markiert.

## Konventionen
- Secrets nur via env (`CD_*`). GitHub-Token nie persistieren.
- DB-Migrationsfrei: `create_all` (kein Alembic). Neue Spalten für bestehende
  SQLite-DBs additiv in `database._SQLITE_COLUMN_ADDITIONS` eintragen (idempotentes
  `ALTER TABLE ADD COLUMN`, läuft nach `create_all` in `init_db`).
- Backend-Endpunkte, die `asyncio.create_task` nutzen, müssen `async def` sein.

## Tests
`backend/tests/smoke.py` (ohne externe Dienste): Security, Parser, Agent-Runner,
voller Git-Commit/Push-Zyklus gegen lokales Bare-Repo, REST + kompletter Task-Run.

## Offene Punkte / mögliche Next Steps
- Optional: Token-Refresh/Logout-Härtung; Multi-User.
- Optional: WS-Disconnect-Erkennung bei stillen, sehr langen Tasks (aktuell beim
  nächsten Publish erkannt).
- Android: Launcher-Icons/Splash (Capacitor-Defaults bis dahin).
- Hermes nutzt `chat -q` (Live-Stream). Leise Alternative ohne Zwischenschritte:
  `command: ["hermes", "-z", "{prompt}"]`.
