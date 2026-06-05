# Coding Dashboard

Ein selbst-gehostetes Dashboard, um Coding-Aufgaben an **Claude Code** oder
**Hermes** zu delegieren – pro Projekt, mit Live-Ausgabe, automatischem
Commit & Push und vollständiger Historie. Erreichbar über **Web** und
**Android-App**.

> **Wichtig:** Dieses Repo enthält Code **und Installations-Scripts**. Es wird
> **nicht** automatisch deployt – du führst `deploy/install.sh` auf deinem
> Ubuntu-Server aus (auf dem `hermes` und `claude` bereits installiert &
> eingeloggt sind).

---

## Was es kann (Anforderungen → Umsetzung)

| Anforderung | Umsetzung |
|---|---|
| Neues Projekt → **privates** GitHub-Repo anlegen | `POST /api/projects` (`mode=create`, `private=true`) via GitHub-API, danach lokaler Clone |
| Bestehendes Repo **importieren** | `mode=import` (`owner/repo` oder URL) |
| Aufgaben an **Hermes oder Claude** stellen, ausführen, Ergebnis anzeigen | Agent-Runner spawnt die CLI, **streamt Output live** per WebSocket |
| Beide pflegen eine **gemeinsame `.md`** für den jeweils anderen Agenten | jeder Task bekommt eine Instruktion angehängt, `AGENTS.md` zu lesen & zu aktualisieren |
| Nach jeder Aufgabe bei Änderungen **ohne Rückfrage pushen** | nach jedem Task: `git add -A` → commit → `git push` (automatisch) |
| **Historie** aus Aufgaben, Ausgabe & Commit | jede Task in SQLite: Prompt, Output, Status, Commit-Hash, Push-Status, Zeiten |
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
            │                          └─ Subprocess: `claude` / `hermes`            │
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
- `claude` (Claude Code) **und** `hermes` installiert **und eingeloggt** für den
  User, unter dem der Service laufen soll (er nutzt deren Credentials in `$HOME`).
- Ein **GitHub Personal Access Token** mit `repo`-Scope
  (fein-granular: *Contents* + *Administration* read/write; für „Repo löschen" zusätzlich *delete*).
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
- `/etc/coding-dashboard/config.yaml` (Agent-Kommandos; `claude`/`hermes`-Pfade automatisch erkannt)
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

**Beide Agenten sind fertig vorkonfiguriert** – Claude über `claude -p … stream-json`,
Hermes über `chat -q` (mit Live-Streaming der Zwischenschritte):

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
```

Warum so bei Hermes:
- **`chat -q`** ist eine einzelne, nicht-interaktive Query, die **Zwischenschritte
  (Tool-Previews) live streamt**; **`AGENTS.md` aus dem CWD** (= Projektverzeichnis)
  wird automatisch injiziert.
- **`--yolo`** überspringt Approvals (autonom), **`--accept-hooks`** läuft headless.
- `NO_COLOR=1` + ANSI-Filter im Backend → saubere Web-Konsole.
- `unset_env` spiegelt den `hermes`-Wrapper, der `PYTHONPATH`/`PYTHONHOME` leert.

Platzhalter: `{prompt}` (Aufgabe inkl. AGENTS.md-Instruktion), `{project_dir}`.
Der absolute `hermes`/`claude`-Pfad wird vom Installer automatisch eingetragen.

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
- Agenten laufen mit `--dangerously-skip-permissions` (Claude) **autonom** und committen/pushen ohne Rückfrage.
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
