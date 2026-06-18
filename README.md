# Coding Dashboard

Ein selbst-gehostetes Dashboard, um Coding-Aufgaben an **Claude Code**,
**Hermes** oder **Codex** zu delegieren – pro Projekt, mit Live-Ausgabe, automatischem
Commit & Push und vollständiger Historie. Erreichbar über **Web** und
**Android-App**.

> **Wichtig:** Dieses Repo enthält Code **und Deployment-Scripts**. Es wird
> **nicht** automatisch deployt. Zwei Wege:
> 1. **systemd** (`deploy/install.sh`) auf einem Ubuntu-Server, auf dem `hermes`,
>    `claude` und `codex` bereits installiert & eingeloggt sind.
> 2. **Docker Compose** – ein in sich geschlossener Container, der die Agent-CLIs
>    selbst mitbringt (siehe [Docker Compose](#docker-compose-alternativ--alles-im-container)).

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

## Installation (Server, systemd)

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

## Docker Compose (alternativ – alles im Container)

Statt des systemd-Installers kann der komplette Stack als **ein Container** laufen:
Backend (uvicorn – liefert auch die SPA **und** die WebSockets aus), die Agent-CLIs
(**Claude**, **Codex** **und Hermes** vorinstalliert) und git – alles drin.
Zustand (SQLite-DB, geklonte Repos), die generierte `config.yaml` sowie die
**Claude-/Codex-Logins** liegen in **named volumes**. **Hermes** nutzt dagegen das
`~/.hermes` des **Hosts** (Bind-Mount), teilt sich also Login/Daten mit einer
Hermes-Installation auf dem Host. Da Bind-Mounts die uid/gid des Hosts behalten,
das Image mit passendem App-User bauen:

```bash
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
```

**Schnellstart** (im Repo-Wurzelverzeichnis, Docker + Compose-Plugin vorausgesetzt):

```bash
cp deploy/docker/coding-dashboard.docker.env.example deploy/docker/coding-dashboard.docker.env
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
# Secrets erzeugen (Image muss gebaut sein) und in die .env eintragen:
docker compose run --rm dashboard python -m app.cli hash-password 'DEIN-PASSWORT'  # -> CD_ADMIN_PASSWORD_HASH
openssl rand -hex 32                                                                # -> CD_SECRET_KEY
# deploy/docker/coding-dashboard.docker.env bearbeiten: Hash, Secret & GitHub-Token eintragen
docker compose up -d
```

**Agenten einloggen** – einmalig; es werden **keine** API-Keys in Dateien abgelegt.
Claude/Codex bleiben im `cd-home`-Volume, Hermes schreibt ins geteilte Host-`~/.hermes`:

```bash
docker compose exec dashboard claude        # im TUI einloggen -> cd-home
docker compose exec dashboard codex login   #   "
docker compose exec dashboard hermes        # schreibt ins Host-~/.hermes (oder auf dem Host einloggen)
```

Den Host-Pfad bei Bedarf via `CD_HERMES_HOST_DIR` umbiegen (Default `~/.hermes`);
das Verzeichnis muss auf dem Host existieren und dem `APP_UID`/`APP_GID`-User gehören.

**Erreichbarkeit:** standardmäßig nur `127.0.0.1:8000`. Für LAN/öffentlich davor
einen TLS-Reverse-Proxy setzen (empfohlen, v.a. für die Android-App) oder die
Bindung ändern:

```bash
CD_BIND_ADDR=0.0.0.0 CD_HOST_PORT=8080 docker compose up -d
```

**Hermes** wird standardmäßig über sein offizielles Install-Script vorinstalliert
(`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`). Da der
Build als root läuft, wählt der Installer das FHS-Layout: der `hermes`-Shim landet
in `/usr/local/bin` (auf dem PATH), der Code in `/usr/local/lib/hermes-agent`. Sein
Daten-/Config-Verzeichnis `~/.hermes` wird (anders als `~/.claude`/`~/.codex`)
nicht im Volume gehalten, sondern als **Bind-Mount vom Host** eingehängt (s.o.) –
Login/Daten sind also mit dem Host geteilt. Zum **Pinnen/Austauschen oder
Deaktivieren**:

```bash
# Hermes weglassen:
HERMES_INSTALL_CMD='' docker compose build
# oder per npm-Paket statt Install-Script:
docker compose build --build-arg HERMES_NPM_PKG=@dein-scope/hermes-cli
docker compose up -d
```

Beim **ersten Start** erkennt der Container automatisch, welche Agent-CLIs vorhanden
sind, und aktiviert nur diese in `config.yaml` (im `cd-config`-Volume). Claude/Codex
lassen sich analog über `--build-arg CLAUDE_NPM_PKG=…` / `CODEX_NPM_PKG=…` pinnen
oder austauschen. Wer einen Agenten nachträglich hinzufügt: `config.yaml` im
`cd-config`-Volume anpassen (oder löschen → wird neu generiert) und neu starten.

**Verwalten / Updaten:**

```bash
docker compose logs -f dashboard
docker compose up -d --build    # Code/Frontend/Agents neu bauen + Neustart (Daten bleiben)
docker compose down             # stoppen; Volumes (Daten/Logins) bleiben erhalten
docker compose down -v          # ACHTUNG: löscht auch die Volumes (DB, Repos, Logins)
```

**„Nichts verlässt den Container":** außer den GitHub-Pushes und den Agent-API-Aufrufen
(Anthropic/OpenAI/…) – beides funktionsbedingt – bleibt der gesamte Zustand in den
Volumes `cd-data` (DB + Repos), `cd-config` (`config.yaml`) und `cd-home` (Agent-Logins).

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
