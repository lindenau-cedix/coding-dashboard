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
  agents.py        run_agent(): Subprocess + Streaming, claude-json/raw Parser,
                   Modell/Effort-Arg-Injektion, Endausgabe-Extraktion
                   (_final_output-Heuristik bzw. {last_message_file})
  uploads.py       Bild-Anhänge: Base64/Data-URL-Decode + Validierung,
                   Speicherung unter data_dir/task_images/{task_id}/
  task_runner.py   TaskManager: pro-Projekt-Lock, WS-Pub/Sub, AGENTS.md-Pflege,
                   Auto-Commit+Push
  routers/         auth, projects, tasks, ws
  main.py          App-Factory, lifespan, SPA-Auslieferung (Fallback)
frontend/src/
  api.ts (REST + apiBase/Token), auth.tsx, types.ts
  pages/ (Login, Projects, ProjectDetail), components/ (TaskConsole, TaskImages, ui, ...)
deploy/            install.sh, update.sh, uninstall.sh, build-android.sh, unit, nginx, *.example
```

## Kernabläufe
- **Task:** `POST /api/projects/{id}/tasks` (Body: `agent`, `prompt`, `mode`,
  optional `model`, `effort`) → `TaskManager.submit` → asyncio-Task. Prompt =
  User-Prompt + `context_instruction` (AGENTS.md-Pflege). Output streamt über WS
  `/api/ws/tasks/{id}` (mit Replay aus Buffer/DB für späte/erneute Joins).
  Reihenfolge nach dem Agentenlauf: Ergebnis in DB → `finished_at` setzen →
  `_update_agents_md()` (s.u.) → `git_ops`: `add -A` → commit (falls Änderungen)
  → push (immer) — der Push enthält also IMMER die aktualisierte AGENTS.md.
  Ergebnis + Commit-Hash + Push-Status in DB.
- **Modell/Effort pro Task:** Agenten mit `model_choices`/`effort_choices`
  (Claude, Codex) bekommen im UI Dropdowns ("Standard" = leer = CLI-Default).
  Auswahl wird in `tasks.model`/`tasks.effort` gespeichert, gegen die Choices
  validiert (400 sonst) und via `model_args`/`effort_args` in die argv injiziert
  (vor einem abschließenden `-`-stdin-Marker, sonst angehängt — explizite
  `command`-Listen in config.yaml funktionieren daher unverändert).
  Claude: `--model {opus|sonnet|haiku}`, `--effort {low|medium|high|xhigh|max}`.
  Codex: `--model …`, `-c model_reasoning_effort={low|medium|high|xhigh}`.
- **Bild-Anhänge:** `TaskCreate.images` = Liste `{name, data}` (data = Base64,
  Data-URL erlaubt; max 6 Bilder, je max 8 MB, nur png/jpg/jpeg/gif/webp;
  Validierung in `uploads.decode_images`, 400 bei Fehler VOR dem Anlegen des
  Task-Rows). Speicherung AUSSERHALB des Repos unter
  `data_dir/task_images/{task_id}/` (Auto-Commit nimmt sie daher nie mit);
  Dateinamen sanitisiert/dedupliziert, als JSON-Liste in `tasks.images`
  (TEXT-Spalte). Der Agent bekommt die absoluten Pfade als Block "Angehängte
  Bilder" an den Prompt angehängt (`build_agent_prompt(..., image_paths=…)`)
  und öffnet sie mit seinem eigenen Read-/Bild-Tool. Auslieferung ans UI über
  `GET /api/tasks/{id}/images/{name}` (nur in `tasks.images` registrierte
  Namen → kein Traversal; Auth nötig, daher lädt das Frontend per fetch +
  Object-URL statt direktem `<img src>`). UI: Datei-Button + Strg+V-Paste ins
  Textfeld, Vorschau mit Entfernen, Thumbnails in der Historie
  (`components/TaskImages.tsx`). Beim Projekt-Löschen werden die Bildordner
  der Tasks mitgelöscht.
- **Endausgabe (`Task.result_summary`):** Priorität: Inhalt von
  `{last_message_file}` (Codex `--output-last-message`, exakt die letzte
  Agent-Nachricht) → Parser-Summary (Claude `result`-Event) →
  `_final_output()`-Heuristik (raw/Hermes: letzter Absatz, Box-Zeichen und
  Session-Footer wie "Resume this session"/"Session:"/"tokens used" gefiltert).
- **Ziel-Modus (`mode="goal"`):** Statt einer Aufgabe gibt man ein Ziel an. Das
  Backend ruft den Agenten über dessen `goal_command`-Template auf (Claude:
  `/goal {prompt}`); der komplette Verlauf bis zum Ziel zählt als ein Task. Alles
  Weitere (Streaming, AGENTS.md-Pflege, Commit, Push, Historie) ist identisch.
  Prompt-Bau zentral in `task_runner.build_agent_prompt()`. Nur Agenten mit
  gesetztem `goal_command` bieten den Modus an (`AgentInfo.supports_goal`); das UI
  blendet den Umschalter dann ein und filtert die Agentenliste entsprechend.
- **Agent-Config:** `config.yaml`. Platzhalter `{prompt}`, `{project_dir}`,
  `{last_message_file}` (temp. Datei für die letzte Agent-Nachricht).
  `stream_format: claude-json|raw`, `prompt_via: arg|stdin`, `env`, `unset_env`,
  `goal_command` (optional, aktiviert Ziel-Modus), `model_choices`/`model_args`/
  `effort_choices`/`effort_args` (optional, aktivieren die Modell-/Effort-
  Dropdowns). **Backfill:** Für eingebaute
  Agenten (`claude`, `hermes`, `codex`) füllt `load_agents_config` fehlende Felder
  aus `default_agents()` auf; die `config.yaml` überschreibt nur explizit gesetzte
  Felder. Alte installer-generierte Configs mit `claude`/`hermes` bekommen neue
  eingebaute Agenten wie `codex` beim Neustart automatisch dazu; reine Custom-
  Configs bleiben explizit. So erhalten bestehende Installationen neue optionale
  Felder/Agenten ohne `/etc/coding-dashboard/config.yaml` von Hand editieren zu
  müssen (`update.sh` lässt eine bestehende Config bewusst unangetastet).
  Claude: `claude -p … stream-json` (Parser zeigt Tool-Calls mit Detail, z.B.
  `[tool] Bash: ls -la` / `[tool] Read: pfad`, statt nur des Tool-Namens).
  Beide Command-Varianten (task + goal) kommen ohne `--use-auth-token` aus.
  Hermes: `hermes chat -q {prompt} --yolo --accept-hooks` (nicht-interaktiv,
  streamt Zwischenschritte, lädt AGENTS.md aus CWD; dazu
  `env: HERMES_ACCEPT_HOOKS=1, NO_COLOR=1` und
  `unset_env: [PYTHONPATH, PYTHONHOME]`). Codex: `codex exec --cd {project_dir}
  --sandbox workspace-write --color never --ephemeral --output-last-message
  {last_message_file} -` mit `prompt_via: stdin` (kein `goal_command`, daher
  kein Ziel-Modus für Codex). `--ask-for-approval` existiert nicht in aktuellen
  Codex-Versionen — das Command ist von sich aus nicht-interaktiv wenn ein
  Prompt übergeben wird. Raw-Output wird im Runner ANSI-gefiltert.
- **AGENTS.md-Aktualisierung:** Nach jedem abgeschlossenen Task (success/failed)
  führt der Agent über die `context_instruction` den Block `## Letzter Durchlauf`
  GANZ AM ANFANG der AGENTS.md (direkt nach dem Titel und dem Zweck-Absatz):
  eine kurze Zusammenfassung dessen, was er in diesem Lauf getan hat. Das Dashboard
  schreibt diesen Block NICHT mehr -- es prüft nur noch vor dem Push, ob alte
  `## Letzte Tasks`-Blöcke (von Dashboards vor 2026-06-12) in der Datei existieren,
  und entfernt sie falls nötig. So bleibt die Datei sauber und der Agent führt
  seinen eigenen Abschnitt. Läuft VOR dem Commit/Push-Schritt.
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
- **2026-06-12:** Modellliste aktualisiert: Claude Code hat jetzt `fable` als viertes Modell;
  Codex verwendet `gpt-5.4`, `gpt-5.5`, `gpt-5.4-mini` (vorher `gpt-5.1-*`).
  Bei Claude Code wird bei gesetztem Effort-Level zusätzlich `~/.claude/settings.json`
  geschrieben (Key: `effort`), damit das Level garantiert genutzt wird — nicht nur
  per `--effort`-Flag in der CLI. Erst nach `systemctl restart coding-dashboard`
  wirksam.
- Optional: Alte Bildordner abgeschlossener Tasks aufräumen (derzeit bleiben
  sie für die Historie-Anzeige unbegrenzt liegen; gelöscht nur mit dem Projekt).
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
- **2026-06-11 (Überarbeitung Letzte-Tasks/Modell/Effort):**
  1. `_update_agents_md()` läuft jetzt VOR Commit/Push (vorher danach → die
     AGENTS.md-Änderung wurde erst vom Folge-Task gepusht) und `finished_at`
     wird vorher gesetzt (vorher fehlte der aktuelle Task oft in den letzten 3,
     weil `ORDER BY finished_at DESC` NULL ans Ende sortiert). Neues Format:
     pro Lauf Aufgabe + NUR die Endausgabe (statt 600-Zeichen-Tail mit
     Hermes-Box-/Footer-Müll).
  2. Endausgabe-Quellen: Codex `--output-last-message {last_message_file}`,
     Claude `result`-Event, raw `_final_output()`-Heuristik.
  3. Claude-Parser zeigt Tool-Calls mit Detail (`[tool] Bash: ls -la`).
  4. Modell-/Effort-Auswahl pro Task für Claude Code und Codex (Backend-Felder,
     DB-Spalten `tasks.model`/`tasks.effort`, UI-Dropdowns).
  WICHTIG: Wird erst nach einem Service-Neustart wirksam
  (`systemctl restart coding-dashboard`) — nicht aus einem laufenden Task heraus
  neustarten, das killt den eigenen Lauf.
- Codex-`model_choices` (gpt-5.1-codex…) sind Stand 2026-06; bei neuen
  Codex-Releases ggf. in `default_agents()` / config.yaml nachziehen.

## Letzter Durchlauf

_Der Agent führt diesen Block bei jedem Durchlauf selbst: kurze Zusammenfassung
der Aufgabe, der wichtigsten Änderung/Erkenntnis und des Ergebnisses.
Das Dashboard entfernt lediglich noch alte "Letzte Tasks"-Blöcke (vor 2026-06-12)._

### 2026-06-12 13:00 — hermes

**Was getan:** Die AGENTS.md-Pflege umgestellt: Der Agent führt jetzt selber
einen `## Letzter Durchlauf`-Block am Anfang der Datei (nach Titel + Zweck),
der bei jedem Lauf überschrieben wird. Das Dashboard schreibt nicht mehr die
letzten 3 Läufe in einen `## Letzte Tasks`-Block am Ende, sondern prüft nur
noch, ob alte "Letzte Tasks"-Blöcke (von Dashboards vor 2026-06-12) existieren
-- und entfernt sie falls nötig. Änderungen: `config.py` (context_instruction
Punkt 5), `task_runner.py` (_update_agents_mdcleanup), `smoke.py` (Tests
angepasst), alle 82 Smoke-Tests bestanden.

**Nächste Schritte:** Nach `systemctl restart coding-dashboard` wirksam.

### 2026-06-12 13:30 — hermes

**Was getan:** `npm audit fix` im frontend: 10 → 6 Vulnerabilities.
- **uuid** (moderate): via `overrides: {uuid: "^11.1.1"}` in package.json gefixt.
- **esbuild/vite** (moderate): `vite@8.0.16` installiert (Vite 8 Major-Upgrade).
- **minimatch + tar** (high, 6x): keine Fixes verfügbar — stammen aus
  `@capacitor/assets` → `@trapezedev/project` → `replace`/`xcode`-Ketten im
  Capacitor-Android-Build-Tooling. Nur Upstream-Fixes möglich.
Build erfolgreich (`vite build`, 415ms).

**Nächste Schritte:** Nach `systemctl restart coding-dashboard` wirksam.

