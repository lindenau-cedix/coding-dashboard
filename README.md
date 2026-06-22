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
and similar settings. **Leave the admin password empty to run without a login**
(no auth screen) — intended for deployments behind an authenticating proxy such
as a Cloudflare tunnel/Access. Setting a password (now or later via
`CD_ADMIN_PASSWORD_HASH`) re-enables the login automatically;
`CD_REQUIRE_AUTH=true|false` is an explicit override. It sets up:

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
backend (uvicorn - also serves the SPA **and** WebSockets), the **Claude** +
**Codex** CLIs, and git - everything inside. State (SQLite DB, cloned repos), the
generated `config.yaml`, and the **Claude/Codex logins** live in **named
volumes**.

**Hermes** is the exception: it does **not** run inside the container at all.
Instead the container `ssh`es into the host and runs the **host's** `hermes`
there (`ssh user@host '… hermes …'`), so there is exactly **one** Hermes process
tree - on the host. This replaces the earlier approach of mirroring the host
`~/.hermes` into the container, which spun up a *second* Hermes and made its
cronjobs and paired channels (WhatsApp, Telegram) fire twice and answer twice.
Now the dashboard just drives the single host Hermes remotely for each task /
session; its login/config/memory/cron/channels stay entirely on the host.

The dashboard's data (DB + cloned repos) stays **private to the container** in the
`cd-data` named volume — the host can't see it. So for each Hermes run the
dashboard **copies the project into a small staging dir** that *is* shared with the
host: a `/tmp` subfolder bind-mounted at an **identical path** on both sides
(`CD_HERMES_STAGING_HOST_DIR`, default `/tmp/coding-dashboard-hermes`). The host's
`hermes` edits that copy over SSH; afterwards the dashboard **merges the result
back** into the repo in `cd-data` and pushes. If the merge conflicts, the Hermes
commit is pushed on its own branch and left for you to merge and then **Pull** — the
dashboard never force-pulls over a conflict. The per-task copy is cleaned up
afterwards; interactive sessions keep one stable per-project copy so Hermes
`--resume` finds the same directory again.

**Host prerequisites** (one time):

1. The host runs **sshd** and has **Hermes installed + logged in** for the user
   that will run it (`CD_HERMES_SSH_USER`).
2. Create an SSH keypair for the container, authorise it for that user, and own
   the shared **staging dir** with that user's uid/gid (so the host's Hermes can
   read/write the staged copy and file ownership matches):

```bash
mkdir -p /tmp/coding-dashboard-hermes                          # staging dir (owned by this user)
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_coding_dashboard      # container -> host key
cat ~/.ssh/id_coding_dashboard.pub >> ~/.ssh/authorized_keys   # let the container in
```

   The dir only ever holds throwaway copies, but it must exist and be **owned by
   that user** before `docker compose up` (Docker would otherwise auto-create the
   bind-mount source as `root`, which the container's `app` user can't write).
   `/tmp` is cleared on reboot, so recreate it on boot (a `tmpfiles.d` entry, or
   just re-run the `mkdir` before `up`) or set `CD_HERMES_STAGING_HOST_DIR` to a
   persistent path you own.

**Quick start** (from the repo root, with Docker + Compose plugin installed):

```bash
cp deploy/docker/coding-dashboard.docker.env.example deploy/docker/coding-dashboard.docker.env
# Build with the app user matching the host user that runs Hermes / owns the data dir:
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
# Generate secrets (image must already be built) and put them into the .env file:
docker compose run --rm dashboard python -m app.cli hash-password 'YOUR-PASSWORD'  # -> CD_ADMIN_PASSWORD_HASH
openssl rand -hex 32                                                               # -> CD_SECRET_KEY
# Edit deploy/docker/coding-dashboard.docker.env: add hash, secret, GitHub token,
#   and set CD_HERMES_SSH_USER (the host user that runs Hermes) to enable Hermes.
docker compose up -d
```

**Agent login** - Claude + Codex log in once (no API keys on disk), persisting in
the `cd-home` volume. Hermes needs no login here - it runs on the host:

```bash
docker compose exec dashboard claude        # log in via TUI -> cd-home
docker compose exec dashboard codex login   #   "
# Verify the container can reach the host's Hermes over SSH:
docker compose exec dashboard ssh -i /home/app/.ssh/id_hermes \
  -o UserKnownHostsFile=/home/app/.ssh_known_hosts -o StrictHostKeyChecking=accept-new \
  "$CD_HERMES_SSH_USER@host.docker.internal" \
  'export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH" && hermes --version'
```

**Reachability:** by default the service listens only on `127.0.0.1:8000`. For LAN
or public access, place a TLS reverse proxy in front of it (recommended, especially
for the Android app) or change the bind address:

```bash
CD_BIND_ADDR=0.0.0.0 CD_HOST_PORT=8080 docker compose up -d
```

**Hermes** runs on the host over SSH - no in-image install by default. Set
`CD_HERMES_SSH_USER` (in the env file) to enable it; the other knobs have
sensible defaults. Build / runtime settings:

| Setting | Where | Meaning | Default |
|---|---|---|---|
| `CD_HERMES_SSH_USER` | env file | Host user that runs Hermes; **empty = Hermes disabled** | *(unset)* |
| `CD_HERMES_SSH_HOST` | env file | Host address reachable from the container | `host.docker.internal` |
| `CD_HERMES_SSH_PORT` | env file | sshd port on the host | `22` |
| `APP_UID` / `APP_GID` | build arg / shell | uid/gid of that host user (owns the shared staging dir; keep stable across rebuilds) | `1000` |
| `CD_HERMES_STAGING_HOST_DIR` | shell | Host path bind-mounted (at the **same** path) where the project is staged for the host's Hermes | `/tmp/coding-dashboard-hermes` |
| `CD_HERMES_SSH_KEY_HOST` | shell | Host path of the private key the container ssh's with | `~/.ssh/id_coding_dashboard` |
| `CD_CODEX_SANDBOX` | shell / compose env | Codex sandbox mode inside Docker; default avoids bubblewrap user-namespace failures | `danger-full-access` |

`host.docker.internal` is mapped to the host gateway via `extra_hosts` in
`docker-compose.yml`; make sure the host's sshd listens on that interface (e.g.
the docker bridge) and any host firewall allows the Compose bridge subnet to
connect to TCP/22. Find the exact source range from the running container:

```bash
NET=$(docker inspect -f '{{range $n, $_ := .NetworkSettings.Networks}}{{println $n}}{{end}}' \
  "$(docker compose ps -q dashboard)" | head -n1)
docker network inspect "$NET" \
  -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
```

Allow that CIDR, for example `172.19.0.0/16`, not the whole Docker private
range unless you intentionally want all Docker networks to reach sshd. If you
need a stable firewall rule, define an explicit Compose network subnet and allow
that fixed CIDR.

Docker defaults Codex to `--sandbox danger-full-access` because Codex's
`workspace-write` sandbox uses bubblewrap/user namespaces, which many Docker
hosts disable for unprivileged container users. Task and session writes are still
contained by the dashboard's per-run git worktrees or host-staging copies. If your
host allows unprivileged user namespaces and you want Codex's inner sandbox too,
run Compose with `CD_CODEX_SANDBOX=workspace-write`.

To instead ship a **self-contained** in-image Hermes (no host coupling; log in
with `docker compose exec dashboard hermes`), leave `CD_HERMES_SSH_USER` empty and
bake the installer in:

```bash
# Self-contained Hermes via its official installer (built as the app user):
docker compose build --build-arg HERMES_INSTALL_CMD='curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash'
# Or via an npm package:
docker compose build --build-arg HERMES_NPM_PKG=@your-scope/hermes-cli
docker compose up -d
```

### Deploy on the dest server

Run as the host user that will run Hermes and own the shared staging dir (so the
build auto-matches its uid), from the repo root. After a `git pull` that changed
`APP_UID`/`APP_GID`, recreate the `cd-home` volume once so it re-seeds with the
correct ownership:

```bash
git pull                                      # get these changes
mkdir -p /tmp/coding-dashboard-hermes         # staging dir, owned by this user
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_coding_dashboard 2>/dev/null || true   # if not already created
cat ~/.ssh/id_coding_dashboard.pub >> ~/.ssh/authorized_keys                    # authorise the container
APP_UID=$(id -u) APP_GID=$(id -g) docker compose build
docker compose down
docker volume ls | grep cd-home               # confirm the exact volume name
docker volume rm coding-dashboard_cd-home     # recreate the orphaned home vol (clears ONLY claude/codex logins)
docker compose up -d
# Set CD_HERMES_SSH_USER in the env file, then verify the host's Hermes is reachable:
docker compose exec dashboard ssh -i /home/app/.ssh/id_hermes \
  -o UserKnownHostsFile=/home/app/.ssh_known_hosts -o StrictHostKeyChecking=accept-new \
  "$CD_HERMES_SSH_USER@host.docker.internal" \
  'export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH" && hermes --version'
```

> **Ran the short-lived host-bind-mount build?** An interim version bind-mounted the
> whole data dir at `/var/lib/coding-dashboard`; data now lives back in the `cd-data`
> named volume. Copy your data in once:
> `docker run --rm -v /var/lib/coding-dashboard:/from -v coding-dashboard_cd-data:/to alpine sh -c 'cp -a /from/. /to/'`.
> If you're coming straight from the original named-volume setup, there's nothing to
> migrate — `cd-data` is reused as-is.

`cd-data` (DB + repos) and `cd-config` (`config.yaml`) are untouched by a rebuild;
you only re-login Claude/Codex (`docker compose exec dashboard claude` /
`codex login`). The volume prefix follows the Compose project name (the repo
dir) — use the name shown by `docker volume ls` if it isn't
`coding-dashboard_cd-home`. **If you change `CD_HERMES_SSH_USER` later**, delete
`config.yaml` in the `cd-config` volume so it regenerates with the new SSH command
(it is written once and never overwritten).

Changes to `CD_CODEX_SANDBOX` are applied when the backend loads the agent config,
so an existing `cd-config` volume does not have to be deleted just to switch Codex
away from Docker's default `danger-full-access` value.

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

**State location:** except GitHub pushes and the agent API calls
(Anthropic/OpenAI/...) - both required by the feature - dashboard state stays in
the volumes `cd-data` (DB + repos), `cd-config` (`config.yaml`) and `cd-home`
(Claude/Codex logins). Hermes is the one cross-boundary piece by design: it runs
on the **host** over SSH, so its login/memory/cron/channels live entirely on the
host; for each run the dashboard copies the project into the shared staging dir
(`/tmp/coding-dashboard-hermes`) for the host's Hermes to edit and merges the
result back into `cd-data`.

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
  `.../coding-dashboard.env` (`chmod 640`). Auth is **off by default** (no
  `CD_ADMIN_PASSWORD_HASH`): the API and UI are open, meant for use behind an
  authenticating proxy (e.g. a Cloudflare tunnel/Access). Set a password hash to
  require login, or force it either way with `CD_REQUIRE_AUTH=true|false`.
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
