# AGENTS.md — Coding Dashboard

Gemeinsamer Kontext für KI-Agenten (Claude Code / Hermes / Codex) und Mitwirkende.
Kurz halten, aktuell halten.

## Letzter Durchlauf

### 2026-06-14 — claude (6 Features: Codex-Output, Fullscreen, AGENTS.md-Refresh, Filebrowser, Parallel-Branches, Agenten-Dashboard)

**Was getan:** Sechs Features geplant und implementiert.
1. **Schönerer Codex-Output:** Neuer `stream_format: "codex"` mit
   `_CodexParser` in `agents.py`. Strippt `[ISO-Timestamp]`-Präfixe, den
   Start-Banner (workdir/model/provider/approval/sandbox/reasoning/session/
   version/`---`) nur am Kopf (`_past_header`-Gate, damit Antworttext, der
   zufällig mit `model`/`session` beginnt, überlebt), den `tokens used`-Footer
   und das `User instructions:`-Prompt-Echo. `bash -lc 'cmd' in /dir` wird zu
   `$ cmd`. Registriert in `_make_parser`; `config.py` setzt codex auf
   `stream_format="codex"`, Literal um `"codex"` erweitert.
2. **Fullscreen für Live-/Historie-/Session-Output:** Neues `FullscreenShell`
   (Portal + Esc-to-close) und `IconButton` in `ui.tsx`. `TaskConsole` bekommt
   Fullscreen-Toggle (+ `title`/`onDismiss`); `SessionTerminalModal` einen
   `expanded`-Zustand (Container-Klassen-Swap); `ProjectDetail` ein
   Fullscreen-Overlay (`fsOutput`) für Historie-Ausgaben.
3. **AGENTS.md-Anzeige aktualisiert automatisch nach einem Run:**
   `ProjectDetail` hält `agentsMdLoaded`-Ref + `reloadAgentsMd()`; `onTaskDone()`
   triggert den Reload, sobald ein Task fertig ist.
4. **Filebrowser pro Projekt mit Seitenvorschau:** Neue Routen
   `GET /projects/{id}/files` (Verzeichnis-Listing, versteckt `.git`) und
   `GET /projects/{id}/file` (Text-Inhalt, NUL→binär, latin-1-Fallback,
   512-KB-Deckel, gepufferter Read statt `read_bytes()`, OSError→403).
   Traversal-Guard `_resolve_within`. Neue Schemas `FileEntry`/`DirListing`/
   `FileContent`, `api.listFiles`/`readFile`, neue `FileBrowser.tsx`
   (Seite-an-Seite, Breadcrumb, reqId-Race-Guard, Fullscreen-Viewer) in
   `ProjectDetail` eingebunden.
5. **Mehrere Tasks/Goals/Sessions parallel auf eigenen Branches + Merge:**
   Kern-Umbau in `task_runner.py`. Jeder isolierte Lauf bekommt ein
   git-worktree auf Branch `cd/<mode>/<task_id[:8]>` (Startpunkt `"HEAD"`,
   nicht `main` — sonst greift DWIM und legt versehentlich `main` an). Der
   Agentenlauf passiert AUSSERHALB des Projekt-Locks (echte Parallelität);
   nur worktree-add und merge sind kurze kritische Sektionen unter dem Lock.
   `_merge_worktree_branch`: commit im worktree → merge in den Projekt-Checkout
   → push `HEAD:base_branch` → cleanup. Konflikt bricht ab, behält+pusht den
   Feature-Branch (Default-Branch bleibt sauber); Solo-Läufe fast-forwarden
   (kein Merge-Commit-Lärm). Neue git_ops-Helfer (`add_worktree`,
   `remove_worktree`, `merge_branch`, `branch_exists`, `push_ref`,
   `delete_branch`, `prune_worktrees`). `Task.merge_state`-Spalte (additiv).
   SessionManager analog (worktree-cwd + Merge-back in `end_session`).
   `reset_interrupted` räumt den worktrees-Root auf.
6. **Startseiten-Dashboard laufender Agenten:** `GET /tasks/running`
   (`RunningTaskOut` mit project_name/slug), neue `RunningAgents.tsx`
   (pollt alle 3 s), in `Projects.tsx` über dem Grid eingebunden.

**Ergebnis:** Alle sechs Features umgesetzt. `read_file` liest jetzt nur noch
einen begrenzten Chunk (kein Voll-Einlesen großer Dateien) und fängt OSError
als 403 ab. `smoke.py` um `test_codex_parser`, `test_worktree_merge`
(clean-FF, zweiter Merge, Konfliktpfad) und Filebrowser-/`/running`-Checks
erweitert. Python-Compile, **120 Smoke-Checks**, Frontend-Typecheck und
Vite-Build grün. Wirksam nach `update.sh` / `systemctl restart
coding-dashboard`. Noch NICHT committet/gepusht.

### 2026-06-13 — codex (Session-Paste: Ctrl+V nicht mehr als TUI-Byte)

**Was getan:** Im Session Mode löste Ctrl+V (und Ctrl+Shift+V) bei jeder
TUI einen Image-Paste-Versuch aus, der in unserem headless PTY ohne X11/
Wayland-Display scheitert. Codex warf *"Failed to paste image: clipboard
unavailable: Unknown error while interacting with the clipboard: X11
server connection timed out because it was unreachable"*, Claude Code
antwortete mit *"no image found"*, Hermes verwarf den Paste still.
- `frontend/src/components/SessionTerminalModal.tsx` `keyToBytes()`:
  Ctrl+V (lowercase und Shift-uppercase) wird jetzt mit `null` returnt,
  *bevor* der Ctrl+X-Branch `String.fromCharCode(...)` das rohe `\x16`
  an die TUI schickt. `onTerminalKeyDown` returnt bei `null` ohne
  `preventDefault()` → Browser-Default überlebt → `paste`-Event feuert
  → `onTerminalPaste` greift → Bracketed-Paste wird gesendet.
- `onTerminalPaste` liest jetzt `text/plain` (mit `text`-Fallback) statt
  nur `text` und droppt alles, was kein Text ist, statt einen Paste
  weiterzuleiten, den die TUI mangels OS-Clipboard nicht verarbeiten kann.

**Ergebnis:** Ctrl+V im Browser-Terminal-Dialog schickt den Clipboard-Text
als DEC-Bracketed-Paste an die TUI; die TUI versucht keinen Image-Paste
mehr und der TUI-Prompt bekommt den Text ganz normal. Python-Compile,
TypeScript-Typecheck, Vite-Build und Smoke-Tests grün.

### 2026-06-13 — codex

**Was getan:** Session Mode Paste-Support (Clipboard, auch mehrzeilig)
sowie Commit/Push-Status in der Historie-Header-Zeile.
- Backend `task_runner.SessionManager.start()` schreibt direkt nach
  PTY-Aufbau einmal `\x1b[?2004h` an die TUI, um DEC Bracketed Paste
  Mode (Modus 2004) zu aktivieren. TUIs, die das nicht selbst tun,
  akzeptieren Pasten damit trotzdem als ein zusammenhängendes Event;
  TUIs, die es schon aktiviert haben, sind idempotent.
- Frontend `SessionTerminalModal.onTerminalPaste` umschließt den
  ausgelesenen Clipboard-Text mit `\x1b[200~ ... \x1b[201~`. Dadurch
  interpretieren Claude Code, Codex, Hermes etc. einen mehrzeiligen
  Paste nicht mehr als eine Serie von Enter-Submits. Ohne diese
  Sequenzen löste jeder `\n` im Paste einen Submit der (möglicherweise
  halb-)fertigen Eingabe aus.
- Frontend `ProjectDetail` zeigt in der Header-Zeile jedes
  Historie-Eintrags jetzt `⎇ <commit-hash>` (als Link zur
  GitHub-Commit-Seite, falls vorhanden) sowie `gepusht ✓` (grün) oder
  `nicht gepusht` (amber). Für laufende/queued Tasks erscheint ein
  dezenter `—` als Platzhalter. Das gilt für alle Tasks, nicht nur
  Sessions — gleicher Look wie die Footer-Zeile in
  `SessionTerminalModal` direkt nach `end_session`.

**Ergebnis:** Pasten aus dem Browser in eine laufende TUI-Session
funktionieren mehrzeilig und prompt-treu, und der Git-Status jedes
abgeschlossenen Tasks ist ohne Aufklappen des Eintrags sichtbar.
Python-Compile, Frontend-Typecheck, Frontend-Build und der volle
Smoke-Test (96 Checks) sind grün.

### 2026-06-13 — codex

**Was getan:** Schwarzen Bildschirm im Session Mode behoben.
- `POST /api/sessions` wartet jetzt, bis der `SessionManager` den PTY und den
  Live-Channel angelegt hat; dadurch kann der Browser-WebSocket nicht mehr vor
  dem Channel in den Replay-/Done-Pfad fallen.
- `/api/ws/sessions/{task_id}` wartet bei gerade startenden Sessions kurz auf
  den Channel, bevor es eine laufende Session als nicht-live behandelt.
- PTY-/Fork-Startfehler werden in `Task.output`, Status und Summary persistiert,
  statt nur unsichtbar im Channel zu landen.
- `SessionTerminalModal` zeigt nun Verbindungs-, Leer- und Fehlerzustände
  explizit an; WebSocket-/Cloudflare-Fehler sind nicht mehr nur eine schwarze
  Terminalfläche. Wenn der Mini-Renderer aus TUI-Steuersequenzen keinen sichtbaren
  Screen erzeugt, wird ein ANSI-bereinigter Text-Fallback angezeigt.

**Ergebnis:** Session-Start-Race geschlossen und Terminal-UI gegen leeren Output,
geschlossene WebSockets und fehlenden `ResizeObserver` gehärtet. Python-Kompilierung,
Frontend-Typecheck, Frontend-Build und ein isolierter Fake-PTY-Session-Check
waren erfolgreich. Voller Smoke-Test bleibt durch den bekannten
FastAPI/Starlette-`TestClient`-Hänger blockiert (`timeout 70s`).

### 2026-06-13 — codex (Session-WebSocket: HTTPBearer-Crash gefixt)

**Was getan:** "Terminal-WebSocket konnte nicht geöffnet werden" + "Terminal-
Verbindung geschlossen" im Session Mode behoben.
- `routers/sessions.py` hatte `dependencies=[Depends(get_current_user)]` auf
  Router-Ebene. `get_current_user` ruft intern `HTTPBearer` auf, das einen
  echten `Request` mit Bearer-Header braucht. Bei WebSocket-Handshakes ist
  dieser Request `None` → `TypeError: HTTPBearer.__call__() missing 1 required
  positional argument: 'request'` → WebSocket schließt sofort ohne Daten
  → Browser sah `onerror` + `onclose`.
- Fix: Router-level `dependencies=` entfernt. Die drei HTTP-Routen
  (`POST /sessions`, `GET /sessions/{task_id}`, `POST /sessions/{task_id}/end`)
  hängen `Depends(get_current_user)` jetzt explizit an die Funktionssignatur.
  Der `@router.websocket("/ws/sessions/{task_id}")` macht weiterhin seine
  eigene Auth via `user_from_token(token)` (Query-Param).
- Verifiziert: Patch-Routing-Tree der WebSocket-Route ist jetzt leer (kein
  HTTPBearer mehr), HTTP-Routen behalten Auth, alle 47 Smoke-Tests grün.

**Ergebnis:** Session Mode ist nach dem nächsten `update.sh`/`systemctl restart
coding-dashboard` wieder voll interaktiv. Vorher lief die PTY im Hintergrund
weiter und sammelte Output in `Task.output` — der Browser konnte nur den
bereits persistierten Transcript anzeigen, aber keine Tasten senden, weil der
WebSocket sofort mit dem TypeError abbrach.

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
  pages/ (Login, Projects, ProjectDetail, SessionPage-Wrapper),
  components/ (TaskConsole, SessionTerminalModal, TaskImages, ui, ...)
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
  `goal_command` (optional, aktiviert Ziel-Modus), `session_command` (optional,
  aktiviert TUI-Session-Modus), `model_choices`/`model_args`/
  `effort_choices`/`effort_args` (optional, aktivieren die Modell-/Effort-
  Dropdowns für Task/Goal). **Backfill:** Für eingebaute
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
  Prompt übergeben wird. Raw-Output wird im Runner ANSI-gefiltert. TUI-Session-
  Defaults: Claude `claude`, Hermes `hermes chat`, Codex `codex`; zusätzliche
  Flags nur über das Startparameter-Feld.
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
- **Session Mode (`mode="session"`):** Shellinabox-artige TUI-Sessions im Browser
  (PTY-basiert). Der Agent startet im Projektordner über seinen TUI-Basisbefehl
  ohne Prompt-Injection; optionale Startparameter kommen aus einem eigenen UI-Feld
  und werden serverseitig mit `shlex.split()` als argv angehängt (keine Shell).
  - `Task.is_session=True`, `Task.prompt` speichert die Startparameter,
    `Task.output` speichert den kompletten rohen TUI-Transcript laufend.
    `chat_history` bleibt nur für alte Daten kompatibel.
  - Backend: `SessionManager.start()` forked einen PTY (`os.openpty` + `os.fork`),
    setzt `TERM=xterm-256color`, startet `session_command + start_args`, liest rohe
    Bytes aus dem PTY und appended sie mit Offset in `Task.output`. Output-Events
    sind `{type:"output", data, offset}`.
  - Agent-spezifisch: `session_command` in `AgentSpec` ist der TUI-Basisbefehl.
    Built-ins: Claude `["claude"]`, Hermes `["hermes","chat"]`, Codex `["codex"]`.
    Modell-/Effort-Dropdowns werden im Session Mode nicht injiziert; dafür sind
    explizite Startparameter gedacht.
  - WebSocket `/api/ws/sessions/{task_id}?token=…&offset=N` leitet
    `{type:"message",content}` als rohe UTF-8-Bytes an den PTY weiter und akzeptiert
    `{type:"resize",cols,rows}` für `TIOCSWINSZ` + `SIGWINCH`.
  - **Bracketed Paste:** `SessionManager.start()` aktiviert direkt nach
    PTY-Aufbau einmal DEC-Modus `?2004h`. `SessionTerminalModal.onTerminalPaste`
    wickelt den ausgelesenen Clipboard-Text zusätzlich in
    `\x1b[200~ ... \x1b[201~` ein. Mehrzeilige Pasten werden damit von
    Claude Code / Codex / Hermes als ein zusammenhängendes Event behandelt
    und nicht in eine Serie von Enter-Submits zerlegt.
  - **Ctrl+V (und Ctrl+Shift+V) werden NICHT als rohes `\x16` an die TUI
    geschickt.** `keyToBytes()` returnt für Ctrl+V absichtlich `null`,
    damit der Browser-Default das `paste`-Event feuert und
    `onTerminalPaste` den Bracketed-Paste bauen kann. Hintergrund: Die
    gängigen TUIs (Codex siehe `codex-rs/tui/src/chatwidget/interaction.rs`,
    Claude Code, Hermes) interpretieren Ctrl+V als Image-Paste-Shortcut
    und rufen intern `arboard::Clipboard::new()` auf, um ein Bild aus
    dem OS-Clipboard zu lesen. In unserem headless PTY ohne DISPLAY
    schlägt das mit "X11 server connection timed out" / "no image
    found" fehl und der Text-Paste geht verloren. Nur der Bracketed-Paste
    via Browser-`paste`-Event erreicht die TUI zuverlässig.
  - Frontend: `SessionTerminalModal` öffnet direkt in `ProjectDetail` als Dialog,
    rendert den Transcript über eine kleine ANSI/Cursor-Emulation, sendet
    Pfeiltasten/Enter/Tab/Ctrl+C/Paste als rohe Terminalsequenzen und lädt bei
    Reopen den gespeicherten `Task.output` weiter. Die alte Session-Route ist nur
    noch ein Wrapper auf denselben Dialog.
  - Fenster schließen / Dialog schließen beendet die Session NICHT. Solange der
    Backend-Prozess lebt, kann die Session aus der Historie wieder geöffnet werden.
  - Nach `end_session`: PTY-Prozessgruppe via `os.killpg(pid, SIGTERM)` beendet
    (manuelles Beenden zählt als success), `result_summary =
    "Interaktive TUI-Session beendet"`, danach Git-Commit falls Änderungen und
    Push immer analog zum normalen Task.
  - **Limitation:** Nach `systemctl restart coding-dashboard` sind laufende Sessions
    beendet (Server-Prozess weg); der bis dahin persistierte Transcript bleibt in
    `Task.output`.

## Konventionen
- Secrets nur via env (`CD_*`). GitHub-Token nie persistieren.
- DB-Migrationsfrei: `create_all` (kein Alembic). Neue Spalten für bestehende
  SQLite-DBs additiv in `database._SQLITE_COLUMN_ADDITIONS` eintragen (idempotentes
  `ALTER TABLE ADD COLUMN`, läuft nach `create_all` in `init_db`).
- Backend-Endpunkte, die `asyncio.create_task` nutzen, müssen `async def` sein.
- **Router mit WebSocket-Endpoints dürfen keine `HTTPBearer`/`HTTP...`-
  Security-Dependencies auf Router-Ebene haben.** FastAPI versucht diese
  für `@router.websocket(...)`-Routen aufzulösen, aber WebSocket-Handshakes
  haben keinen echten `Request` → `TypeError: HTTPBearer.__call__() missing
  1 required positional argument: 'request'` → WebSocket schließt sofort.
  WebSockets authentifizieren sich über `user_from_token(token)` (Query-Param)
  innerhalb der Route; HTTP-Routen im selben Router bekommen
  `Depends(get_current_user)` explizit in der Signatur.

## Tests
`backend/tests/smoke.py` (ohne externe Dienste): Security, Parser, Agent-Runner,
voller Git-Commit/Push-Zyklus gegen lokales Bare-Repo, REST + kompletter Task-Run.

## Offene Punkte / mögliche Next Steps
- **2026-06-13 (Fix):** Session-Paste via Ctrl+V schlug bei Codex mit
  "Failed to paste image: clipboard unavailable: X11 server connection
  timed out" fehl, Claude Code meldete "no image found", Hermes machte
  nichts. Ursache: `SessionTerminalModal.keyToBytes()` schickte Ctrl+V
  als rohes `\x16` an die TUI; die TUIs interpretieren Ctrl+V als
  Image-Paste-Shortcut und lesen das OS-Clipboard via `arboard` —
  scheitert im headless PTY ohne X11/Wayland. Fix: `keyToBytes()` returnt
  für Ctrl+V/Ctrl+Shift+V jetzt `null`; Browser-Default überlebt, das
  `paste`-Event feuert, `onTerminalPaste` schickt den Text als
  Bracketed-Paste (`\x1b[200~ ... \x1b[201~`). Resultat: Pasten aus dem
  Browser funktionieren prompt-treu, ohne dass die TUI ein Bild sucht.
  Erst nach `update.sh`/`systemctl restart coding-dashboard` wirksam.
- **2026-06-13 (Feature):** Session Mode: Bracketed Paste (DEC ?2004h +
  `\x1b[200~ ... \x1b[201~`) aktiviert — mehrzeilige Pasten aus dem
  Browser werden in der TUI nicht mehr als Enter-Submits interpretiert.
  Backend aktiviert den PTY-Modus idempotent beim Start,
  `SessionTerminalModal.onTerminalPaste` umschließt den Text.
  Zusätzlich zeigt die Historie-Header-Zeile in `ProjectDetail` für
  jeden Task Commit-Hash (als Link, falls GitHub-Repo gepflegt) und
  `gepusht ✓` / `nicht gepusht` — gleicher Look wie die
  `SessionTerminalModal`-Footer-Zeile direkt nach `end_session`. Wirksam
  nach `update.sh` / `systemctl restart coding-dashboard`.
- **2026-06-13 (Fix):** Session-WebSocket schlug sofort fehl mit
  `TypeError: HTTPBearer.__call__() missing 1 required positional argument:
  'request'`. Ursache: `routers/sessions.py` definierte
  `dependencies=[Depends(get_current_user)]` auf Router-Ebene — FastAPI
  versucht diese auch für `@router.websocket(...)`-Routen aufzulösen, was
  ohne `Request`-Objekt scheitert. Fix: Router-level `dependencies=`
  entfernt, HTTP-Routen bekommen `Depends(get_current_user)` explizit
  in der Signatur, WebSocket auth'd sich weiterhin manuell via
  `user_from_token(token)`. Erst nach `update.sh`/`systemctl restart
  coding-dashboard` wirksam.
- **2026-06-13 (Fix):** Session Mode zeigte teils nur einen schwarzen Dialog,
  weil der Session-WebSocket direkt nach `POST /sessions` verbinden konnte,
  bevor `SessionManager.start()` seinen Channel registriert hatte. Fix:
  Session-Start wird bis zur PTY-/Channel-Anlage awaited, der WebSocket wartet
  kurz auf gerade startende Channels, Startfehler werden persistiert und das
  Frontend zeigt Verbindungs-/Fehlerzustände statt leerem Schwarz. Erst nach
  `systemctl restart coding-dashboard` wirksam.
- **2026-06-13:** Session Mode überarbeitet: startet Agent-TUIs im Projektordner
  über `session_command` ohne Prompt-Injection; Startparameter-Feld wird mit
  `shlex.split()` als argv angehängt. Dialog in `ProjectDetail` statt Seitenwechsel,
  rohe Tastaturweiterleitung inkl. Pfeiltasten/Ctrl+C/Paste, Resize-Events,
  ANSI/Cursor-Rendering und persistenter `Task.output`-Transcript mit Offset-Replay.
  Nach Session-Ende: Git-Commit falls Änderungen und Push immer. Erst nach
  `systemctl restart coding-dashboard` wirksam.
- **2026-06-12 (Fix):** `_write_codex_config` strippte beim Lesen die
  Anführungszeichen von allen Werten (`strip('"')`), schrieb sie aber nur
  für `model`/`model_reasoning_effort` zurück — `service_tier = "default"`
  wurde so zu `service_tier = default` (ohne Quotes), was Codex blockierte.
  Fix: Raw-Wert ohne Quote-Removal speichern, Formatierung bleibt erhalten.
  Nach `systemctl restart coding-dashboard` wirksam.
- **2026-06-12:** Session Mode PTY-basiert: Agent läuft in echtem PTY, alle
  Tastatureingaben (Pfeiltasten, Ctrl+C, etc.) werden 1:1 durchgereicht.
  `session_command` in AgentSpec (neu), `os.openpty`+`os.fork` statt
  `asyncio.subprocess`, grüner Terminal-Display im Frontend.
  Nach `systemctl restart coding-dashboard` wirksam.
- **2026-06-12:** Modellliste aktualisiert: Claude Code hat jetzt `fable` als viertes Modell;
  Codex verwendet `gpt-5.4`, `gpt-5.5`, `gpt-5.4-mini` (vorher `gpt-5.1-*`).
  Bei Claude Code wird bei gesetztem Effort-Level zusätzlich `~/.claude/settings.json`
  geschrieben (Key: `effort`), damit das Level garantiert genutzt wird — nicht nur
  per `--effort`-Flag in der CLI. Erst nach `systemctl restart coding-dashboard`
  wirksam.
- **2026-06-12 (Fix):** `@vitejs/plugin-react` upgedated von `^4.3.2` auf `^6.0.2` (braucht jetzt
  `@rolldown/plugin-babel@^0.2.3` + `babel-plugin-react-compiler@^1.0.0` als peer deps).
  Fix für npm 10 + vite 8 peer-dep conflict. Build erfolgreich.
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
