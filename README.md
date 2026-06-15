# Coding Dashboard

Ein selbst-gehostetes Dashboard, um Coding-Aufgaben an **Claude Code**,
**Hermes** oder **Codex** zu delegieren – pro Projekt, mit Live-Ausgabe, automatischem
Commit & Push und vollständiger Historie. Erreichbar über **Web** und
**Android-App**.

> **Wichtig:** Dieses Repo enthält Code **und Installations-Scripts**. Es wird
> **nicht** automatisch deployt – du führst `deploy/install.sh` auf deinem
> Ubuntu-Server aus (auf dem `hermes`, `claude` und `codex` bereits installiert &
> eingeloggt sind).

---

## Was es kann (Anforderungen → Umsetzung)

| Anforderung | Umsetzung |
|---|---|
| Neues Projekt → **privates** GitHub-Repo anlegen | `POST /api/projects` (`mode=create`, `private=true`) via GitHub-API, danach lokaler Clone |
| Bestehendes Repo **importieren** | `mode=import` (`owner/repo` oder URL) |
| Aufgaben an **Hermes, Claude oder Codex** stellen, ausführen, Ergebnis anzeigen | Agent-Runner spawnt die CLI, **streamt Output live** per WebSocket |
| Agenten pflegen eine **gemeinsame `.md`** als Kontextübergabe | jeder Task bekommt eine Instruktion angehängt, `AGENTS.md` zu lesen & zu aktualisieren |
| Nach jeder Aufgabe bei Änderungen **ohne Rückfrage pushen** | nach jedem Task: `git add -A` → commit → `git push` (automatisch) |
| **Mehrere Tasks/Goals/Sessions gleichzeitig** pro Projekt | jeder Lauf in eigenem **git-Worktree + Branch**, am Ende Merge in den Default-Branch (Konflikt → Branch bleibt erhalten) |
| **Dashboard aller laufenden Agenten** auf der Startseite | `GET /api/running` (projektübergreifend), Live-Polling im Frontend |
| **Dateibrowser** je Projekt mit seitlicher Vorschau | `GET /api/projects/{id}/files` + `/file` (read-only, traversalsicher) |
| **Fullscreen** für Live-, Historie- & Session-Ausgabe | Portal-Overlay (`FullscreenShell`) auf Konsolen, Terminal & Datei-Viewer |
| **Saubere Codex-Ausgabe** | `stream_format: codex` entfernt Timestamps, Banner, Prompt-Echo & Token-Footer |
| **Historie** aus Aufgaben, Ausgabe & Commit | jede Task in SQLite: Prompt, Output, Status, Commit-Hash, Push-Status, Merge-Status, Zeiten |
| Backend als **systemd-Service** (Ubuntu) | `deploy/coding-dashboard.service` + `install.sh` |
| Erreichbar via **Web & Android** | React-SPA + **Capacitor**-APK aus derselben Codebasis |
| Webserver auf **demselben Server** | nginx (Static + Reverse-Proxy) via `install.sh` |
| Single-User-Login | Passwort-Login → JWT |

---

## Architektur

```
            ┌─────────────────────────── Ubuntu Server ───────────────────────────┐
 Web ─────► │  nginx :80/:443                                                       │
 Android ─► │    ├─ /            → Static SPA (frontend/dist)                        │
            │    └─ /api, /ws    → uvicorn 127.0.0.1:8000  (systemd: coding-dashboard)│
            │                          │                                            │
            │                          ├─ SQLite  (Projekte, Tasks, Historie)        │
            │                          ├─ GitHub API (Repo anlegen/importieren)      │
            │                          └─ Subprocess: `claude` / `hermes` / `codex`  │
            │                                 └─ in /var/lib/coding-dashboard/projects/<slug>
            └──────────────────────────────────────────────────────────────────────┘
```

- **Backend:** Python / FastAPI (`backend/`)
- **Frontend:** React + Vite + TypeScript + Tailwind (`frontend/`)
- **Android:** Capacitor-Wrapper um die SPA (echte APK)
- **DB:** SQLite (`/var/lib/coding-dashboard/dashboard.db`)
- **Repos:** geklont unter `/var/lib/coding-dashboard/projects/`

---

## Voraussetzungen auf dem Server

- Ubuntu mit `sudo`
- `claude` (Claude Code), `hermes` **und** `codex` installiert **und eingeloggt** für den
  User, unter dem der Service laufen soll (er nutzt deren Credentials in `$HOME`).
- Ein **GitHub Personal Access Token**:
  - **Klassisch:** `repo`-Scope (zum Löschen von Repos zusätzlich `delete_repo`).
  - **Fein-granular:** *Contents* read/write **und** *Administration* read/write mit
    Repository-Zugriff **„All repositories"** – zum Anlegen neuer Repos zwingend
    („Only select repositories" genügt dafür nicht; *Administration: write* deckt
    Anlegen und Löschen ab). Fehlt das Recht, schlägt „Projekt erstellen" mit
    *„Resource not accessible by personal access token"* fehl.
- Optional, aber empfohlen: Node.js + npm auf dem Server (für den Frontend-Build).
  Fehlt npm, baue das Frontend vorab woanders (siehe unten) – das `dist` wird dann mitkopiert.

---

## Installation (Server)

```bash
# Repo auf den Server bringen (git clone / scp / rsync), dann:
cd coding-dashboard
sudo ./deploy/install.sh
```

Der Installer fragt interaktiv nach Admin-Passwort, GitHub-Token, Domain etc.
und richtet ein:

- Code unter `/opt/coding-dashboard`
- venv + Dependencies, Frontend-Build
- `/etc/coding-dashboard/config.yaml` (Agent-Kommandos; `claude`/`hermes`/`codex`-Pfade automatisch erkannt)
- `/etc/coding-dashboard/coding-dashboard.env` (Secrets, `chmod 640`)
- systemd-Service `coding-dashboard` (enabled + gestartet)
- nginx-Site (optional)

**Nicht-interaktiv / automatisiert:**

```bash
sudo NONINTERACTIVE=1 SETUP_NGINX=yes DOMAIN=dash.example.com \
     SERVICE_USER=deploy \
     ADMIN_PASSWORD='…' CD_GITHUB_TOKEN='ghp_…' CD_GITHUB_OWNER='myorg' \
     ./deploy/install.sh
```

**TLS aktivieren** (dringend empfohlen, v.a. für die Android-App):

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d dash.example.com
```

**Service verwalten:**

```bash
systemctl status coding-dashboard
journalctl -u coding-dashboard -f
sudo ./deploy/update.sh      # Code/Frontend/Deps aktualisieren + Neustart
sudo ./deploy/uninstall.sh   # entfernen
```

---

## ⚙️ Agent-Konfiguration (`config.yaml`)

**Die Agenten sind fertig vorkonfiguriert** – Claude über `claude -p … stream-json`,
Hermes über `chat -q` (mit Live-Streaming der Zwischenschritte) und Codex über
`codex exec`:

```yaml
agents:
  claude:
    command: ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    stream_format: claude-json
  hermes:
    command: ["hermes", "chat", "-q", "{prompt}", "--yolo", "--accept-hooks"]
    stream_format: raw
    env: { HERMES_ACCEPT_HOOKS: "1", NO_COLOR: "1" }
    unset_env: ["PYTHONPATH", "PYTHONHOME"]   # Backend-venv nicht in Hermes leaken
  codex:
    command: ["codex", "exec", "--cd", "{project_dir}", "--sandbox", "workspace-write", "--color", "never", "--ephemeral", "--output-last-message", "{last_message_file}", "-"]
    prompt_via: stdin
    stream_format: codex   # entfernt Timestamps, Metadaten-Banner, Prompt-Echo & Token-Footer
    env: { NO_COLOR: "1" }
    unset_env: ["PYTHONPATH", "PYTHONHOME"]
```

Warum so bei Hermes:
- **`chat -q`** ist eine einzelne, nicht-interaktive Query, die **Zwischenschritte
  (Tool-Previews) live streamt**; **`AGENTS.md` aus dem CWD** (= Projektverzeichnis)
  wird automatisch injiziert.
- **`--yolo`** überspringt Approvals (autonom), **`--accept-hooks`** läuft headless.
- `NO_COLOR=1` + ANSI-Filter im Backend → saubere Web-Konsole.
- `unset_env` spiegelt den `hermes`-Wrapper, der `PYTHONPATH`/`PYTHONHOME` leert.

Warum so bei Codex:
- **`codex exec`** ist der nicht-interaktive Lauf für eine einzelne Aufgabe.
- **`prompt_via: stdin`** mit `-` hält lange Dashboard-Prompts aus der Prozessliste.
- **`--sandbox workspace-write`** + **`--ask-for-approval never`** läuft headless,
  ohne die Codex-Sandbox komplett zu deaktivieren.
- **`--color never`** + `NO_COLOR=1` halten die Web-Konsole lesbar.

Platzhalter: `{prompt}` (Aufgabe inkl. AGENTS.md-Instruktion), `{project_dir}`.
Der absolute `hermes`/`claude`/`codex`-Pfad wird vom Installer automatisch eingetragen.
Bestehende installer-generierte Configs mit `claude`/`hermes` bekommen `codex`
beim Neustart automatisch über den Backend-Backfill dazu. Wenn Codex nicht im UI
auftauchen soll, in `/etc/coding-dashboard/config.yaml` eine `codex`-Sektion mit
`enabled: false` setzen und den Service neu starten.

Leiser (nur **Endergebnis**, kein Live-Stream): `command: ["hermes", "-z", "{prompt}"]`.
Nach Änderungen: `sudo systemctl restart coding-dashboard`.

---

## Android-App bauen

Auf einer Maschine mit Node, JDK 17+ und Android SDK (`ANDROID_SDK_ROOT` gesetzt):

```bash
./deploy/build-android.sh https://dash.example.com
# → frontend/android/app/build/outputs/apk/debug/app-debug.apk
adb install -r frontend/android/app/build/outputs/apk/debug/app-debug.apk
```

**App-Icon/Logo:** Das Launcher-Icon wird beim Build aus `frontend/assets/`
erzeugt (`icon-only.png` = Legacy, `icon-foreground.png` + `icon-background.png` =
Adaptive Icon); `build-android.sh` ruft dafür `@capacitor/assets` auf. Weil
`frontend/android/` nicht eingecheckt ist und bei jedem Build neu entsteht, ist
`frontend/assets/` die dauerhafte Quelle. Zum Ändern das Quell-PNG ersetzen
(Vorlage/Master: `logo_android.png` im Repo-Wurzelverzeichnis, siehe
`frontend/assets/README.md`) und neu bauen.

Wichtig fuer das Build-Tooling: Verwende derzeit ein kompatibles JDK wie 17 oder
21. Ein zu neues JDK kann mit Gradle/AGP in Fehler wie `Unsupported class file
major version 69` laufen; das entspricht Java 25.

Optional kann der Build interaktiv nach `CF-Access-Client-Id` und
`CF-Access-Client-Secret` fragen oder diese aus `CF_ACCESS_CLIENT_ID` und
`CF_ACCESS_CLIENT_SECRET` übernehmen. Dann werden die Cloudflare-Access-Credentials
fest in die APK eingebaut und nur an genau die beim Build gesetzte Backend-URL
gesendet.

Die oeffentliche Backend-URL wird fest eingebaut (`VITE_API_BASE`); sie lässt sich
in der App unter „Server-Einstellungen" auf dem Login-Screen auch zur Laufzeit
ändern. **Nutze HTTPS** – Android blockt sonst Klartext (sonst `cleartext: true`
in `frontend/capacitor.config.ts`).

Wenn das Backend per Cloudflare Access geschuetzt ist, muss die Access-App
mindestens eine `Allow`-Policy besitzen, damit Cloudflare nach dem initialen
Service-Token-Request eine `CF_Authorization`-Cookie fuer Folge-Requests und
WebSockets akzeptiert. Bei einer reinen `Service Auth`-Policy verlangt
Cloudflare das Service-Token laut Doku bei jedem Request erneut, was Browser-
WebSockets nicht per Custom-Header leisten koennen.

Fuer Android/WebView muss Cloudflare Access ausserdem `OPTIONS`-Preflights
durchlassen oder selbst korrekt beantworten. In Access unter Advanced settings >
CORS kann `Bypass OPTIONS requests to origin` aktiviert werden. Das Backend
spiegelt bei `CD_CORS_ORIGINS=*` die konkrete Origin, damit credentialed CORS
mit der Capacitor-Origin `https://localhost` funktioniert.

---

## Lokale Entwicklung

```bash
# Backend
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../deploy/config.example.yaml ./config.yaml          # Agent-Kommandos
export CD_SECRET_KEY=dev CD_GITHUB_TOKEN=ghp_… 
export CD_ADMIN_USERNAME=admin
export CD_ADMIN_PASSWORD_HASH=$(python -m app.cli hash-password 'dev')
uvicorn app.main:app --reload --port 8000

# Frontend (zweites Terminal) – proxyt /api → :8000
cd frontend
npm install
npm run dev      # http://localhost:5173
```

Tests (ohne externe Dienste, prüft auch Auto-Commit/Push gegen ein lokales Repo):

```bash
cd backend && .venv/bin/python tests/smoke.py
```

---

## Sicherheit

- **Single-User-Login** (Passwort → JWT, pbkdf2-Hash). Secrets in `…/coding-dashboard.env` (`chmod 640`).
- GitHub-Token wird **nie** in `.git/config` geschrieben – nur pro Netzwerk-Operation als Auth-Header injiziert.
- Agenten laufen autonom (Claude mit `--dangerously-skip-permissions`, Hermes mit
  `--yolo`, Codex mit `--ask-for-approval never`) und committen/pushen ohne Rückfrage.
  Betreibe das nur mit privaten Repos und einem dafür vorgesehenen Token. Aktiviere TLS.
- Der Service läuft als normaler User (nicht root) – Voraussetzung für Claudes Autonomie-Flag.

---

## Projektstruktur

```
backend/   FastAPI-App (app/), requirements.txt, tests/smoke.py
frontend/  React+Vite SPA, capacitor.config.ts
deploy/    install.sh, update.sh, uninstall.sh, build-android.sh,
           coding-dashboard.service, nginx.conf, *.example
```

Details für KI-Agenten/Mitwirkende: siehe [`AGENTS.md`](./AGENTS.md).
