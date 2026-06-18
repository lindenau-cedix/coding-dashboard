# Coding Dashboard

A self-hosted dashboard for delegating coding tasks to **Claude Code**,
**Hermes**, or **Codex** per project, with live output, automatic commit & push,
and full history. Available as a **web app** and an **Android app**.

> **Important:** This repository contains both code and deployment scripts. It
> is **not** auto-deployed. There are two supported paths:
> 1. **systemd** (`deploy/install.sh`) on an Ubuntu server where `hermes`,
>    `claude`, and `codex` are already installed and logged in.
> 2. **Docker Compose** - a self-contained container that bundles the agent
>    CLIs itself (see [Docker Compose](#docker-compose-alternative--everything-in-one-container)).

---

## What it does

| Requirement | Implementation |
|---|---|
| Create a new project as a **private** GitHub repo | `POST /api/projects` (`mode=create`, `private=true`) via the GitHub API, then clone locally |
| **Import** an existing repo | `mode=import` (`owner/repo` or URL) |
| Submit tasks to **Hermes, Claude, or Codex**, run them, and show the result | The agent runner spawns the CLI and **streams output live** over WebSocket |
| Agents maintain a shared `.md` file as handoff context | Every task is instructed to read and update `AGENTS.md` |
| After each task, **push changes without asking** | `git add -A` -> commit -> `git push` after every task |
| **Multiple tasks/goals/sessions at once** per project | Each run gets its own **git worktree + branch**, then merges back into the default branch at the end (conflict -> branch kept) |
| Dashboard of all running agents on the home page | `GET /api/running` (across projects), live polling in the frontend |
| **File browser** per project with a side preview | `GET /api/projects/{id}/files` + `/file` (read-only, traversal-safe) |
| **Fullscreen** for live, history, and session output | Portal overlay (`FullscreenShell`) for consoles, terminal, and file viewer |
| Clean **Codex output** | `stream_format: codex` strips timestamps, banners, prompt echo, and token footer |
| **History** from tasks, output, and commits | Every task is stored in SQLite with prompt, output, status, commit hash, push status, merge status, and timestamps |
| Backend as a **systemd service** on Ubuntu | `deploy/coding-dashboard.service` + `install.sh` |
| Available on **web and Android** | React SPA + **Capacitor** APK from the same codebase |
| Web server on the **same machine** | nginx (static + reverse proxy) via `install.sh` |
| Single-user login | Password login -> JWT |

---

## Architecture

```
            ┌─────────────────────────── Ubuntu Server ───────────────────────────┐
 Web ─────► │  nginx :80/:443                                                       │
 Android ─► │    ├─ /            → Static SPA (frontend/dist)                      │
            │    └─ /api, /ws    → uvicorn 127.0.0.1:8000 (systemd: coding-dashboard)│
            │                          │                                            │
            │                          ├─ SQLite  (projects, tasks, history)       │
            │                          ├─ GitHub API (create/import/delete repos)   │
            │                          └─ Subprocess: `claude` / `hermes` / `codex`│
            │                                 └─ in /var/lib/coding-dashboard/projects/<slug>
            └──────────────────────────────────────────────────────────────────────┘
```

- **Backend:** Python / FastAPI (`backend/`)
- **Frontend:** React + Vite + TypeScript + Tailwind (`frontend/`)
- **Android:** Capacitor wrapper around the SPA (real APK)
- **DB:** SQLite (`/var/lib/coding-dashboard/dashboard.db`)
- **Repos:** cloned under `/var/lib/coding-dashboard/projects/`

---

## Prerequisites on the server

- Ubuntu with `sudo`
- `claude` (Claude Code), `hermes`, **and** `codex` installed and logged in for the
  user that runs the service (it uses their credentials in `$HOME`)
- A **GitHub Personal Access Token**:
  - **Classic:** `repo` scope (plus `delete_repo` if you want to delete repos)
  - **Fine-grained:** *Contents* read/write **and** *Administration* read/write with
    repository access set to **"All repositories"** - required to create new repos
    ("Only select repositories" is not enough; *Administration: write* covers create
    and delete). Without it, project creation fails with
    *"Resource not accessible by personal access token"*.
- Optional but recommended: Node.js + npm on the server for the frontend build.
  If npm is missing, build the frontend elsewhere first and copy the resulting `dist`.

---

## Installation (server, systemd)

```bash
# Copy the repo to the server first (git clone / scp / rsync), then:
cd coding-dashboard
sudo ./deploy/install.sh
```

The installer interactively asks for the admin password, GitHub token, domain,
and similar settings. It sets up:

- Code under `/opt/coding-dashboard`
- venv + dependencies, frontend build
- `/etc/coding-dashboard/config.yaml` (agent commands; `claude`/`hermes`/`codex` paths are detected automatically)
- `/etc/coding-dashboard/coding-dashboard.env` (secrets, `chmod 640`)
- systemd service `coding-dashboard` (enabled + started)
- nginx site (optional)

**Non-interactive / automated:**

```bash
sudo NONINTERACTIVE=1 SETUP_NGINX=yes DOMAIN=dash.example.com \
     SERVICE_USER=deploy \
     ADMIN_PASSWORD='…' CD_GITHUB_TOKEN='ghp_…' CD_GITHUB_OWNER='myorg' \
     ./deploy/install.sh
```

**Enable TLS** (strongly recommended, especially for the Android app):

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d dash.example.com
```

**Service management:**

```bash
systemctl status coding-dashboard
journalctl -u coding-dashboard -f
sudo ./deploy/update.sh      # update code/frontend/dependencies + restart
sudo ./deploy/uninstall.sh    # remove
```

---

## Docker Compose alternative - everything in one container

Instead of the systemd installer, the whole stack can run as **one container**:
backend (uvicorn - also serves the SPA **and** WebSockets), the agent CLIs
(**Claude**, **Codex**, and **Hermes** preinstalled), and git - everything inside.
State (SQLite DB, cloned repos), the generated `config.yaml`, and the
**Claude/Codex logins** live in **named volumes**. **Hermes** uses the host's
`~/.hermes` instead (bind mount), so it shares login/data with a host Hermes
installation. Bind mounts keep the host uid/gid; the Docker defaults create
`app` as UID/GID 1000, matching the common first-user setup. If your host
`~/.hermes` has a different owner, build the image with matching ids:

```bash
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
```

**Quick start** (from the repo root, with Docker + Compose plugin installed):

```bash
cp deploy/docker/coding-dashboard.docker.env.example deploy/docker/coding-dashboard.docker.env
docker compose build
# Generate secrets (image must already be built) and put them into the .env file:
docker compose run --rm dashboard python -m app.cli hash-password 'YOUR-PASSWORD'  # -> CD_ADMIN_PASSWORD_HASH
openssl rand -hex 32                                                               # -> CD_SECRET_KEY
# Edit deploy/docker/coding-dashboard.docker.env: add hash, secret, and GitHub token
docker compose up -d
```

**Agent login** - one-time only; no API keys are written to files.
Claude/Codex stay in the `cd-home` volume, Hermes writes to the shared host `~/.hermes`:

```bash
docker compose exec dashboard claude        # log in via TUI -> cd-home
docker compose exec dashboard codex login   #   "
docker compose exec dashboard hermes        # writes to host ~/.hermes (or log in on the host)
```

You can override the host path with `CD_HERMES_HOST_DIR` (default `~/.hermes`);
the directory must exist on the host and belong to the image's `app` UID/GID
(default `1000:1000`, or your `APP_UID`/`APP_GID` override).

**Reachability:** by default the service listens only on `127.0.0.1:8000`. For LAN
or public access, place a TLS reverse proxy in front of it (recommended, especially
for the Android app) or change the bind address:

```bash
CD_BIND_ADDR=0.0.0.0 CD_HOST_PORT=8080 docker compose up -d
```

**Hermes** is preinstalled by default via its official install script
(`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`). The
installer runs as the image's `app` user, so its launcher goes under
`/home/app/.local/bin` (on PATH) and its venv under `/home/app/.hermes`. At runtime
that `~/.hermes` path is mounted from the host (see above), so login/data are shared
with the host and the container `app` UID/GID must match the host owner. To **pin,
replace, or disable** it:

```bash
# Omit Hermes entirely:
HERMES_INSTALL_CMD='' docker compose build
# Or use an npm package instead of the install script:
docker compose build --build-arg HERMES_NPM_PKG=@your-scope/hermes-cli
docker compose up -d
```

On the **first start**, the container automatically detects which agent CLIs are
available and enables only those in `config.yaml` (in the `cd-config` volume).
Claude/Codex can be pinned or replaced the same way via
`--build-arg CLAUDE_NPM_PKG=…` / `CODEX_NPM_PKG=…`. If you add an agent later,
adjust `config.yaml` in the `cd-config` volume (or delete it so it is regenerated)
and restart.

**Manage / update:**

```bash
docker compose logs -f dashboard
docker compose up -d --build    # rebuild code/frontend/agents + restart (data stays)
docker compose down             # stop; volumes (data/logins) remain
docker compose down -v          # WARNING: deletes the volumes too (DB, repos, logins)
```

**"Nothing leaves the container":** except GitHub pushes and the agent API calls
(Anthropic/OpenAI/...) - both required by the feature - everything remains in the
volumes `cd-data` (DB + repos), `cd-config` (`config.yaml`), and `cd-home` (agent
logins).

---

## Agent configuration (`config.yaml`)

**The agents are preconfigured** - Claude via `claude -p ... stream-json`,
Hermes via `chat -q` (with live streaming of intermediate steps), and Codex via
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
    unset_env: ["PYTHONPATH", "PYTHONHOME"]   # do not leak the backend venv into Hermes
  codex:
    command: ["codex", "exec", "--cd", "{project_dir}", "--sandbox", "workspace-write", "--color", "never", "--ephemeral", "--output-last-message", "{last_message_file}", "-"]
    prompt_via: stdin
    stream_format: codex   # removes timestamps, metadata banner, prompt echo, and token footer
    env: { NO_COLOR: "1" }
    unset_env: ["PYTHONPATH", "PYTHONHOME"]
```

Why Hermes is configured this way:
- **`chat -q`** is a single non-interactive query that **streams intermediate steps
  (tool previews) live**; **`AGENTS.md` from the CWD** (the project directory) is
  injected automatically.
- **`--yolo`** skips approvals, **`--accept-hooks`** keeps it headless.
- `NO_COLOR=1` plus ANSI filtering in the backend keeps the web console clean.
- `unset_env` matches the `hermes` wrapper, which clears `PYTHONPATH`/`PYTHONHOME`.

Why Codex is configured this way:
- **`codex exec`** is the non-interactive run for one task.
- **`prompt_via: stdin`** with `-` keeps long dashboard prompts out of the process list.
- **`--sandbox workspace-write`** plus **`--ask-for-approval never`** runs headless
  without disabling the Codex sandbox entirely.
- **`--color never`** plus `NO_COLOR=1` keep the web console readable.

Placeholders: `{prompt}` (task plus AGENTS.md instruction), `{project_dir}`.
The absolute `hermes`/`claude`/`codex` path is filled in automatically by the installer.
Existing installer-generated configs with `claude`/`hermes` get `codex` added on
restart through the backend backfill. If you do not want Codex to appear in the UI,
set a `codex` section with `enabled: false` in `/etc/coding-dashboard/config.yaml`
and restart the service.

For a quieter run (only the **final result**, no live stream):

```yaml
command: ["hermes", "-z", "{prompt}"]
```

After changes: `sudo systemctl restart coding-dashboard`.

---

## Build the Android app

On a machine with Node, JDK 17+ and the Android SDK (`ANDROID_SDK_ROOT` set):

```bash
./deploy/build-android.sh https://dash.example.com
# -> frontend/android/app/build/outputs/apk/debug/app-debug.apk
adb install -r frontend/android/app/build/outputs/apk/debug/app-debug.apk
```

**App icon / logo:** the launcher icon is generated during the build from
`frontend/assets/` (`icon-only.png` = legacy, `icon-foreground.png` +
`icon-background.png` = adaptive icon); `build-android.sh` calls
`@capacitor/assets` for this. Because `frontend/android/` is not checked in and is
regenerated on every build, `frontend/assets/` is the durable source. To change it,
replace the source PNG (master/template: `logo_android.png` in the repo root, see
`frontend/assets/README.md`) and rebuild.

For the build tooling, use a compatible JDK such as 17 or 21. A too-new JDK can
trigger Gradle/AGP errors like `Unsupported class file major version 69`, which
corresponds to Java 25.

The build can optionally prompt for `CF-Access-Client-Id` and
`CF-Access-Client-Secret`, or read them from `CF_ACCESS_CLIENT_ID` and
`CF_ACCESS_CLIENT_SECRET`. In that case, the Cloudflare Access credentials are
baked into the APK and only sent to the backend URL selected at build time.

The public backend URL is baked in (`VITE_API_BASE`); it can also be changed at
runtime in the app under "Server settings" on the login screen. **Use HTTPS** -
Android blocks cleartext unless you enable `cleartext: true` in
`frontend/capacitor.config.ts`.

If the backend is protected by Cloudflare Access, the Access app must allow at
least one `Allow` policy so Cloudflare accepts the `CF_Authorization` cookie for
subsequent requests and WebSockets after the initial service-token request. With
only a `Service Auth` policy, Cloudflare requires the service token for every
request, which browser WebSockets cannot provide via custom headers.

For Android/WebView, Cloudflare Access must also allow `OPTIONS` preflights or
answer them correctly. In Access under Advanced settings > CORS, you can enable
`Bypass OPTIONS requests to origin`. The backend reflects the concrete origin when
`CD_CORS_ORIGINS=*`, so credentialed CORS works with the Capacitor origin
`https://localhost`.

---

## Local development

```bash
# Backend
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../deploy/config.example.yaml ./config.yaml          # agent commands
export CD_SECRET_KEY=dev CD_GITHUB_TOKEN=ghp_...
export CD_ADMIN_USERNAME=admin
export CD_ADMIN_PASSWORD_HASH=$(python -m app.cli hash-password 'dev')
uvicorn app.main:app --reload --port 8000

# Frontend (second terminal) - proxies /api -> :8000
cd frontend
npm install
npm run dev      # http://localhost:5173
```

Tests (no external services; also checks auto-commit/push against a local repo):

```bash
cd backend && .venv/bin/python tests/smoke.py
```

---

## Security

- **Single-user login** (password -> JWT, pbkdf2 hash). Secrets live in
  `.../coding-dashboard.env` (`chmod 640`).
- The GitHub token is **never** written to `.git/config` - it is injected only as
  an auth header per network operation.
- Agents run autonomously (Claude with `--dangerously-skip-permissions`, Hermes
  with `--yolo`, Codex non-interactive) and commit/push without confirmation.
  Use this only with private repos and a dedicated token. Enable TLS.
- The service runs as a normal user, not root - required for Claude's autonomy flag.

---

## Project structure

```
backend/   FastAPI app (app/), requirements.txt, tests/smoke.py
frontend/  React + Vite SPA, capacitor.config.ts
deploy/    install.sh, update.sh, uninstall.sh, build-android.sh,
           coding-dashboard.service, nginx.conf, *.example
```

Details for AI agents and contributors: see [`AGENTS.md`](./AGENTS.md).
