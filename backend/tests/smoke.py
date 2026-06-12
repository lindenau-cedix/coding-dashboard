"""End-to-end smoke test that needs no external services.

Run from anywhere:  .venv/bin/python tests/smoke.py
It validates: password hashing, the Claude stream-json parser, the agent
subprocess runner, the full git auto-commit+push cycle (against a local bare
repo), the REST API (login/auth/agents), and a complete task run through the
TaskManager (agent -> file change -> commit -> push -> history).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- make the `app` package importable and configure env BEFORE imports ----- #
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

TMP = Path(tempfile.mkdtemp(prefix="cd-smoke-"))
CONFIG = TMP / "config.yaml"

os.environ.update(
    CD_SECRET_KEY="test-secret-key",
    CD_ADMIN_USERNAME="admin",
    CD_DATA_DIR=str(TMP / "data"),
    CD_AGENTS_CONFIG_PATH=str(CONFIG),
    CD_FRONTEND_DIST=str(TMP / "no-frontend"),
    CD_GITHUB_TOKEN="",  # local bare remote needs no auth
)

PY = sys.executable
FAKE_SCRIPT = (
    "import pathlib;"
    "print('hello from fake agent');"
    "pathlib.Path('agent_out.txt').write_text('result', encoding='utf-8')"
)

import yaml  # noqa: E402

CONFIG.write_text(
    yaml.safe_dump(
        {
            "context_instruction": "TEST-CONTEXT: pflege AGENTS.md.",
            "agents": {
                "fake": {
                    "display_name": "Fake Agent",
                    "command": [PY, "-c", FAKE_SCRIPT],
                    "prompt_via": "arg",
                    "stream_format": "raw",
                }
            },
        }
    ),
    encoding="utf-8",
)

from app import git_ops  # noqa: E402
from app.agents import _build_command, _ClaudeJSONParser, _final_output, run_agent  # noqa: E402
from app.config import AgentSpec, get_agents_config  # noqa: E402
from app.security import hash_password, verify_password  # noqa: E402

PASSWORD_HASH = hash_password("secret-pw")
os.environ["CD_ADMIN_PASSWORD_HASH"] = PASSWORD_HASH

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=True
    ).stdout.strip()


# --------------------------------------------------------------------------- #
def test_security() -> None:
    check("password roundtrip", verify_password("secret-pw", PASSWORD_HASH))
    check("password wrong rejected", not verify_password("nope", PASSWORD_HASH))


def test_parser() -> None:
    p = _ClaudeJSONParser()
    out = ""
    out += p.feed('{"type":"system","subtype":"init","model":"claude-x"}\n')
    out += p.feed('{"type":"assistant","message":{"content":[{"type":"text","text":"Hallo"}]}}\n')
    out += p.feed('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"}]}}\n')
    out += p.feed(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read",'
        '"input":{"file_path":"/tmp/x.py"}}]}}\n'
    )
    out += p.feed(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
        '"input":{"command":"ls -la"}}]}}\n'
    )
    out += p.feed('{"type":"result","subtype":"success","is_error":false,"result":"Fertig."}\n')
    out += p.feed("not-json-line\n")
    check("parser streams assistant text", "Hallo" in out, out)
    check("parser shows tool use", "[tool] Bash" in out, out)
    check("parser shows file detail", "[tool] Read: /tmp/x.py" in out, out)
    check("parser shows command detail", "[tool] Bash: ls -la" in out, out)
    check("parser captures summary", p.summary() == "Fertig.", p.summary())
    check("parser not error", p.is_error is False)
    check("parser passes through non-json", "not-json-line" in out, out)


def test_command_building() -> None:
    spec = AgentSpec(
        key="c",
        display_name="C",
        command=["claude", "-p", "{prompt}"],
        model_choices=["opus"],
        model_args=["--model", "{model}"],
        effort_choices=["high"],
        effort_args=["--effort", "{effort}"],
    )
    cmd = _build_command(spec, "hi", "/proj", model="opus", effort="high")
    check(
        "model/effort args appended",
        cmd == ["claude", "-p", "hi", "--model", "opus", "--effort", "high"],
        str(cmd),
    )
    cmd0 = _build_command(spec, "hi", "/proj")
    check("no selection -> command unchanged", cmd0 == ["claude", "-p", "hi"], str(cmd0))

    stdin_spec = AgentSpec(
        key="x",
        display_name="X",
        command=["codex", "exec", "-"],
        prompt_via="stdin",
        model_choices=["m1"],
        model_args=["--model", "{model}"],
    )
    cmd2 = _build_command(stdin_spec, "hi", "/proj", model="m1")
    check("stdin marker '-' stays last", cmd2 == ["codex", "exec", "--model", "m1", "-"], str(cmd2))


def test_final_output() -> None:
    raw = (
        "schritt 1: datei lesen\n"
        "tool output blah\n\n"
        "╭──────────────╮\n"
        "│ Alles erledigt: Pull-Button gefixt. │\n"
        "╰──────────────╯\n\n"
        "Resume this session with:\n"
        "  hermes --resume 20260611_abc\n\n"
        "Session:        20260611_abc\n"
        "Duration:       1m 30s\n"
        "Messages:       30 (1 user, 28 tool calls)\n"
    )
    fin = _final_output(raw)
    check("final output keeps last message", "Alles erledigt" in fin, fin)
    check("final output drops earlier steps", "schritt 1" not in fin, fin)
    check(
        "final output strips session footer",
        "Resume" not in fin and "Session" not in fin and "Duration" not in fin,
        fin,
    )
    check("final output strips box chars", "╰" not in fin and "│" not in fin, fin)


def test_agent_runner() -> None:
    import asyncio

    spec = AgentSpec(
        key="fake",
        display_name="Fake",
        command=[PY, "-c", "print('line1'); print('line2')"],
        stream_format="raw",
    )
    chunks: list[str] = []

    async def go():
        return await run_agent(spec, "prompt", str(TMP), lambda c: _collect(chunks, c))

    res = asyncio.run(go())
    joined = "".join(chunks)
    check("agent exit 0", res.exit_code == 0, str(res.exit_code))
    check("agent streamed output", "line1" in joined and "line2" in joined, joined)
    check("agent not error", res.is_error is False)

    stdin_spec = AgentSpec(
        key="stdin",
        display_name="Stdin",
        command=[PY, "-c", "import sys; print(sys.stdin.read())"],
        prompt_via="stdin",
        stream_format="raw",
    )
    chunks = []
    res_stdin = asyncio.run(
        run_agent(stdin_spec, "prompt over stdin", str(TMP), lambda c: _collect(chunks, c))
    )
    check("agent prompt via stdin", "prompt over stdin" in res_stdin.transcript, res_stdin.transcript)

    missing = AgentSpec(key="x", display_name="x", command=["definitely-not-a-binary-xyz"])
    res2 = asyncio.run(run_agent(missing, "p", str(TMP), lambda c: _noop()))
    check("missing binary -> error", res2.is_error and res2.exit_code == 127, str(res2.exit_code))

    # A CLI that writes its final message to {last_message_file} (codex's
    # --output-last-message): the file content must win as result summary.
    last_spec = AgentSpec(
        key="last",
        display_name="Last",
        command=[
            PY,
            "-c",
            "import sys, pathlib;"
            "print('streamed zwischenschritt');"
            "pathlib.Path(sys.argv[1]).write_text('FINALE ANTWORT', encoding='utf-8')",
            "{last_message_file}",
        ],
        stream_format="raw",
    )
    chunks = []
    res3 = asyncio.run(run_agent(last_spec, "p", str(TMP), lambda c: _collect(chunks, c)))
    check("last-message file becomes summary", res3.summary == "FINALE ANTWORT", res3.summary)
    check("last-message run streamed too", "streamed zwischenschritt" in res3.transcript, res3.transcript)


async def _collect(buf: list[str], c: str) -> None:
    buf.append(c)


async def _noop() -> None:
    return None


def test_goal_mode() -> None:
    from app.task_runner import build_agent_prompt

    claude = AgentSpec(
        key="claude",
        display_name="Claude Code",
        command=["claude"],
        goal_command="/goal {prompt}",
    )
    plain = AgentSpec(key="x", display_name="X", command=["x"])  # no goal_command

    ctx = "CTX"
    goal = build_agent_prompt(claude, "ship the feature", "goal", ctx)
    task = build_agent_prompt(claude, "ship the feature", "task", ctx)
    nogoal = build_agent_prompt(plain, "ship the feature", "goal", ctx)

    check("goal mode wraps with /goal", goal.startswith("/goal ship the feature"), goal)
    check("goal mode keeps context", ctx in goal, goal)
    check("task mode leaves prompt as-is", task.startswith("ship the feature"), task)
    check("agent without goal_command falls back", nogoal.startswith("ship the feature"), nogoal)
    check("supports_goal reflects goal_command", bool(claude.goal_command) and not plain.goal_command)


def test_images() -> None:
    """Upload validation + prompt augmentation for task image attachments."""
    from app import uploads
    from app.schemas import TaskImagePayload
    from app.task_runner import build_agent_prompt

    spec = AgentSpec(key="x", display_name="X", command=["x"])
    with_imgs = build_agent_prompt(spec, "fix it", "task", "CTX", image_paths=["/tmp/a.png"])
    check("prompt lists image path", "/tmp/a.png" in with_imgs, with_imgs)
    check("prompt has image instruction", "Angehängte Bilder" in with_imgs, with_imgs)
    without = build_agent_prompt(spec, "fix it", "task", "CTX")
    check("no image block without images", "Angehängte Bilder" not in without, without)

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    decoded = uploads.decode_images(
        [
            TaskImagePayload(name="shot.png", data=f"data:image/png;base64,{png_b64}"),
            TaskImagePayload(name="../evil/shot.png", data=png_b64),
        ]
    )
    check("data-url decoded", decoded[0][0] == "shot.png" and decoded[0][1][:4] == b"\x89PNG")
    check("path components stripped, name deduped", decoded[1][0] == "shot-2.png", decoded[1][0])

    names = uploads.save_images("smoke-task", decoded)
    paths = uploads.image_paths("smoke-task", names)
    check("images saved", len(paths) == 2 and all(Path(p).exists() for p in paths), str(paths))
    uploads.delete_images("smoke-task")
    check("images deleted", not uploads.task_image_dir("smoke-task").exists())

    for bad, why in [
        (TaskImagePayload(name="x.exe", data=png_b64), "bad extension"),
        (TaskImagePayload(name="x.png", data="not-base64!!"), "bad base64"),
    ]:
        try:
            uploads.decode_images([bad])
            check(f"image rejected ({why})", False)
        except uploads.ImageError:
            check(f"image rejected ({why})", True)


def test_config_backfill() -> None:
    """A config.yaml that defines a built-in agent but omits newer optional
    fields (e.g. goal_command) must inherit them from the built-in defaults, so
    existing installer-generated configs gain new features after a restart."""
    from app.config import load_agents_config

    cfg_path = TMP / "backfill.yaml"
    cfg_path.write_text(
        "agents:\n"
        "  claude:\n"
        '    display_name: "Claude Code"\n'
        '    command: ["claude", "-p", "{prompt}"]\n'
        "    stream_format: claude-json\n"
        "    enabled: true\n"
        "  hermes:\n"
        '    display_name: "Hermes"\n'
        '    command: ["hermes", "chat", "-q", "{prompt}"]\n'
        "    stream_format: raw\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_agents_config(cfg_path)
    claude = cfg.agents["claude"]
    codex = cfg.agents["codex"]
    check("backfill: goal_command inherited", claude.goal_command == "/goal {prompt}", claude.goal_command)
    # A YAML command SHORTER than the builtin gets the missing builtin tail
    # appended (so old installer configs keep gaining required flags).
    check(
        "backfill: shorter command completed from builtin",
        claude.command
        == [
            "claude",
            "-p",
            "{prompt}",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ],
        str(claude.command),
    )
    check("backfill: codex added to legacy config", codex.command[:2] == ["codex", "exec"], str(codex.command))
    check("backfill: codex prompt via stdin", codex.prompt_via == "stdin", codex.prompt_via)
    check("backfill: claude model choices", "opus" in claude.model_choices, str(claude.model_choices))
    check("backfill: claude effort args", claude.effort_args == ["--effort", "{effort}"], str(claude.effort_args))
    check(
        "backfill: codex writes last message file",
        "{last_message_file}" in codex.command,
        str(codex.command),
    )

    custom_path = TMP / "custom-only.yaml"
    custom_path.write_text(
        "agents:\n"
        "  fake:\n"
        '    display_name: "Fake"\n'
        f'    command: ["{PY}", "-c", "print(123)"]\n',
        encoding="utf-8",
    )
    custom = load_agents_config(custom_path)
    check("backfill: custom-only config stays explicit", set(custom.agents) == {"fake"}, str(sorted(custom.agents)))


def test_git_cycle() -> None:
    remote = TMP / "remote.git"
    work = TMP / "work"
    run(["git", "init", "--bare", str(remote)])
    git_ops.clone(str(remote), work, token="")
    git_ops.ensure_identity(work, "Tester", "t@example.com")
    (work / "file.txt").write_text("hello", encoding="utf-8")
    check("git detects changes", git_ops.has_changes(work))
    commit = git_ops.commit_all(work, "msg", "Tester", "t@example.com")
    check("git commit returns hash", bool(commit) and len(commit) == 40, str(commit))
    check("git clean after commit", not git_ops.has_changes(work))
    git_ops.push(work, "main", token="")
    remote_head = run(["git", "--git-dir", str(remote), "rev-parse", "main"])
    check("git push reached remote", remote_head == commit, f"{remote_head} vs {commit}")


def test_api_and_task() -> None:
    from fastapi.testclient import TestClient

    from app.database import session_scope
    from app.main import app
    from app.models import Project

    # prepare a project repo with a bare remote
    remote = TMP / "proj-remote.git"
    proj = TMP / "data" / "projects" / "proj"
    run(["git", "init", "--bare", str(remote)])
    git_ops.clone(str(remote), proj, token="")
    git_ops.ensure_identity(proj, "Tester", "t@example.com")
    (proj / "README.md").write_text("# proj\n", encoding="utf-8")
    git_ops.commit_all(proj, "init", "Tester", "t@example.com")
    git_ops.push(proj, "main", token="")
    # seed with an AGENTS.md that has the old "Letzte Tasks" block (pre-2026-06-12)
    (proj / "AGENTS.md").write_text(
        "# AGENTS.md\n\n## Letzter Durchlauf\n\n_Noch kein Durchlauf aufgezeichnet._\n\n## Letzte Tasks\n\n_die letzten 3 laeufe_\n",
        encoding="utf-8",
    )
    git_ops.commit_all(proj, "add AGENTS.md", "Tester", "t@example.com")
    git_ops.push(proj, "main", token="")
    base_head = run(["git", "--git-dir", str(remote), "rev-parse", "main"])

    with TestClient(app) as client:
        check("health", client.get("/api/health").json().get("status") == "ok")

        preflight = client.options(
            "/api/auth/login",
            headers={
                "Origin": "https://localhost",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "content-type,cf-access-client-id,cf-access-client-secret"
                ),
            },
        )
        check("cors preflight -> 200", preflight.status_code == 200, str(preflight.status_code))
        check(
            "cors reflects android origin",
            preflight.headers.get("access-control-allow-origin") == "https://localhost",
            str(preflight.headers),
        )
        check(
            "cors allows credentials",
            preflight.headers.get("access-control-allow-credentials") == "true",
            str(preflight.headers),
        )

        bad = client.post("/api/auth/login", json={"username": "admin", "password": "x"})
        check("login wrong -> 401", bad.status_code == 401, str(bad.status_code))

        ok = client.post("/api/auth/login", json={"username": "admin", "password": "secret-pw"})
        check("login ok -> 200", ok.status_code == 200, str(ok.status_code))
        token = ok.json().get("access_token", "")
        check("login returns token", bool(token))
        H = {"Authorization": f"Bearer {token}"}

        check("agents require auth", client.get("/api/agents").status_code == 401)
        agents = client.get("/api/agents", headers=H).json()
        check("fake agent listed", any(a["key"] == "fake" for a in agents), str(agents))

        check(
            "projects empty",
            client.get("/api/projects", headers=H).json() == [],
        )

        # insert a project row pointing at our local repo
        with session_scope() as db:
            p = Project(
                name="Proj",
                slug="proj",
                local_path=str(proj),
                default_branch="main",
                clone_url=str(remote),
                github_full_name="local/proj",
            )
            db.add(p)
            db.flush()
            pid = p.id

        r = client.post(
            f"/api/projects/{pid}/tasks",
            headers=H,
            json={"agent": "fake", "prompt": "do work"},
        )
        check("task created", r.status_code == 201, str(r.status_code))
        tid = r.json()["id"]

        # poll until terminal
        terminal = {"success", "failed", "error", "interrupted", "cancelled"}
        status = "queued"
        deadline = time.time() + 25
        detail = {}
        while time.time() < deadline:
            detail = client.get(f"/api/tasks/{tid}", headers=H).json()
            status = detail["status"]
            if status in terminal:
                break
            time.sleep(0.4)

        check("task reached success", status == "success", f"status={status} err={detail.get('error')}")
        check("task committed", detail.get("commit_created") is True, str(detail.get("commit_created")))
        check("task pushed", detail.get("pushed") is True, str(detail.get("pushed")))
        check("task has output", "fake agent" in (detail.get("output", "")), detail.get("output", "")[:200])

        new_head = run(["git", "--git-dir", str(remote), "rev-parse", "main"])
        check("push advanced remote", new_head != base_head, f"{new_head} vs {base_head}")
        files = run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "main"])
        check("committed agent file", "agent_out.txt" in files, files)

        # AGENTS.md: agent writes "Letzter Durchlauf" at top via context_instruction;
        # Dashboard strips old "Letzte Tasks" block. Both changes land before push.
        agents_md = (proj / "AGENTS.md").read_text(encoding="utf-8")
        check(
            "AGENTS.md old Letzte Tasks stripped",
            "## Letzte Tasks" not in agents_md,
            agents_md[:300],
        )
        check(
            "AGENTS.md has Letzter Durchlauf",
            "## Letzter Durchlauf" in agents_md,
            agents_md[:300],
        )
        check("AGENTS.md pushed with task", "AGENTS.md" in files, files)

        # model/effort validation: the fake agent offers no choices -> 400.
        bad_model = client.post(
            f"/api/projects/{pid}/tasks",
            headers=H,
            json={"agent": "fake", "prompt": "x", "model": "no-such-model"},
        )
        check("invalid model -> 400", bad_model.status_code == 400, str(bad_model.status_code))

        # Task with an image attachment: stored outside the repo, served via
        # the image endpoint, never committed.
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        r_img = client.post(
            f"/api/projects/{pid}/tasks",
            headers=H,
            json={
                "agent": "fake",
                "prompt": "schau dir das bild an",
                "images": [{"name": "screenshot.png", "data": f"data:image/png;base64,{png_b64}"}],
            },
        )
        check("image task created", r_img.status_code == 201, str(r_img.json()))
        tid_img = r_img.json()["id"]
        check("image listed on task", r_img.json().get("images") == ["screenshot.png"], str(r_img.json().get("images")))
        img_res = client.get(f"/api/tasks/{tid_img}/images/screenshot.png", headers=H)
        check("image served", img_res.status_code == 200 and img_res.content[:4] == b"\x89PNG", str(img_res.status_code))
        check(
            "unknown image -> 404",
            client.get(f"/api/tasks/{tid_img}/images/other.png", headers=H).status_code == 404,
        )
        deadline = time.time() + 25
        while time.time() < deadline:
            d_img = client.get(f"/api/tasks/{tid_img}", headers=H).json()
            if d_img["status"] in terminal:
                break
            time.sleep(0.4)
        check("image task success", d_img["status"] == "success", str(d_img.get("error")))
        files_img = run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "main"])
        check("image NOT committed to repo", "screenshot.png" not in files_img, files_img)

        bad_img = client.post(
            f"/api/projects/{pid}/tasks",
            headers=H,
            json={
                "agent": "fake",
                "prompt": "x",
                "images": [{"name": "evil.exe", "data": png_b64}],
            },
        )
        check("invalid image -> 400", bad_img.status_code == 400, str(bad_img.status_code))

        # Second run: the Letzte-Tasks section is REPLACED (one marker, both
        # entries), not appended twice.
        r2 = client.post(
            f"/api/projects/{pid}/tasks",
            headers=H,
            json={"agent": "fake", "prompt": "zweite aufgabe"},
        )
        check("second task created", r2.status_code == 201, str(r2.status_code))
        tid2 = r2.json()["id"]
        deadline = time.time() + 25
        while time.time() < deadline:
            d2 = client.get(f"/api/tasks/{tid2}", headers=H).json()
            if d2["status"] in terminal:
                break
            time.sleep(0.4)
        check("second task success", d2["status"] == "success", str(d2))
        agents_md2 = (proj / "AGENTS.md").read_text(encoding="utf-8")
        check(
            "old Letzte Tasks still stripped after second run",
            agents_md2.count("## Letzte Tasks") == 0,
            str(agents_md2.count("## Letzte Tasks")),
        )
        check(
            "Letzter Durchlauf present after second run",
            "## Letzter Durchlauf" in agents_md2,
            agents_md2[:300],
        )

        hist = client.get(f"/api/projects/{pid}/tasks", headers=H).json()
        check("history lists task", any(t["id"] == tid for t in hist), str(len(hist)))


def main() -> int:
    try:
        test_security()
        test_parser()
        test_command_building()
        test_final_output()
        test_agent_runner()
        test_goal_mode()
        test_images()
        test_config_backfill()
        test_git_cycle()
        test_api_and_task()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + ("=" * 50))
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
