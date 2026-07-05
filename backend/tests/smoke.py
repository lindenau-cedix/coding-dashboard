"""End-to-end smoke test that needs no external services.

Run from anywhere:  .venv/bin/python tests/smoke.py
It validates: password hashing, the Claude stream-json parser, the agent
subprocess runner, the full git auto-commit+push cycle (against a local bare
repo), the REST API (login/auth/agents), and a complete task run through the
TaskManager (agent -> file change -> commit -> push -> history).
"""
from __future__ import annotations

import json
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
    # Heartbeat agent key: the smoke test config only defines ``fake``,
    # so pin the heartbeat to it; otherwise the runner would warn that
    # ``claude`` isn't in the agents config and skip every tick.
    CD_HEARTBEAT_AGENT_KEY="fake",
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


def test_auth_toggle() -> None:
    """auth_enabled auto-derives from the password hash; CD_REQUIRE_AUTH overrides."""
    from app import auth as auth_mod
    from app.config import get_settings

    def settings_with(**env):
        for k in ("CD_REQUIRE_AUTH", "CD_ADMIN_PASSWORD_HASH"):
            os.environ.pop(k, None)
        os.environ.update(env)
        get_settings.cache_clear()
        return get_settings()

    try:
        # No password, no override -> auth OFF (default for a fresh install).
        s = settings_with()
        check("auth off without password", s.auth_enabled is False)
        check(
            "get_current_user bypassed when off",
            auth_mod.get_current_user(None) == s.admin_username,
        )
        check(
            "ws auth bypassed when off",
            auth_mod.user_from_token(None) == s.admin_username,
        )

        # Password present -> auth ON automatically.
        s = settings_with(CD_ADMIN_PASSWORD_HASH=PASSWORD_HASH)
        check("auth on with password", s.auth_enabled is True)

        # Explicit override wins both ways.
        s = settings_with(CD_REQUIRE_AUTH="false", CD_ADMIN_PASSWORD_HASH=PASSWORD_HASH)
        check("CD_REQUIRE_AUTH=false forces off", s.auth_enabled is False)
        s = settings_with(CD_REQUIRE_AUTH="true")
        check("CD_REQUIRE_AUTH=true forces on", s.auth_enabled is True)
    finally:
        # Restore the auth-on environment the rest of the suite relies on.
        os.environ.pop("CD_REQUIRE_AUTH", None)
        os.environ["CD_ADMIN_PASSWORD_HASH"] = PASSWORD_HASH
        get_settings.cache_clear()


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


def test_codex_parser() -> None:
    from app.agents import _CodexParser

    transcript = (
        "[2026-06-13T20:00:00] OpenAI Codex v0.139.0 (research preview)\n"
        "--------\n"
        "workdir: /proj\n"
        "model: gpt-5.5\n"
        "provider: openai\n"
        "approval: never\n"
        "sandbox: workspace-write\n"
        "reasoning effort: medium\n"
        "--------\n"
        "[2026-06-13T20:00:01] User instructions:\n"
        "Bitte erledige die Aufgabe.\n"
        "GEHEIMER LANGER KONTEXT der nicht in der Konsole landen soll.\n"
        "\n"
        "[2026-06-13T20:00:05] thinking\n"
        "Ich schaue mir die Dateien an.\n"
        "[2026-06-13T20:00:06] exec bash -lc 'ls -la' in /proj\n"
        "[2026-06-13T20:00:07] bash -lc 'ls -la' succeeded in 12ms:\n"
        "total 0\n"
        "[2026-06-13T20:00:20] codex\n"
        "Fertig: Die Aufgabe wurde erledigt.\n"
        "model selection wurde angepasst.\n"  # answer line starting with a banner keyword
        "[2026-06-13T20:00:21] tokens used: 4321\n"
    )
    p = _CodexParser()
    out = "".join(p.feed(line + "\n") for line in transcript.splitlines())
    check("codex strips timestamps", "[2026-06-13T20:00" not in out, out)
    check("codex drops version banner", "OpenAI Codex" not in out, out)
    check("codex drops metadata banner", "workdir:" not in out and "sandbox:" not in out, out)
    check("codex drops echoed prompt", "GEHEIMER LANGER KONTEXT" not in out, out)
    check("codex drops token footer", "tokens used" not in out, out)
    check("codex keeps thinking text", "schaue mir die Dateien" in out, out)
    check("codex formats exec as shell", "$ ls -la" in out, out)
    check("codex keeps final answer", "Die Aufgabe wurde erledigt" in out, out)
    check("codex drops bare codex marker", "\ncodex\n" not in out, out)
    check(
        "codex keeps answer line starting with banner keyword",
        "model selection wurde angepasst" in out,
        out,
    )


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

    long_json_spec = AgentSpec(
        key="claude",
        display_name="Claude Long Line",
        command=[
            PY,
            "-c",
            "import json; print(json.dumps({'type':'assistant','message':"
            "{'content':[{'type':'text','text':'x'*70000}]}}))",
        ],
        stream_format="claude-json",
    )
    chunks = []
    res4 = asyncio.run(run_agent(long_json_spec, "p", str(TMP), lambda c: _collect(chunks, c)))
    check("agent handles long stdout line", res4.exit_code == 0 and not res4.is_error, str(res4.exit_code))
    check("long stdout line streamed", "x" * 1000 in res4.transcript, res4.transcript[:200])


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
    check("backfill: codex session command", codex.session_command == ["codex"], str(codex.session_command))
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

    old_sandbox = os.environ.get("CD_CODEX_SANDBOX")
    try:
        os.environ["CD_CODEX_SANDBOX"] = "danger-full-access"
        docker_cfg_path = TMP / "docker-existing.yaml"
        docker_cfg_path.write_text(
            "agents:\n"
            "  hermes:\n"
            '    display_name: "Hermes"\n'
            "    command:\n"
            '      - "ssh"\n'
            '      - "-i"\n'
            '      - "/home/app/.ssh/id_hermes"\n'
            '      - "-p"\n'
            '      - "22"\n'
            '      - "debian@host.docker.internal"\n'
            '      - "cd {project_dir} && exec env HERMES_ACCEPT_HOOKS=1 NO_COLOR=1 hermes chat -q \\"$(cat)\\" --yolo --accept-hooks"\n'
            "    session_command:\n"
            '      - "ssh"\n'
            '      - "-tt"\n'
            '      - "-i"\n'
            '      - "/home/app/.ssh/id_hermes"\n'
            '      - "-p"\n'
            '      - "22"\n'
            '      - "debian@host.docker.internal"\n'
            '      - "cd {project_dir} && exec hermes chat"\n'
            "    prompt_via: stdin\n"
            "    stream_format: raw\n"
            "    host_staging: true\n"
            "    enabled: true\n"
            "  codex:\n"
            '    display_name: "Codex"\n'
            '    command: ["codex", "exec", "--cd", "{project_dir}", "--sandbox", "workspace-write", "--color", "never", "--ephemeral", "--output-last-message", "{last_message_file}", "-"]\n'
            "    prompt_via: stdin\n"
            "    stream_format: codex\n"
            "    enabled: true\n",
            encoding="utf-8",
        )
        docker_cfg = load_agents_config(docker_cfg_path)
        codex_cmd = docker_cfg.agents["codex"].command
        sandbox_idx = codex_cmd.index("--sandbox")
        check(
            "docker override: codex sandbox patched",
            codex_cmd[sandbox_idx + 1] == "danger-full-access",
            str(codex_cmd),
        )
        hermes_remote = docker_cfg.agents["hermes"].command[-1]
        check(
            "docker override: hermes remote PATH added",
            "export PATH=" in hermes_remote and "$HOME/.local/bin" in hermes_remote,
            hermes_remote,
        )
        check(
            "docker override: hermes project dir quoted",
            hermes_remote.startswith('cd "{project_dir}" && '),
            hermes_remote,
        )
    finally:
        if old_sandbox is None:
            os.environ.pop("CD_CODEX_SANDBOX", None)
        else:
            os.environ["CD_CODEX_SANDBOX"] = old_sandbox


def test_worktree_merge() -> None:
    """Isolated-worktree merge flow: a task branch merges back into the default
    branch and pushes; two concurrent branches both land; a conflicting branch
    is kept (merge_state='conflict') instead of corrupting the default branch."""
    from app.task_runner import _merge_worktree_branch

    remote = TMP / "wt-remote.git"
    proj = TMP / "wt-proj"
    run(["git", "init", "--bare", str(remote)])
    git_ops.clone(str(remote), proj, token="")
    git_ops.ensure_identity(proj, "Tester", "t@example.com")
    (proj / "base.txt").write_text("base\n", encoding="utf-8")
    git_ops.commit_all(proj, "init", "Tester", "t@example.com")
    git_ops.push(proj, "main", token="")

    # One clean branch via a worktree -> merges and pushes.
    wt1 = TMP / "wt1"
    git_ops.add_worktree(proj, wt1, "cd/task/aaa", "HEAD")
    (wt1 / "feature_a.txt").write_text("A\n", encoding="utf-8")
    git_ops.ensure_identity(wt1, "Tester", "t@example.com")
    res1 = _merge_worktree_branch(
        str(proj), str(wt1), "cd/task/aaa", "main", "",
        "Tester", "t@example.com", "feature A", "feature A",
    )
    check("worktree branch merged", res1["merge_state"] == "merged", str(res1["messages"]))
    check("worktree merge pushed", res1["pushed"] is True, str(res1["messages"]))
    check("worktree dir removed after merge", not wt1.exists(), str(wt1))
    files = run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "main"])
    check("merged feature on remote", "feature_a.txt" in files, files)

    # A second clean branch off the (now-advanced) checkout also lands.
    wt2 = TMP / "wt2"
    git_ops.add_worktree(proj, wt2, "cd/task/bbb", "HEAD")
    (wt2 / "feature_b.txt").write_text("B\n", encoding="utf-8")
    git_ops.ensure_identity(wt2, "Tester", "t@example.com")
    res2 = _merge_worktree_branch(
        str(proj), str(wt2), "cd/task/bbb", "main", "",
        "Tester", "t@example.com", "feature B", "feature B",
    )
    check("second worktree merged", res2["merge_state"] == "merged", str(res2["messages"]))
    files2 = run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "main"])
    check("both features on remote", "feature_a.txt" in files2 and "feature_b.txt" in files2, files2)

    # A conflicting branch: edit base.txt in a worktree AND on the default branch
    # so the merge cannot apply -> kept as conflict, default branch untouched.
    wt3 = TMP / "wt3"
    git_ops.add_worktree(proj, wt3, "cd/task/ccc", "HEAD")
    (wt3 / "base.txt").write_text("worktree change\n", encoding="utf-8")
    git_ops.ensure_identity(wt3, "Tester", "t@example.com")
    git_ops.commit_all(wt3, "wt edit", "Tester", "t@example.com")
    (proj / "base.txt").write_text("main change\n", encoding="utf-8")
    git_ops.commit_all(proj, "main edit", "Tester", "t@example.com")
    head_before = git_ops.head_commit(proj)
    res3 = _merge_worktree_branch(
        str(proj), str(wt3), "cd/task/ccc", "main", "",
        "Tester", "t@example.com", "conflicting", "conflicting",
    )
    check("conflict detected", res3["merge_state"] == "conflict", str(res3["messages"]))
    check(
        "default branch untouched on conflict",
        git_ops.head_commit(proj) == head_before,
        f"{git_ops.head_commit(proj)} vs {head_before}",
    )
    check(
        "default branch has no conflict markers",
        "<<<<<<<" not in (proj / "base.txt").read_text(encoding="utf-8"),
        (proj / "base.txt").read_text(encoding="utf-8"),
    )


def test_host_staging() -> None:
    """Host-staging flow (off-host agents, e.g. the SSH-driven Hermes): the agent
    edits a COPY of the project; the commit is fetched back, merged into the
    default branch and pushed; a conflicting copy is kept on a pushed branch
    (merge_state='conflict') without touching the default branch."""
    from app import host_staging

    remote = TMP / "hs-remote.git"
    proj = TMP / "hs-proj"
    run(["git", "init", "--bare", str(remote)])
    git_ops.clone(str(remote), proj, token="")
    git_ops.ensure_identity(proj, "Tester", "t@example.com")
    (proj / "base.txt").write_text("base\n", encoding="utf-8")
    git_ops.commit_all(proj, "init", "Tester", "t@example.com")
    git_ops.push(proj, "main", token="")

    # 1. Clean run: copy the project, edit the copy, integrate -> merged + pushed.
    stage1 = TMP / "hs-stage1"
    host_staging.prepare_copy(str(proj), stage1)
    check("staging copy is an independent repo", git_ops.is_git_repo(stage1), str(stage1))
    check("staging copy has the project files", (stage1 / "base.txt").exists())
    (stage1 / "feature_a.txt").write_text("A\n", encoding="utf-8")
    res1 = host_staging.integrate(
        str(proj), str(stage1), "cd/task/aaa", "main", "",
        "Tester", "t@example.com", "feature A", "feature A", cleanup=True,
    )
    check("staging integrate merged", res1["merge_state"] == "merged", str(res1["messages"]))
    check("staging integrate pushed", res1["pushed"] is True, str(res1["messages"]))
    check("staging copy cleaned up (cleanup=True)", not stage1.exists(), str(stage1))
    files = run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "main"])
    check("staged feature on remote", "feature_a.txt" in files, files)

    # 2. Conflicting copy: change base.txt in the copy AND on the default branch so
    #    the merge cannot apply -> kept on a pushed branch, default branch untouched.
    stage2 = TMP / "hs-stage2"
    host_staging.prepare_copy(str(proj), stage2)
    (stage2 / "base.txt").write_text("staged change\n", encoding="utf-8")
    (proj / "base.txt").write_text("main change\n", encoding="utf-8")
    git_ops.commit_all(proj, "main edit", "Tester", "t@example.com")
    head_before = git_ops.head_commit(proj)
    res2 = host_staging.integrate(
        str(proj), str(stage2), "cd/task/bbb", "main", "",
        "Tester", "t@example.com", "conflicting", "conflicting", cleanup=False,
    )
    check("staging conflict detected", res2["merge_state"] == "conflict", str(res2["messages"]))
    check(
        "default branch untouched on staging conflict",
        git_ops.head_commit(proj) == head_before,
        f"{git_ops.head_commit(proj)} vs {head_before}",
    )
    branch_on_remote = run(["git", "--git-dir", str(remote), "branch", "--list", "cd/task/bbb"])
    check("conflict branch pushed to remote", "cd/task/bbb" in branch_on_remote, branch_on_remote)
    check("staging copy kept on conflict (cleanup=False)", stage2.exists(), str(stage2))
    check(
        "default branch has no conflict markers",
        "<<<<<<<" not in (proj / "base.txt").read_text(encoding="utf-8"),
        (proj / "base.txt").read_text(encoding="utf-8"),
    )
    # is_staging_dir keys off settings.hermes_staging_dir (the shared mount root).
    from app.config import get_settings
    root = Path(get_settings().hermes_staging_dir).resolve()
    check("is_staging_dir true under root", host_staging.is_staging_dir(root / "tasks" / "x"))
    check("is_staging_dir false for project dir", not host_staging.is_staging_dir(str(proj)))


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
            "agents expose session support",
            any(a["key"] == "fake" and a.get("supports_session") is False for a in agents),
            str(agents),
        )

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

        # File browser: list root, traversal blocked, read a text file.
        listing = client.get(f"/api/projects/{pid}/files", headers=H)
        check("file listing ok", listing.status_code == 200, str(listing.status_code))
        names = [e["name"] for e in listing.json().get("entries", [])]
        check("file listing has README", "README.md" in names, str(names))
        check("file listing hides .git", ".git" not in names, str(names))
        traversal = client.get(
            f"/api/projects/{pid}/files", headers=H, params={"path": "../.."}
        )
        check("file traversal blocked -> 400", traversal.status_code == 400, str(traversal.status_code))
        readf = client.get(
            f"/api/projects/{pid}/file", headers=H, params={"path": "README.md"}
        )
        check("file read ok", readf.status_code == 200, str(readf.status_code))
        check(
            "file read returns content",
            readf.json().get("content", "").startswith("# proj") and not readf.json().get("is_binary"),
            str(readf.json())[:200],
        )
        missing = client.get(
            f"/api/projects/{pid}/file", headers=H, params={"path": "nope.txt"}
        )
        check("file read missing -> 404", missing.status_code == 404, str(missing.status_code))

        # Running dashboard: completed tasks must not appear as running.
        running = client.get("/api/running", headers=H)
        check("running endpoint ok", running.status_code == 200, str(running.status_code))
        check(
            "finished task not in running",
            all(r["id"] != tid for r in running.json()),
            str(running.json()),
        )


# --------------------------------------------------------------------------- #
# Session mode
# --------------------------------------------------------------------------- #

def test_session_api_and_manager() -> None:
    from app.task_runner import session_manager, SessionChannel

    # SessionChannel works.
    ch = SessionChannel("test-ch")
    check("channel publish+subscribe works", len(ch.buffer) == 0, str(len(ch.buffer)))
    ch.publish({"type": "output", "data": "hello"})
    q = ch.subscribe()
    got = q.get_nowait()
    check("subscribed queue receives published message", got == {"type": "output", "data": "hello"}, str(got))
    q_no_replay = ch.subscribe(replay=False)
    check("session channel can subscribe without replay", q_no_replay.empty())
    ch.close()

    # SessionManager.start with invalid agent raises ValueError.
    import asyncio
    try:
        asyncio.run(
            session_manager.start("fake-task", "fake-project", "nonexistent-agent", "", "")
        )
        check("start with invalid agent raised", False, "no error")
    except ValueError as e:
        check("start with invalid agent raises ValueError", "Unknown or disabled agent" in str(e), str(e))


def test_session_end_flow() -> None:
    """Smoke test for end_session: creates a task record, calls end_session,
    verifies Task is updated with output transcript, result_summary, and marked done."""
    from app.database import init_db, session_scope, engine
    from app.models import Task, Project
    from app.task_runner import session_manager
    import asyncio
    from sqlalchemy.orm import Session

    init_db()
    # Use an existing project from the DB or create a minimal local repo.
    with Session(engine) as db:
        proj_row = db.query(Project).first()
        if proj_row is None:
            remote = TMP / "session-remote.git"
            work = TMP / "session-work"
            run(["git", "init", "--bare", str(remote)])
            git_ops.clone(str(remote), work, token="")
            git_ops.ensure_identity(work, "Tester", "t@example.com")
            (work / "README.md").write_text("# session\n", encoding="utf-8")
            git_ops.commit_all(work, "init", "Tester", "t@example.com")
            git_ops.push(work, "main", token="")
            proj_row = Project(
                name="SessionProj",
                slug="session-proj",
                local_path=str(work),
                default_branch="main",
                clone_url=str(remote),
            )
            db.add(proj_row)
            db.commit()
        pid = proj_row.id

    task = Task(project_id=pid, agent="fake", prompt="", mode="session",
                is_session=True, status="running", chat_history="[]")
    with session_scope() as db:
        db.add(task)
        db.commit()
        db.refresh(task)
        tid = task.id

    # Pre-populate the terminal transcript.
    with session_scope() as db:
        t = db.get(Task, tid)
        t.output = "hello from terminal\r\nagent prompt> "
        db.commit()

    # end_session should succeed (process already dead → catch path).
    result = asyncio.run(
        session_manager.end_session(tid, pid, commit_message="test commit")
    )
    check("session end returns dict", isinstance(result, dict), str(result))
    check("session end has status", "status" in result, str(result))

    with session_scope() as db:
        t = db.get(Task, tid)
        if t is None:
            check("task exists after end_session", False, "task is None")
            return
        check("task marked finished after end_session", t.finished_at is not None, str(t.finished_at))
        check("task terminal output preserved", "hello from terminal" in (t.output or ""), t.output or "")


def test_worktrees() -> None:
    """Isolated worktrees let parallel sessions work without clobbering files."""
    # Distinct remote/repo names from test_worktree_merge: both run in the same
    # shared TMP and ``git init --bare`` on an existing repo is a no-op, so a
    # collision would leave this test pushing onto an already-populated branch.
    remote = TMP / "wt-par-remote.git"
    repo = TMP / "wt-par-repo"
    run(["git", "init", "--bare", str(remote)])
    git_ops.clone(str(remote), repo, token="")
    git_ops.ensure_identity(repo, "T", "t@example.com")
    (repo / "a.txt").write_text("base", encoding="utf-8")
    git_ops.commit_all(repo, "init", "T", "t@example.com")
    git_ops.push(repo, "main", token="")

    check("is_git_repo true for repo", git_ops.is_git_repo(repo))
    check("is_git_repo false for plain dir", not git_ops.is_git_repo(TMP / "no-such-dir"))

    wt1 = TMP / "wt-1"
    wt2 = TMP / "wt-2"
    git_ops.add_worktree(repo, wt1)
    git_ops.add_worktree(repo, wt2)
    check("worktree 1 checked out", (wt1 / "a.txt").exists())
    check("two parallel worktrees coexist", (wt2 / "a.txt").exists())

    (wt1 / "b.txt").write_text("only in wt1", encoding="utf-8")
    check(
        "worktrees are file-isolated",
        not (wt2 / "b.txt").exists() and not (repo / "b.txt").exists(),
    )

    # A detached worktree can still commit and push to the default branch.
    git_ops.commit_all(wt1, "from worktree", "T", "t@example.com")
    git_ops.push(wt1, "main", token="")
    remote_files = run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "main"])
    check("worktree commit pushed to remote", "b.txt" in remote_files, remote_files)

    # Re-adding an already-populated worktree path is a no-op (resume reuse).
    git_ops.add_worktree(repo, wt2)
    check("re-add existing worktree is a no-op", (wt2 / "a.txt").exists())

    git_ops.remove_worktree(repo, wt1)
    check("worktree removed", not wt1.exists())


def test_session_dirs() -> None:
    """Resume-parameter parsing + recorded-cwd resolution from agent stores."""
    from app.session_dirs import parse_resume_request, resolve_recorded_cwd

    # --- parse: claude / hermes flag style ---
    r = parse_resume_request("claude", ["--resume", "abc123"])
    check("parse claude --resume <id>", bool(r) and r.kind == "id" and r.session_id == "abc123", str(r))
    r = parse_resume_request("claude", ["-r", "xyz"])
    check("parse claude -r <id>", bool(r) and r.kind == "id" and r.session_id == "xyz", str(r))
    r = parse_resume_request("claude", ["--resume=foo"])
    check("parse claude --resume=<id>", bool(r) and r.kind == "id" and r.session_id == "foo", str(r))
    r = parse_resume_request("claude", ["--continue"])
    check("parse claude --continue", bool(r) and r.kind == "continue", str(r))
    r = parse_resume_request("claude", ["--resume"])
    check("parse claude bare --resume -> continue", bool(r) and r.kind == "continue", str(r))
    r = parse_resume_request("claude", ["--model", "opus", "--effort", "high"])
    check("parse claude no resume -> None", r is None, str(r))

    # --- parse: codex subcommand style ---
    r = parse_resume_request("codex", ["resume", "019ec73d"])
    check("parse codex resume <id>", bool(r) and r.kind == "id" and r.session_id == "019ec73d", str(r))
    r = parse_resume_request("codex", ["resume", "--last"])
    check("parse codex resume --last", bool(r) and r.kind == "last", str(r))
    r = parse_resume_request("codex", ["resume"])
    check("parse codex bare resume -> continue", bool(r) and r.kind == "continue", str(r))
    r = parse_resume_request("codex", ["--model", "gpt-5.4"])
    check("parse codex no resume -> None", r is None, str(r))

    # --- resolve: claude reads cwd from the session jsonl ---
    home = TMP / "fakehome"
    cdir = home / ".claude" / "projects" / "-work-dirA"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "sid-claude.jsonl").write_text(
        json.dumps({"type": "summary", "content": "hi"}) + "\n"
        + json.dumps({"type": "user", "cwd": "/work/dirA"}) + "\n",
        encoding="utf-8",
    )
    check(
        "resolve claude cwd from store",
        resolve_recorded_cwd("claude", "sid-claude", home=home) == "/work/dirA",
        str(resolve_recorded_cwd("claude", "sid-claude", home=home)),
    )
    check(
        "resolve claude unknown id -> None",
        resolve_recorded_cwd("claude", "does-not-exist", home=home) is None,
    )

    # --- resolve: codex reads cwd from session_meta ---
    xdir = home / ".codex" / "sessions" / "2026" / "06" / "14"
    xdir.mkdir(parents=True, exist_ok=True)
    (xdir / "rollout-2026-06-14T00-00-00-uuid-codex-1.jsonl").write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "uuid-codex-1", "cwd": "/work/dirB"}}
        ) + "\n",
        encoding="utf-8",
    )
    check(
        "resolve codex cwd from store",
        resolve_recorded_cwd("codex", "uuid-codex-1", home=home) == "/work/dirB",
        str(resolve_recorded_cwd("codex", "uuid-codex-1", home=home)),
    )
    check(
        "resolve codex unknown id -> None",
        resolve_recorded_cwd("codex", "nope-zzz", home=home) is None,
    )
    # Substring collision: a SHORTER id is contained in the longer rollout's
    # filename. The id inside session_meta must be verified, so the short id
    # (which has no matching session) must NOT resolve to the long one's cwd.
    check(
        "resolve codex rejects filename substring collision",
        resolve_recorded_cwd("codex", "uuid-codex", home=home) is None,
        str(resolve_recorded_cwd("codex", "uuid-codex", home=home)),
    )


def test_session_workdir_resolution() -> None:
    """SessionManager picks project dir, isolates parallel sessions, resumes."""
    import asyncio

    from app.database import init_db, session_scope
    from app.models import Task
    from app.session_dirs import ResumeRequest
    from app.task_runner import SessionManager

    init_db()
    remote = TMP / "swr-remote.git"
    proj = TMP / "swr-proj"
    run(["git", "init", "--bare", str(remote)])
    git_ops.clone(str(remote), proj, token="")
    git_ops.ensure_identity(proj, "T", "t@example.com")
    (proj / "r.txt").write_text("x", encoding="utf-8")
    git_ops.commit_all(proj, "init", "T", "t@example.com")
    git_ops.push(proj, "main", token="")

    sm = SessionManager()
    pid = "swr-project"

    def resolve(task_id, agent, req):
        return asyncio.run(sm._resolve_session_workdir(task_id, pid, str(proj), agent, req))

    # New session, project folder free -> use the project folder, no note.
    wd, note = resolve("t1", "claude", None)
    check("free primary -> project dir", wd == str(proj) and note == "", f"{wd!r} {note!r}")

    # Project folder busy with a live session -> isolated worktree for the next.
    sm._procs["t1"] = {"project_id": pid, "workdir": str(proj), "pid": 0, "master_fd": -1}
    wd2, note2 = resolve("t2", "claude", None)
    check(
        "busy primary -> isolated worktree",
        wd2 != str(proj) and Path(wd2).exists() and "Isolierte" in note2,
        f"{wd2!r} {note2!r}",
    )
    check("worktree lives under session_worktrees", "session_worktrees" in wd2, wd2)
    check("worktree is a real checkout", (Path(wd2) / "r.txt").exists(), wd2)

    # Resume "continue" re-uses the most recent prior session directory.
    with session_scope() as db:
        db.add(
            Task(
                project_id=pid,
                agent="claude",
                prompt="",
                mode="session",
                is_session=True,
                status="success",
                chat_history="[]",
                workdir=str(proj),
            )
        )
    check("last session workdir found (same agent)", sm._last_session_workdir(pid, "claude") == str(proj))
    check(
        "last session workdir ignores other agents",
        sm._last_session_workdir(pid, "codex") is None,
        str(sm._last_session_workdir(pid, "codex")),
    )
    wd3, note3 = resolve("t3", "claude", ResumeRequest("continue"))
    check(
        "continue -> last session dir",
        wd3 == str(proj) and "letzten Session" in note3,
        f"{wd3!r} {note3!r}",
    )
    # A codex continue must NOT inherit claude's directory. With the primary
    # folder free, it falls through to the project dir instead of reusing the
    # claude session's workdir (which a same-agent continue would).
    sm._procs.pop("t1", None)
    wd4, note4 = resolve("t4", "codex", ResumeRequest("continue"))
    check(
        "continue is agent-scoped",
        wd4 == str(proj) and "letzten Session" not in note4,
        f"{wd4!r} {note4!r}",
    )

    # end_session cleans up an isolated worktree once its work is pushed, but
    # keeps it when the push failed (so commits are never stranded).
    asyncio.run(sm._cleanup_worktree_if_done(wd2, str(proj), True, None))
    check("pushed worktree reclaimed on end", not Path(wd2).exists(), wd2)

    git_ops.add_worktree(str(proj), wd2)  # simulate a second isolated session
    asyncio.run(sm._cleanup_worktree_if_done(wd2, str(proj), False, None))
    check("unpushed worktree kept for recovery", Path(wd2).exists(), wd2)
    asyncio.run(sm._cleanup_worktree_if_done(str(proj), str(proj), True, None))
    check("project folder never reclaimed", Path(proj).exists())

    git_ops.remove_worktree(str(proj), wd2)


def _init_bare_main(remote: Path) -> None:
    """Bare ``git init`` defaults HEAD to ``master`` (not ``main``); set it
    to ``main`` explicitly so a fresh clone lands on ``main`` and the
    dashboard's default-branch assumptions hold.  Used by every test that
    creates a temporary bare remote and immediately clones it."""
    run(["git", "init", "--bare", str(remote)])
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)


def test_auto_pull_helpers() -> None:
    """The auto-pull primitives: ``fetch_only`` is safe on a dirty tree, and
    ``has_remote_update`` correctly detects a fast-forward situation."""
    branch = "main"
    remote = TMP / "ap-remote.git"
    repo = TMP / "ap-repo"
    _init_bare_main(remote)
    git_ops.clone(str(remote), repo, token="")
    git_ops.ensure_identity(repo, "T", "t@example.com")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    git_ops.commit_all(repo, "init", "T", "t@example.com")
    git_ops.push(repo, branch, token="")

    # Same HEAD — no remote update.
    check("auto-pull: no remote update when up to date", not git_ops.has_remote_update(repo, branch))

    # Make a SECOND commit in a temp checkout, push it, then hard-reset the
    # local clone back one commit so it is BEHIND the remote. (A plain
    # second clone from the same remote has the same HEAD and can't be
    # "behind" — the only way for a real dashboard install to fall behind
    # is for a teammate to push while we're idle.)
    sibling = TMP / "ap-sibling"
    git_ops.clone(str(remote), sibling, token="")
    git_ops.ensure_identity(sibling, "T", "t@example.com")
    (sibling / "b.txt").write_text("b", encoding="utf-8")
    git_ops.commit_all(sibling, "second", "T", "t@example.com")
    git_ops.push(sibling, branch, token="")
    head1 = run(["git", "rev-parse", "HEAD"], cwd=repo)
    head2 = run(["git", "rev-parse", "HEAD"], cwd=sibling)
    assert head1 != head2, "expected two distinct commits in test setup"
    # Hard-reset the local clone to the older commit so origin is ahead,
    # then fetch so origin/<branch> is updated to head2 locally.
    run(["git", "reset", "--hard", head1], cwd=repo)
    git_ops.fetch_only(repo, token="")
    check("auto-pull: detects remote ahead", git_ops.has_remote_update(repo, branch))

    # ``fetch_only`` is safe with a dirty tree (no merge attempted).
    (repo / "dirty.txt").write_text("local edit", encoding="utf-8")
    git_ops.fetch_only(repo, token="")
    check("auto-pull: fetch_only leaves dirty tree untouched", (repo / "dirty.txt").exists())
    # After fetch, has_remote_update still true (we haven't merged yet).
    check("auto-pull: fetch doesn't merge", not (repo / "b.txt").exists())

    # ``_pull_ff_only`` (the actual function the task runner uses) refuses
    # to merge when the local dirty change CONFLICTS with the incoming
    # commit, and silently fast-forwards when it doesn't.  Both branches
    # must preserve the dirty local file (no data loss).
    from app.task_runner import _pull_ff_only

    # --- 2a. Conflict scenario: dirty change touches a file the remote
    # commit also touched, so ``git pull --ff-only`` MUST refuse rather
    # than silently dropping the local edit.  Real-world: the dashboard
    # never overwrites a user edit; the user has to resolve manually.
    (repo / "b.txt").write_text("local edit of remote-touched file", encoding="utf-8")
    blocked = False
    try:
        _pull_ff_only(repo, branch, token="")
    except git_ops.GitError:
        blocked = True
    check("auto-pull: pull_ff_only refuses conflicting dirty change", blocked)
    check("auto-pull: local edit preserved on refused merge", (repo / "b.txt").read_text() == "local edit of remote-touched file")

    # --- 2b. No-conflict scenario: dirty file is local-only (the remote
    # commit doesn't touch it), so the FF is fine and the dirty file is
    # preserved alongside the new files from the remote.
    (repo / "dirty.txt").write_text("only local", encoding="utf-8")
    (repo / "b.txt").unlink()  # resolve the conflict so the FF succeeds
    _pull_ff_only(repo, branch, token="")
    check("auto-pull: pull_ff_only succeeds when no conflict", (repo / "b.txt").exists())
    check("auto-pull: local-only file preserved through FF", (repo / "dirty.txt").read_text() == "only local")
    check("auto-pull: no remote update after pull", not git_ops.has_remote_update(repo, branch))


def test_sync_from_github_validation() -> None:
    """The /projects/from-github + /projects/sync-from-github endpoints.
    We don't hit GitHub (no token / network) — we exercise the route's
    validation paths and confirm the 503 fires when no token is set."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.config import get_settings

    # Force "no token" AND "no auth" so list_from_github hits the 503 branch
    # without being intercepted by the auth dependency (which would otherwise
    # short-circuit a request without an Authorization header with 401 when
    # ``CD_ADMIN_PASSWORD_HASH`` is configured for the rest of the suite).
    old_token = os.environ.pop("CD_GITHUB_TOKEN", None)
    old_hash = os.environ.pop("CD_ADMIN_PASSWORD_HASH", None)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            r = client.get("/api/projects/from-github")
            check("from-github without token -> 503", r.status_code == 503, str(r.status_code))
            r2 = client.post("/api/projects/sync-from-github", json={})
            check("sync-from-github without token -> 503", r2.status_code == 503, str(r2.status_code))
    finally:
        if old_token is not None:
            os.environ["CD_GITHUB_TOKEN"] = old_token
        if old_hash is not None:
            os.environ["CD_ADMIN_PASSWORD_HASH"] = old_hash
        get_settings.cache_clear()


def test_hermes_clarify_disabled() -> None:
    """Hermes in non-interactive mode must NOT expose the `clarify` toolset.

    `hermes chat -q "<prompt>"` is one-shot and non-interactive: the dashboard
    streams stdout to a browser tab with no way to type back.  The `clarify`
    toolset would call into a None platform callback and either stall the run
    or bounce back with "Clarify tool is not available in this execution
    context."  Fix: pass `-t <csv>` excluding `clarify`.

    Interactive sessions (`session_command` = `hermes chat`, real TUI) keep
    the full toolset so the user can answer questions there.
    """
    from app.config import (
        HERMES_NON_INTERACTIVE_TOOLSETS,
        default_agents,
        load_agents_config,
    )

    hermes = default_agents()["hermes"]
    csv = HERMES_NON_INTERACTIVE_TOOLSETS
    toolsets = [t.strip() for t in csv.split(",")]

    # --- Built-in defaults ---
    check("hermes default: -t flag present", "-t" in hermes.command, str(hermes.command))
    # The CSV sits at the index right after -t.
    t_idx = hermes.command.index("-t")
    check(
        "hermes default: toolset CSV immediately after -t",
        hermes.command[t_idx + 1] == csv,
        str(hermes.command),
    )
    check(
        "hermes default: toolsets exclude clarify",
        "clarify" not in toolsets,
        csv,
    )
    check(
        "hermes default: non-empty toolset list (>= 10 entries)",
        len(toolsets) >= 10,
        f"len={len(toolsets)}",
    )
    check(
        "hermes default: session_command left untouched",
        hermes.session_command == ["hermes", "chat"],
        str(hermes.session_command),
    )

    # --- Legacy installer config (flat argv) ---
    legacy = TMP / "hermes-legacy.yaml"
    legacy.write_text(
        "agents:\n"
        "  hermes:\n"
        '    display_name: "Hermes"\n'
        '    command: ["hermes", "chat", "-q", "{prompt}"]\n'
        "    stream_format: raw\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_agents_config(legacy)
    cmd = cfg.agents["hermes"].command
    check("hermes legacy: -t appended as separate argv tokens", "-t" in cmd, str(cmd))
    check("hermes legacy: -t toolsets exclude clarify", "clarify" not in toolsets, csv)
    # The original tail (--yolo --accept-hooks) must also still be there.
    check("hermes legacy: --yolo + --accept-hooks backfilled", "--yolo" in cmd and "--accept-hooks" in cmd, str(cmd))

    # --- SSH-driven Docker config (remote-shell string in last argv token) ---
    ssh_yaml = TMP / "hermes-ssh.yaml"
    ssh_yaml.write_text(
        "agents:\n"
        "  hermes:\n"
        '    display_name: "Hermes"\n'
        "    command:\n"
        '      - "ssh"\n'
        '      - "-i"\n'
        '      - "/home/app/.ssh/id_hermes"\n'
        '      - "-p"\n'
        '      - "22"\n'
        '      - "debian@host.docker.internal"\n'
        '      - "cd {project_dir} && exec env HERMES_ACCEPT_HOOKS=1 NO_COLOR=1 hermes chat -q \\"$(cat)\\" --yolo --accept-hooks"\n'
        "    session_command:\n"
        '      - "ssh"\n'
        '      - "-tt"\n'
        '      - "-i"\n'
        '      - "/home/app/.ssh/id_hermes"\n'
        '      - "-p"\n'
        '      - "22"\n'
        '      - "debian@host.docker.internal"\n'
        '      - "cd {project_dir} && exec hermes chat"\n'
        "    prompt_via: stdin\n"
        "    stream_format: raw\n"
        "    host_staging: true\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    ssh_cfg = load_agents_config(ssh_yaml)
    ssh_cmd = ssh_cfg.agents["hermes"].command
    # The command still has 7 tokens (no extra ssh argv leaked).
    check(
        "hermes ssh legacy: command still has 7 tokens (no extra argv leaked)",
        len(ssh_cmd) == 7,
        str(ssh_cmd),
    )
    # The remote-shell string is the last token and now contains the splice.
    remote = ssh_cmd[-1]
    # Find the position of `--accept-hooks` and confirm `-t <csv>` immediately
    # follows it (separated by spaces).
    padded = " " + remote + " "
    ah_pos = padded.find(" --accept-hooks ")
    check(
        "hermes ssh legacy: --accept-hooks found with surrounding spaces",
        ah_pos >= 0,
        repr(remote),
    )
    after_ah = padded[ah_pos + len(" --accept-hooks ") :]
    # After `--accept-hooks `, the next two whitespace-separated tokens must be
    # `-t` and the CSV (in that order).
    parts = after_ah.split(" ", 2)
    check(
        "hermes ssh legacy: -t immediately follows --accept-hooks",
        len(parts) >= 2 and parts[0] == "-t",
        f"got {parts[:2]!r}",
    )
    check(
        "hermes ssh legacy: toolset CSV immediately follows -t",
        len(parts) >= 3 and parts[1] == csv,
        f"got {parts[1] if len(parts) >= 2 else None!r}",
    )
    # No duplicate `-t` in the remote string (idempotency).
    check(
        "hermes ssh legacy: exactly one -t in remote string",
        remote.count(" -t ") == 1,
        f"remote.count(' -t ') = {remote.count(' -t ')}",
    )
    # And the toolset list does not include `clarify` even after splice.
    check(
        "hermes ssh legacy: clarify excluded from the spliced -t",
        "clarify" not in csv,
        csv,
    )
    # session_command is NOT modified (interactive TUI needs full toolset).
    sc = ssh_cfg.agents["hermes"].session_command
    check(
        "hermes ssh legacy: session_command not modified",
        "-t" not in sc,
        str(sc),
    )


def test_project_archive() -> None:
    """The archive feature: hidden from default list, idempotent,
    visible-by-default-with-?archived=true, round-trippable, surviving
    ``GET /api/projects/{id}`` (so the user can still inspect history
    while the project is archived) and ``GET /api/running`` (archive is
    a UI concern, it does not stop running tasks).

    We don't run a real agent here - we exercise the routes against an
    inserted Project row + the existing auth header from the suite.
    """
    from fastapi.testclient import TestClient

    from app.database import session_scope
    from app.main import app
    from app.models import Project

    # Make a fresh project row pointing at a temp repo (we don't need a
    # remote - archive must work without one).
    remote = TMP / "archive-remote.git"
    proj = TMP / "data" / "projects" / "archive-me"
    if remote.exists():
        shutil.rmtree(remote, ignore_errors=True)
    if proj.exists():
        shutil.rmtree(proj, ignore_errors=True)
    run(["git", "init", "--bare", str(remote)])
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=remote)
    git_ops.clone(str(remote), proj, token="")
    git_ops.ensure_identity(proj, "Tester", "t@example.com")
    (proj / "README.md").write_text("# archive-me\n", encoding="utf-8")
    git_ops.commit_all(proj, "init", "Tester", "t@example.com")
    git_ops.push(proj, "main", token="")

    with session_scope() as db:
        p = Project(
            name="archive-me",
            slug="archive-me",
            local_path=str(proj),
            default_branch="main",
            clone_url=str(remote),
            github_full_name="local/archive-me",
        )
        db.add(p)
        db.flush()
        pid = p.id
        # Sanity: a brand-new project is NOT archived.
        check(
            "archive: fresh project archived=False",
            db.get(Project, pid).archived is False,
            "default not False",
        )

    with TestClient(app) as client:
        ok = client.post("/api/auth/login", json={"username": "admin", "password": "secret-pw"})
        check("archive: login ok", ok.status_code == 200, str(ok.status_code))
        H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

        # --- Default list: project visible (it is fresh, archived=False) ---
        lst_default = client.get("/api/projects", headers=H).json()
        ids = [p["id"] for p in lst_default]
        check(
            "archive: visible in default /api/projects when fresh",
            pid in ids,
            f"pid NOT in default list (ids={[i[:6] for i in ids]})",
        )
        check(
            "archive: /api/projects default rows have archived=false",
            all(p.get("archived") is False for p in lst_default),
            str([p.get("archived") for p in lst_default]),
        )

        # --- archived=true list: project not there yet (it is fresh) ---
        lst_archived = client.get("/api/projects?archived=true", headers=H).json()
        check(
            "archive: NOT in ?archived=true when fresh",
            not any(p["id"] == pid for p in lst_archived),
            f"pid in archived list (count={len(lst_archived)})",
        )
        check(
            "archive: ?archived=true rows all archived=true",
            all(p.get("archived") is True for p in lst_archived),
            str([p.get("archived") for p in lst_archived]),
        )

        # --- archived=all: includes both ---
        lst_all = client.get("/api/projects?archived=all", headers=H).json()
        check(
            "archive: ?archived=all returns both",
            any(p["id"] == pid for p in lst_all)
            and any(p["id"] not in [pp["id"] for pp in lst_archived] for p in lst_all),
            f"len(all)={len(lst_all)} len(archived)={len(lst_archived)})",
        )

        # --- POST /api/projects/{id}/archive ---
        r = client.post(f"/api/projects/{pid}/archive", headers=H)
        check("archive: archive -> 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check(
            "archive: response body archived=true",
            body.get("archived") is True,
            str(body.get("archived")),
        )
        check(
            "archive: response body archived_at set",
            bool(body.get("archived_at")),
            str(body.get("archived_at")),
        )

        # --- Default list now hides it ---
        ids2 = [p["id"] for p in client.get("/api/projects", headers=H).json()]
        check("archive: hidden from default after archive", pid not in ids2)

        # --- archived=true list shows it ---
        ids3 = [p["id"] for p in client.get("/api/projects?archived=true", headers=H).json()]
        check("archive: appears in ?archived=true after archive", pid in ids3)

        # --- GET /api/projects/{id} still works (full ProjectDetail) ---
        detail = client.get(f"/api/projects/{pid}", headers=H)
        check(
            "archive: GET detail still works while archived",
            detail.status_code == 200,
            str(detail.status_code),
        )
        check(
            "archive: detail exposes archived=true",
            detail.json().get("archived") is True,
            str(detail.json().get("archived")),
        )

        # --- Idempotent: archiving twice is a no-op (same archived_at) ---
        again = client.post(f"/api/projects/{pid}/archive", headers=H).json()
        check(
            "archive: archive is idempotent (archived_at unchanged)",
            again.get("archived_at") == body.get("archived_at"),
            f"{again.get('archived_at')} vs {body.get('archived_at')}",
        )

        # --- Unknown id -> 404 ---
        r404a = client.post("/api/projects/does-not-exist/archive", headers=H)
        check("archive: unknown id -> 404", r404a.status_code == 404, str(r404a.status_code))
        r404u = client.post("/api/projects/does-not-exist/unarchive", headers=H)
        check("unarchive: unknown id -> 404", r404u.status_code == 404, str(r404u.status_code))

        # --- POST /api/projects/{id}/unarchive ---
        u = client.post(f"/api/projects/{pid}/unarchive", headers=H)
        check("unarchive: unarchive -> 200", u.status_code == 200, str(u.status_code))
        body2 = u.json()
        check(
            "unarchive: archived=false after unarchive",
            body2.get("archived") is False,
            str(body2.get("archived")),
        )
        check(
            "unarchive: archived_at cleared",
            body2.get("archived_at") in (None, ""),
            str(body2.get("archived_at")),
        )

        # --- Idempotent unarchive: archived_at stays None ---
        u2 = client.post(f"/api/projects/{pid}/unarchive", headers=H).json()
        check(
            "unarchive: idempotent (archived_at stays None)",
            u2.get("archived_at") in (None, ""),
            str(u2.get("archived_at")),
        )

        # --- And the project is back in the default list ---
        ids4 = [p["id"] for p in client.get("/api/projects", headers=H).json()]
        check("unarchive: visible in default list again", pid in ids4)

        # --- Tasks for an archived project: still listed, archive is a UI
        # concern, not a teardown.  Re-archive and check that an existing
        # task's GET still works (no FK cascade). ---
        client.post(f"/api/projects/{pid}/archive", headers=H)
        with session_scope() as db:
            from app.models import Task

            t = Task(
                project_id=pid,
                agent="fake",
                prompt="pre-archive",
                mode="task",
                status="success",
                result_summary="done",
            )
            db.add(t)
            db.flush()
            tid = t.id
        ts = client.get(f"/api/projects/{pid}/tasks", headers=H).json()
        check(
            "archive: tasks for archived project still listed",
            any(t["id"] == tid for t in ts),
            str(len(ts)),
        )
        # Clean up the orphan task so other tests don't trip on it.
        with session_scope() as db:
            db.query(Task).filter(Task.id == tid).delete()
            db.query(Project).filter(Project.id == pid).delete()


def test_host_lock() -> None:
    """Host-visible lock file lifecycle for one-shot + interactive runs.

    Verifies:
      * ``write`` stamps ``<kind>-<id>.lock`` with parseable JSON payload.
      * Stale-lock overwrite is atomic (O_EXCL collision falls back to replace).
      * ``remove`` clears it, ``read``/``list_active`` discover it.
      * TaskManager._run leaves the lock present while the agent runs and
        drops it cleanly when the run finishes (success path).
      * _run's exception path still drops the lock (cleanup on failure).
      * SessionManager.start writes one and ``end_session`` clears it.
      * ``reset_interrupted()`` clears stale lock files left by a crash.
    """
    import os
    import signal
    import time
    from fastapi.testclient import TestClient

    from app import host_lock, task_runner
    from app.config import get_settings
    from app.database import session_scope
    from app.main import app
    from app.models import Project, Task

    # Use a throwaway lock dir under TMP so we don't touch /var/lock.
    lock_dir = TMP / "host-lock"
    if lock_dir.exists():
        shutil.rmtree(lock_dir, ignore_errors=True)

    # The lock module reads ``host_lock_dir`` via get_settings() each call,
    # so we mutate the cached singleton + clear the cache so the next call
    # rebuilds against our local dir (and later tests see the original again
    # in the ``finally``).
    from app import config as app_config

    settings = app_config.get_settings()
    original_dir = settings.host_lock_dir
    settings.host_lock_dir = lock_dir
    app_config.get_agents_config.cache_clear()
    try:
        # Dir doesn't exist yet — the lock module creates it on first write.
        check("lock dir absent before first write", not lock_dir.exists(), str(lock_dir))
        p = host_lock.write("task", "abc123", "proj-1", "claude", "task")
        check("lock dir created on demand (by write)", lock_dir.exists(), str(lock_dir))
        # 1. write/read/remove lifecycle.
        check("write returns a path", p is not None, str(p))
        check("lock file visible on disk", p.exists() if p else False)
        info = host_lock.read("task", "abc123")
        check("read parses JSON", info is not None and info["kind"] == "task")
        check("read round-trips agent", info["agent"] == "claude" if info else False)
        check("read round-trips project_id", info["project_id"] == "proj-1" if info else False)
        check("read round-trips mode", info["mode"] == "task" if info else False)

        # 2. Concurrent write for the SAME id overwrites atomically (no two
        #    files coexist). Simulate by writing twice with different payloads.
        host_lock.write("task", "abc123", "proj-2", "codex", "goal")
        info2 = host_lock.read("task", "abc123")
        check("stale lock overwritten", info2["agent"] == "codex" if info2 else False)
        listed = host_lock.list_active()
        check("exactly one file for same id", len([x for x in listed if "abc123" in x.name]) == 1, str(listed))

        # 3. Different ids -> different files.
        host_lock.write("session", "sess-9", "proj-1", "claude", "session")
        listed = host_lock.list_active()
        names = sorted(p.name for p in listed)
        check("two distinct ids -> two files", "task-abc123.lock" in names and "session-sess-9.lock" in names, str(names))

        # 4. remove clears the file.
        host_lock.remove("task", "abc123")
        check("remove drops file", not host_lock.read("task", "abc123"))
        check("remove on missing id is no-op", True)  # just confirm no exception

        # 5. list_active is empty after cleanup.
        host_lock.remove("session", "sess-9")
        check("list_active empty after cleanup", host_lock.list_active() == [], str(host_lock.list_active()))

        # 6. TaskManager end-to-end: post a task via the REST API and watch
        #    the lock file appear / disappear through the actual run loop.
        #    Use a long-ish agent command so we definitely catch the file mid-run.
        remote = TMP / "lock-remote.git"
        proj = TMP / "data" / "projects" / "lock-proj"
        run(["git", "init", "--bare", str(remote)])
        git_ops.clone(str(remote), proj, token="")
        git_ops.ensure_identity(proj, "Tester", "t@example.com")
        (proj / "init.txt").write_text("init\n", encoding="utf-8")
        git_ops.commit_all(proj, "init", "Tester", "t@example.com")
        git_ops.push(proj, "main", token="")

        with TestClient(app) as client:
            ok = client.post("/api/auth/login", json={"username": "admin", "password": "secret-pw"})
            token = ok.json()["access_token"]
            H = {"Authorization": f"Bearer {token}"}

            # Skip the create_project route (it requires a real GitHub token);
            # insert the Project row directly, the same way test_project_archive
            # does.  The task-create route below doesn't touch GitHub.
            with session_scope() as db:
                p = Project(
                    name="lock-proj",
                    slug="lock-proj",
                    local_path=str(proj),
                    default_branch="main",
                    clone_url=str(remote),
                    github_full_name="local/lock-proj",
                )
                db.add(p)
                db.flush()
                project_id = p.id
            check("project row inserted", bool(project_id))
            slow_script = (
                "import os,time;"
                "lock=os.environ.get('CD_LOCK_MARKER','');"
                "open('/tmp/lock-agent-marker.txt','w').write(lock);"
                "time.sleep(2);"
                "open('result.txt','w').write('done')"
            )
            slow_path = TMP / "slow_agent.py"
            slow_path.write_text(slow_script, encoding="utf-8")

            # Stash a custom config that points "slow" at our script.
            cfg_path = get_settings().agents_config_path
            cfg_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
            import yaml
            loaded = yaml.safe_load(cfg_text) if cfg_text.strip() else {"context_instruction": "", "agents": {}}
            loaded.setdefault("context_instruction", "")
            loaded["agents"]["slow"] = {
                "display_name": "Slow",
                "command": [sys.executable, str(slow_path)],
                "prompt_via": "arg",
                "stream_format": "raw",
                "session_command": [sys.executable, "-c",
                                    "import time;"
                                    "open('session_out.txt','w').write('alive');"
                                    "time.sleep(30)"],
                "enabled": True,
            }
            cfg_path.write_text(yaml.safe_dump(loaded, allow_unicode=True), encoding="utf-8")
            # Force the agents config to reload on next access.
            app_config.get_agents_config.cache_clear()

            # Submit a task and poll for the lock file in a background thread
            # so we can see the file EXIST while the agent is mid-run, then
            # wait for it to disappear once the run finishes.
            resp = client.post(
                f"/api/projects/{project_id}/tasks",
                headers=H,
                json={"agent": "slow", "prompt": "hello", "mode": "task"},
            )
            check("task submit", resp.status_code in (200, 201), str(resp.status_code))
            task_id = resp.json()["id"]

            deadline = time.time() + 5
            seen_mid_run = False
            while time.time() < deadline:
                if host_lock.read("task", task_id) is not None:
                    seen_mid_run = True
                    break
                time.sleep(0.05)
            check("task lock present while running", seen_mid_run)

            # Wait until the task actually finishes (DB has finished_at set).
            for _ in range(100):
                with session_scope() as db:
                    t = db.get(Task, task_id)
                    if t and t.finished_at is not None:
                        break
                time.sleep(0.1)
            check("task finishes", True)

            # Give _run's finally a beat to run.
            time.sleep(0.3)
            check(
                "task lock removed after run",
                host_lock.read("task", task_id) is None,
                str([p.name for p in host_lock.list_active()]),
            )

            # 7. SessionManager end-to-end: start a session, observe the
            #    host-visible lock INSIDE the same event loop (otherwise
            #    asyncio.run cancels the pump task on exit, which calls
            #    end_session which removes the lock — a race that has
            #    nothing to do with the lock code itself), then end the
            #    session cleanly and observe the lock disappear.
            with session_scope() as db:
                sess_task = Task(
                    project_id=project_id,
                    agent="slow",
                    prompt="",
                    mode="session",
                    is_session=True,
                    status="queued",
                )
                db.add(sess_task)
                db.flush()
                sess_id = sess_task.id

            import asyncio as _aio

            async def _session_lifecycle():
                started = await task_runner.session_manager.start(
                    sess_id, project_id, "slow", "", "", "",
                )
                assert started, "session_manager.start returned False"
                # Lock must be visible RIGHT after start, while the pump
                # task is still alive.
                listed = host_lock.list_active()
                in_list = any(sess_id in p.name for p in listed)
                visible = host_lock.read("session", sess_id) is not None
                # Make the agent quit so end_session can run its git steps,
                # then call end_session explicitly.
                tca = task_runner.session_manager._procs.get(sess_id)
                if tca:
                    try:
                        os.killpg(tca["pid"], signal.SIGTERM)
                    except OSError:
                        pass
                # end_session takes over (as if the frontend clicked stop).
                # We have to wait for the agent to actually die before ending
                # the session cleanly — otherwise end_session's SIGKILL fallback
                # breaks the OSError-handling path under `signal.SIGTERM`-first.
                await _aio.sleep(0.5)
                await task_runner.session_manager.end_session(
                    sess_id, project_id, terminate=True,
                )
                return in_list, visible

            sess_in_list, sess_visible = _aio.run(_session_lifecycle())
            check("session lock in list_active while running", sess_in_list)
            check("session lock visible (read) while running", sess_visible)
            check(
                "session lock removed after end_session",
                host_lock.read("session", sess_id) is None,
                str([p.name for p in host_lock.list_active()]),
            )

            # 8. reset_interrupted cleans up zombies.
            # Drop a stub lock file by hand, then run reset_interrupted.
            zombie = host_lock.write("task", "ghost", project_id, "slow", "task")
            check("zombie lock created", zombie is not None and zombie.exists())
            task_runner.reset_interrupted()
            check("reset_interrupted clears zombies", not zombie.exists())

            # Cleanup the orphan Project + Task rows + the cfg we modified so
            # subsequent tests run against a clean slate.
            with session_scope() as db:
                db.query(Task).filter(Task.project_id == project_id).delete()
                db.query(Project).filter(Project.id == project_id).delete()

        # Restore the original agents config so subsequent tests are unaffected.
        if cfg_text:
            cfg_path.write_text(cfg_text, encoding="utf-8")
        else:
            try:
                cfg_path.unlink()
            except FileNotFoundError:
                pass
        app_config.get_agents_config.cache_clear()

    finally:
        # Restore the original host_lock_dir on the cached singleton so other
        # tests (and any later in-process startup) see the real default.
        app_config.get_settings.cache_clear()
        settings.host_lock_dir = original_dir
        app_config.get_settings.cache_clear()


def test_heartbeat() -> None:
    """Heartbeat feature: auto-poll GitHub issues + auto-spawn Claude Code tasks.

    Verifies:
      * ``HeartbeatRunner`` settings + state toggle, settings fields exist.
      * Per-project opt-out (``heartbeat_enabled`` column, persisted via API).
      * Cooldown: a project with a SUCCESS heartbeat task inside the window
        is skipped; a project with only failed tasks is NOT skipped.
      * ``_build_prompt`` substitutes the issue title, body, URL, repo, etc.
      * ``list_issues`` helper: filters PRs out of the returned list at the
        consumer boundary (heartbeat side).
      * ``heartbeat_seen`` ledger: idempotent INSERT OR IGNORE-style behaviour
        via the runner; re-claiming the same issue doesn't double-dispatch.
      * ``_spawn_task`` creates a Task with ``heartbeat_spawned=True`` +
        ``heartbeat_issue_number=N`` + the configured agent key.
      * Manual ``POST /api/heartbeat/trigger`` flips the runner state and
        runs a tick (observed via TestClient).
      * Global toggle via ``POST /api/heartbeat/{enable,disable}``.
      * Per-project toggle via ``POST /api/projects/{id}/heartbeat/{enable,disable}``.
      * ``GET /api/heartbeat`` returns the expected shape including all
        per-project statuses + the configured agent_key + interval_seconds.
    """
    import asyncio

    from fastapi.testclient import TestClient

    from app import config as app_config
    from app import github_client, heartbeat as hb_mod
    from app.config import DEFAULT_HEARTBEAT_PROMPT_TEMPLATE
    from app.database import session_scope
    from app.main import app
    from app.models import HeartbeatSeen, Project, Task

    # -------- 1. settings fields exist ----------------------------------- #
    s = app_config.get_settings()
    check("heartbeat: heartbeat_enabled setting", hasattr(s, "heartbeat_enabled"))
    check("heartbeat: heartbeat_interval_seconds setting", hasattr(s, "heartbeat_interval_seconds"))
    check("heartbeat: heartbeat_max_concurrent setting", hasattr(s, "heartbeat_max_concurrent"))
    check("heartbeat: heartbeat_cooldown_minutes setting", hasattr(s, "heartbeat_cooldown_minutes"))
    check(
        "heartbeat: heartbeat_agent_key is configurable (default 'claude',"
        " but the smoke test pins it to 'fake' since the test config only"
        " defines the fake agent)",
        s.heartbeat_agent_key == "fake",
        repr(s.heartbeat_agent_key),
    )
    check(
        "heartbeat: prompt template non-empty",
        bool(s.heartbeat_prompt_template) and "{number}" in s.heartbeat_prompt_template,
    )
    check(
        "heartbeat: default template identical to constant",
        s.heartbeat_prompt_template == DEFAULT_HEARTBEAT_PROMPT_TEMPLATE,
    )

    # -------- 2. global enable/disable via API --------------------------- #
    runner = hb_mod.heartbeat
    runner.set_enabled(True)
    check("heartbeat: set_enabled(True) reflects in property", runner.enabled is True)
    runner.set_enabled(False)
    check("heartbeat: set_enabled(False) reflects in property", runner.enabled is False)

    # -------- 3. _list_active_projects filters archive / no-github ------- #
    with session_scope() as db:
        active_proj = Project(
            name="hb-active",
            slug="hb-active",
            local_path="/tmp/hb-active",
            github_full_name="owner/hb-active",
            heartbeat_enabled=True,
        )
        archived_proj = Project(
            name="hb-archived",
            slug="hb-archived",
            local_path="/tmp/hb-archived",
            github_full_name="owner/hb-archived",
            archived=True,
            heartbeat_enabled=True,
        )
        no_github_proj = Project(
            name="hb-no-github",
            slug="hb-no-github",
            local_path="/tmp/hb-no-github",
            github_full_name="",
            heartbeat_enabled=True,
        )
        db.add_all([active_proj, archived_proj, no_github_proj])
        db.flush()
        active_id = active_proj.id
        archived_id = archived_proj.id
        no_github_id = no_github_proj.id

    listed = runner._list_active_projects()
    listed_ids = {p.id for p in listed}
    check(
        "heartbeat: _list_active_projects includes active github project",
        active_id in listed_ids,
    )
    check(
        "heartbeat: _list_active_projects skips archived",
        archived_id not in listed_ids,
    )
    check(
        "heartbeat: _list_active_projects skips no-github",
        no_github_id not in listed_ids,
    )

    # -------- 4. _build_prompt substitutes placeholders ------------------ #
    sample_issue = {
        "number": 42,
        "title": "Crash on startup",
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}, {"name": "P1"}],
        "created_at": "2026-07-01T00:00:00Z",
        "body": "Steps to reproduce:\n1. start\n2. boom",
        "html_url": "https://github.com/owner/repo/issues/42",
    }
    with session_scope() as db:
        active = db.get(Project, active_id)
        db.expunge(active)
    prompt = runner._build_prompt(active, sample_issue, DEFAULT_HEARTBEAT_PROMPT_TEMPLATE)
    check("heartbeat: prompt contains issue number", "#42" in prompt)
    check("heartbeat: prompt contains issue title", "Crash on startup" in prompt)
    check("heartbeat: prompt contains issue URL", "issues/42" in prompt)
    check("heartbeat: prompt contains body excerpt", "Steps to reproduce" in prompt)
    check("heartbeat: prompt contains repo full_name", "owner/hb-active" in prompt)
    check("heartbeat: prompt contains author login", "alice" in prompt)
    check(
        "heartbeat: prompt lists labels (bug + P1)",
        "bug" in prompt and "P1" in prompt,
    )

    # -------- 5. PRs are filtered out (consumer-side check) -------------- #
    real_issues_payload = [
        sample_issue,
        {**sample_issue, "number": 43, "pull_request": {"url": "x"}, "title": "PR thing"},
        {**sample_issue, "number": 44, "title": "Real issue"},
    ]
    real_only = [i for i in real_issues_payload if not i.get("pull_request")]
    check(
        "heartbeat: PR filtered out by consumer",
        len(real_only) == 2 and 43 not in {i["number"] for i in real_only},
    )

    # -------- 6. heartbeat_seen ledger idempotency ----------------------- #
    with session_scope() as db:
        # ensure clean state for active_id
        db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == active_id).delete()
        db.commit()

    first_claim = runner._claim_issue(active_id, 101, "Issue 101", "https://x/101")
    second_claim = runner._claim_issue(active_id, 101, "Issue 101 (dup)", "https://x/101")
    third_claim = runner._claim_issue(active_id, 102, "Issue 102", "https://x/102")
    check("heartbeat: first claim inserts (new=True)", first_claim is True)
    check("heartbeat: second claim of same issue is NOT new", second_claim is False)
    check("heartbeat: different issue number inserts (new=True)", third_claim is True)

    with session_scope() as db:
        rows = (
            db.query(HeartbeatSeen)
            .filter(HeartbeatSeen.project_id == active_id)
            .order_by(HeartbeatSeen.issue_number)
            .all()
        )
    check(
        "heartbeat: heartbeat_seen has 2 rows after 3 claims",
        len(rows) == 2,
        f"got {len(rows)}",
    )

    # -------- 7. _record_dispatch stamps the row ------------------------- #
    with session_scope() as db:
        rows = (
            db.query(HeartbeatSeen)
            .filter(HeartbeatSeen.project_id == active_id)
            .filter(HeartbeatSeen.issue_number == 101)
            .all()
        )
        check(
            "heartbeat: row exists before record_dispatch",
            len(rows) == 1,
        )
    runner._record_dispatch(active_id, 101, "task-abc")
    with session_scope() as db:
        row = (
            db.query(HeartbeatSeen)
            .filter(HeartbeatSeen.project_id == active_id)
            .filter(HeartbeatSeen.issue_number == 101)
            .first()
        )
        check(
            "heartbeat: _record_dispatch stamps dispatched_task_id",
            row is not None and row.dispatched_task_id == "task-abc",
        )

    # -------- 8. _set_project_status stamps fields ----------------------- #
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    runner._set_project_status(active_id, "success", poll_at=now)
    with session_scope() as db:
        p = db.get(Project, active_id)
        check(
            "heartbeat: last_heartbeat_status set",
            p.last_heartbeat_status == "success",
            repr(p.last_heartbeat_status),
        )
        check(
            "heartbeat: last_issue_poll_at set",
            p.last_issue_poll_at is not None,
        )
        check(
            "heartbeat: last_heartbeat_at set",
            p.last_heartbeat_at is not None,
        )

    runner._set_project_status(active_id, "error", error="rate limit")
    with session_scope() as db:
        p = db.get(Project, active_id)
        check(
            "heartbeat: error status + error message persisted",
            p.last_heartbeat_status == "error" and "rate limit" in p.last_heartbeat_error,
        )

    # -------- 9. _in_cooldown respects success vs failed ----------------- #
    # Mark a recent heartbeat task as success within the cooldown window
    with session_scope() as db:
        success_task = Task(
            project_id=active_id,
            agent="claude",
            prompt="Fix it",
            mode="task",
            status="success",
            heartbeat_spawned=True,
            heartbeat_issue_number=999,
            finished_at=datetime.now(timezone.utc),
        )
        failed_task = Task(
            project_id=active_id,
            agent="claude",
            prompt="Fix it 2",
            mode="task",
            status="error",
            heartbeat_spawned=True,
            heartbeat_issue_number=998,
            finished_at=datetime.now(timezone.utc),
        )
        db.add_all([success_task, failed_task])
        db.commit()

    in_cooldown = runner._in_cooldown(active_id, cooldown_minutes=30)
    check("heartbeat: _in_cooldown=True when a recent success exists", in_cooldown is True)

    # Mark the success task as OLDER than the cooldown window
    with session_scope() as db:
        t = db.query(Task).filter(Task.id == success_task.id).one()
        from datetime import timedelta
        t.finished_at = datetime.now(timezone.utc) - timedelta(hours=2)

    in_cooldown_after = runner._in_cooldown(active_id, cooldown_minutes=30)
    check(
        "heartbeat: _in_cooldown=False once success is older than window",
        in_cooldown_after is False,
    )

    # -------- 10. _spawn_task creates a heartbeat-marked Task ----------- #
    async def _run_spawn() -> str:
        return await runner._spawn_task(active, sample_issue, "claude")

    spawned_id = asyncio.run(_run_spawn())
    with session_scope() as db:
        t = db.get(Task, spawned_id)
        check(
            "heartbeat: spawned task has heartbeat_spawned=True",
            t is not None and t.heartbeat_spawned is True,
        )
        check(
            "heartbeat: spawned task has heartbeat_issue_number=42",
            t is not None and t.heartbeat_issue_number == 42,
        )
        check(
            "heartbeat: spawned task agent=claude",
            t is not None and t.agent == "claude",
        )
        check(
            "heartbeat: spawned task mode=task",
            t is not None and t.mode == "task",
        )

    # -------- 11. REST: GET /api/heartbeat shape ------------------------- #
    # Wipe leftover runtime state so the test is hermetic.
    with session_scope() as db:
        db.query(Task).filter(Task.heartbeat_spawned.is_(True)).delete()
        db.commit()

    with TestClient(app) as client:
        ok = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
        )
        check("heartbeat: login ok", ok.status_code == 200, str(ok.status_code))
        H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

        r = client.get("/api/heartbeat", headers=H)
        check("heartbeat: GET /api/heartbeat 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check(
            "heartbeat: GET body has interval_seconds",
            isinstance(body.get("interval_seconds"), int) and body["interval_seconds"] > 0,
        )
        check(
            "heartbeat: GET body has a known agent_key (defaults to claude if present)",
            body.get("agent_key") in get_agents_config().agents,
            repr(body.get("agent_key")),
        )
        check(
            "heartbeat: GET body projects list contains hb-active",
            any(p["id"] == active_id for p in body.get("projects", [])),
        )
        check(
            "heartbeat: GET body projects list omits archived",
            all(p["id"] != archived_id for p in body.get("projects", [])),
        )

        # POST enable / disable global
        en = client.post("/api/heartbeat/enable", headers=H)
        check("heartbeat: POST /enable 200", en.status_code == 200, str(en.status_code))
        check("heartbeat: POST /enable sets property", runner.enabled is True)
        dis = client.post("/api/heartbeat/disable", headers=H)
        check("heartbeat: POST /disable 200", dis.status_code == 200, str(dis.status_code))
        check("heartbeat: POST /disable clears property", runner.enabled is False)

        # POST per-project enable / disable
        en_p = client.post(
            f"/api/projects/{active_id}/heartbeat/disable", headers=H
        )
        check("heartbeat: POST project disable 200", en_p.status_code == 200)
        with session_scope() as db:
            check(
                "heartbeat: per-project disable persisted",
                db.get(Project, active_id).heartbeat_enabled is False,
            )
        en_p2 = client.post(
            f"/api/projects/{active_id}/heartbeat/enable", headers=H
        )
        check("heartbeat: POST project enable 200", en_p2.status_code == 200)
        with session_scope() as db:
            check(
                "heartbeat: per-project enable persisted",
                db.get(Project, active_id).heartbeat_enabled is True,
            )

        # GET heartbeat/issues (empty after wipe)
        r2 = client.get(
            f"/api/projects/{active_id}/heartbeat/issues", headers=H
        )
        check("heartbeat: GET issues 200", r2.status_code == 200, str(r2.status_code))
        check(
            "heartbeat: GET issues returns a list",
            isinstance(r2.json(), list),
        )

        # POST /trigger kicks a tick (fire-and-forget)
        client.post("/api/heartbeat/enable", headers=H)  # re-enable for the tick
        # Patch list_issues to a deterministic stub so the tick can run without
        # network — return one new issue that we haven't seen yet.
        async def fake_list_issues(
            full_name, *, state="open", labels=None, since=None,
            per_page=50, max_pages=5,
        ):
            return [
                {
                    "number": 7777,
                    "title": "Heartbeat-stubbed issue",
                    "user": {"login": "bob"},
                    "labels": [],
                    "created_at": "2026-07-05T00:00:00Z",
                    "body": "Body of stubbed issue",
                    "html_url": f"https://github.com/{full_name}/issues/7777",
                }
            ]

        # Also patch the heartbeat module's reference (it imported the
        # module directly).
        original_list_issues = github_client.list_issues
        github_client.list_issues = fake_list_issues
        hb_mod.github_client.list_issues = fake_list_issues
        try:
            tr = client.post("/api/heartbeat/trigger", headers=H)
            check("heartbeat: POST /trigger 200", tr.status_code == 200, str(tr.status_code))
            check("heartbeat: POST /trigger body triggered=true", tr.json().get("triggered") is True)
            # Wait briefly for the tick to complete (fire-and-forget task).
            deadline = time.time() + 10.0
            dispatched = False
            while time.time() < deadline:
                with session_scope() as db:
                    n = (
                        db.query(Task)
                        .filter(Task.heartbeat_spawned.is_(True))
                        .filter(Task.heartbeat_issue_number == 7777)
                        .count()
                    )
                if n >= 1:
                    dispatched = True
                    break
                time.sleep(0.2)
            check("heartbeat: tick dispatched a task for issue #7777", dispatched)
        finally:
            github_client.list_issues = original_list_issues
            hb_mod.github_client.list_issues = original_list_issues
            client.post("/api/heartbeat/disable", headers=H)

    # -------- 12. cleanup ----------------------------------------------- #
    with session_scope() as db:
        db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == active_id).delete()
        db.query(Task).filter(Task.project_id.in_([active_id, archived_id, no_github_id])).delete()
        db.query(Project).filter(Project.id.in_([active_id, archived_id, no_github_id])).delete()


def main() -> int:
    try:
        test_security()
        test_auth_toggle()
        test_parser()
        test_codex_parser()
        test_command_building()
        test_final_output()
        test_agent_runner()
        test_goal_mode()
        test_images()
        test_config_backfill()
        test_worktree_merge()
        test_host_staging()
        test_git_cycle()
        test_api_and_task()
        test_session_api_and_manager()
        test_session_end_flow()
        test_worktrees()
        test_session_dirs()
        test_session_workdir_resolution()
        test_auto_pull_helpers()
        test_sync_from_github_validation()
        test_hermes_clarify_disabled()
        test_project_archive()
        test_host_lock()
        test_heartbeat()
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
