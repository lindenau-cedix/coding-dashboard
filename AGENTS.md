# AGENTS.md — Coding Dashboard

Gemeinsamer Kontext für KI-Agenten (Claude Code / Hermes / Codex) und Mitwirkende.
Kurz halten, aktuell halten.

## Zweck
Self-hosted Dashboard, um Coding-Aufgaben pro Projekt an Claude Code, Hermes oder Codex
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
  agents.py        run_agent(): Subprocess + Streaming + claude-json/raw Parser
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
  Agenten (`claude`, `hermes`, `codex`) füllt `load_agents_config` fehlende Felder
  aus `default_agents()` auf; die `config.yaml` überschreibt nur explizit gesetzte
  Felder. Alte installer-generierte Configs mit `claude`/`hermes` bekommen neue
  eingebaute Agenten wie `codex` beim Neustart automatisch dazu; reine Custom-
  Configs bleiben explizit. So erhalten bestehende Installationen neue optionale
  Felder/Agenten ohne `/etc/coding-dashboard/config.yaml` von Hand editieren zu
  müssen (`update.sh` lässt eine bestehende Config bewusst unangetastet).
  Claude: `claude -p … stream-json`. Hermes: `hermes chat -q {prompt} --yolo
  --accept-hooks` (nicht-interaktiv, streamt Zwischenschritte, lädt AGENTS.md aus
  CWD; dazu `env: HERMES_ACCEPT_HOOKS=1, NO_COLOR=1` und
  `unset_env: [PYTHONPATH, PYTHONHOME]`). Codex: `codex exec --cd {project_dir} --sandbox workspace-write --color never --ephemeral -`
  mit `prompt_via: stdin` (kein `goal_command`, daher kein Ziel-Modus für Codex).
  `--ask-for-approval` existiert nicht in aktuellen Codex-Versionen — das Command
  ist von sich aus nicht-interaktiv wenn ein Prompt übergeben wird.
  Raw-Output wird im Runner ANSI-gefiltert.
- **AGENTS.md-Aktualisierung:** Nach jedem abgeschlossenen Task aktualisiert
  `TaskManager._update_agents_md()` die `AGENTS.md` im Projektverzeichnis:
  Das ``## Letzte Tasks``-Block wird ersetzt (oder neu angehängt) mit den
  letzten 3 erfolgreichen/failed Tasks (Datum, Agent, Zusammenfassung).
  Der Agent selbst aktualisiert den Rest der AGENTS.md über die
  `context_instruction` (siehe oben).
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
- Optional: Stash/Pull-Rebase-Option bei Pull-Konflikten (aktuell `git pull origin branch`,
  kein Stash-Schutz).
- **2026-06-11:** Pull-Button zeigt Terminal-Output jetzt in einem Modal-Dialog an.
  `git_ops.pull` gibt `result.stdout.strip()` zurück (vorher `None`); `pull_project`
  Router antwortet mit `{ok, branch, output}`; Frontend zeigt `Modal`-Dialog mit
  farbcodiertem Output (erfolgreich = slate, fehlgeschlagen = red).
- **2026-06-11 (Fix):** `Modal` war in `ProjectDetail.tsx` verwendet aber nicht
  importiert → TypeScript-Fehler TS2304, trotz Vite-Build. Fix: `Modal` zum
  ui-Import hinzugefügt.


## Letzte Tasks

- **2026-06-08 12:10** [claude] Erledigt. Zusammenfassung:

 Ursache
Der `/goal`-Toggle erscheint im UI nur, wenn das Backend `supports_goal=true` für den Agenten meldet — das ist genau dann der Fall, wenn `goal_command` in der Agent-Config gesetzt ist (`routers/tasks.py:25`).

Das Backend liest die Config aus `/etc/coding-dashboard/config.yaml` (per `CD_AGENTS_CONFIG_PATH`). Diese Datei wurde am 5. Juni vom Installer erzeugt — vor dem Goal-Feature — und enthält kein `goal_command`. `deploy/update.sh` aktualisiert Code + Frontend, lässt eine bestehende Config aber bewusst unverändert ("Env/config bleiben"). Dein Re-Deploy konnte das Feature also gar nicht aktivieren.

 Fix (dauerhaft, im Code)
In `backend/app/config.py` → `load_agents_config`: Für eingebaute Agenten (`claude`, `hermes`) werden fehlende Felder aus `default_agents()` aufgefüllt; die `config.yaml` überschreibt nur, was sie explizit setzt. Damit:
- bekommt `claude` automatisch `goal_command="/goal {prompt}"` zurück,
- behält `hermes` seinen angepassten absoluten Pfad und seine env-Vars,
- lighten künftige neue Optionalfelder ebenfalls von selbst auf — kein `/etc`-Handediting mehr nötig.

Verifiziert direkt gegen deine echte deployte Config (claude → `goal_command='/goal {prompt}'`, hermes unverändert). Neuer Smoke-Test `test_config_backfill` ergänzt; alle Tests grün. AGENTS.md aktualisiert.

 Was du noch tun musst
Damit der Fix greift, muss der neue Code deployt + Service neu gestartet werden:

```bash
sudo deploy/update.sh
```

(Den Service-Neustart macht `update.sh` selbst. Die `/etc/...config.yaml` musst du nicht anfassen — das war ja der Punkt.)

Ich konnte den Deploy/Neustart nicht selbst ausführen: `sudo` ist in dieser Umgebung durch `NoNewPrivileges` gesperrt, und die Config gehört `root`. Committen/Pushen übernimmt wie gewünscht das Dashboard.

Hinweis: Falls du es lieber sofort ohne vollen Re-Deploy willst, ginge alternativ ein manuelles Einfügen von `goal_command: "/goal {prompt}"` beim `claude`-Agenten in `/etc/coding-dashboard/config.yaml` + `sudo systemctl restart coding-dashboard` — aber der Code-Fix ist die saubere, wiederholbare Lösung.
- **2026-06-11 18:31** [hermes] nderungen:
    
    Backend
    - backend/app/git_ops.py — pull() hinzugefügt (analog zu push(), nutzt git pull origin <branch>)
    - backend/app/routers/projects.py — POST /projects/{id}/pull Endpoint (auth-geschützt, GitError → HTTP 409)
    
    Frontend
    - frontend/src/api.ts — api.pullProject(id) hinzugefügt
    - frontend/src/pages/ProjectDetail.tsx — Pull-Button neben dem Branch-Badge im Header; zeigt pullError unter dem allgemeinen error anAGENTS.md — Offener Punkt zu Pull-Konflikten vermerkt.
╰──────────────────────────────────────────────────────────────────────────────╯
- **2026-06-11 18:36** [hermes] ersetzt (oder neu angehängt) mit je einem Eintrag: Datum [agent] Zusammenfassung
    - AGENTS.md existiert noch nicht → wird neu angelegt mit leerem Header + dem Block- Der Agent selbst pflegt den Rest der AGENTS.md (Struktur, Tech-Stack, Entscheidungen) über die context_instruction — das Dashboard kümmert sich nur um das Letzte-Tasks-Block
╰──────────────────────────────────────────────────────────────────────────────╯

Resume this session with:
  hermes --resume 20260611_203430_9e19b2

Session:        20260611_203430_9e19b2
Duration:       1m 30s
Messages:       30 (1 user, 28 tool calls)
