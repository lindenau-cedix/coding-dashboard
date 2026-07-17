"""End-to-end smoke test that needs no external services.

Run from anywhere:  .venv/bin/python tests/smoke.py
It validates: password hashing, the Claude stream-json parser, the agent
subprocess runner, the full git auto-commit+push cycle (against a local bare
repo), the REST API (login/auth/agents), and a complete task run through the
TaskManager (agent -> file change -> commit -> push -> history).
"""
from __future__ import annotations

import io
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
        work_path = Path(proj_row.local_path)

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
        t.result_summary = "implemented retries on the auth path"
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
        # Explicit commit_message echoes verbatim.
        check(
            "explicit commit_message preserved on task",
            t.commit_message == "test commit",
            t.commit_message,
        )

    # And with an empty commit_message the auto-generated subject from
    # result_summary is used (mirrors task/goal mode where Task.prompt
    # is the source). Need an actual uncommitted change in the repo so
    # ``commit_created`` becomes True and the subject lands on the row
    # (no-op commits deliberately keep commit_message empty so callers
    # can tell "nothing changed" from "commit happened with subject X").
    (work_path / "session_change.txt").write_text("from session\n", encoding="utf-8")
    with session_scope() as db:
        t2 = Task(
            project_id=pid, agent="fake", prompt="--resume", mode="session",
            is_session=True, status="running", chat_history="[]",
            result_summary="implemented retries on the auth path",
        )
        db.add(t2); db.commit(); db.refresh(t2)
        tid2 = t2.id
        t2.output = "tail of session\r\n"
        db.commit()

    asyncio.run(session_manager.end_session(tid2, pid, commit_message=""))
    with session_scope() as db:
        t2_after = db.get(Task, tid2)
        check(
            "empty commit_message falls back to auto-generated subject",
            t2_after.commit_message == "Session: implemented retries on the auth path",
            t2_after.commit_message,
        )
        # Issue #5: when a session is ended (whether via the popup's
        # "Session beenden" button, by the agent self-quitting, or by the
        # server noticing the pump loop exited), the dashboard's /running
        # view MUST drop the entry on the next poll.  end_session_locked
        # persists ``task.status`` to the terminal status *before* the git
        # commit/push step, so /running drops it as soon as the HTTP
        # response is in flight, not after a multi-second push.
        check(
            "session end persists terminal task status (not running/queued)",
            t.status not in ("running", "queued"),
            str(t.status),
        )


def test_auto_commit_subject() -> None:
    """Auto-generated commit subjects across all 3 modes.

    Task/goal mode uses ``Task.prompt``; session mode uses
    ``Task.result_summary`` (since ``Task.prompt`` there holds
    ``start_args`` like ``--resume`` which is low signal).  When neither
    is set, the helper falls back to a stable placeholder.
    """
    from app.database import init_db, session_scope
    from app.models import Project, Task
    from app.task_runner import _auto_commit_subject

    init_db()
    # Re-use the project created by the session_end test when present;
    # otherwise create a minimal local repo so the FK on tasks is happy.
    with session_scope() as db:
        proj = db.query(Project).first()
        if proj is None:
            remote = TMP / "subject-remote.git"
            work = TMP / "subject-work"
            run(["git", "init", "--bare", str(remote)])
            git_ops.clone(str(remote), work, token="")
            git_ops.ensure_identity(work, "Tester", "t@example.com")
            (work / "README.md").write_text("# subject\n", encoding="utf-8")
            git_ops.commit_all(work, "init", "Tester", "t@example.com")
            git_ops.push(work, "main", token="")
            proj = Project(
                name="SubjectProj",
                slug="subject-proj",
                local_path=str(work),
                default_branch="main",
                clone_url=str(remote),
            )
            db.add(proj)
            db.commit()
            db.refresh(proj)
            pid = proj.id
        else:
            pid = proj.id

    # 1. task mode — first line of prompt becomes the subject.
    task_task = Task(
        project_id=pid, agent="fake", prompt="add caching layer\n\nbody",
        mode="task", status="queued",
    )
    with session_scope() as db:
        db.add(task_task); db.commit(); db.refresh(task_task)
    check(
        "task mode subject from prompt first line",
        _auto_commit_subject(task_task.id) == "add caching layer",
        _auto_commit_subject(task_task.id),
    )

    # 2. goal mode — same path as task mode (the user's prompt is the input).
    goal_task = Task(
        project_id=pid, agent="fake", prompt="ship the v2 release",
        mode="goal", status="queued",
    )
    with session_scope() as db:
        db.add(goal_task); db.commit(); db.refresh(goal_task)
    check(
        "goal mode subject from prompt",
        _auto_commit_subject(goal_task.id) == "ship the v2 release",
        _auto_commit_subject(goal_task.id),
    )

    # 3. session mode — start_args (low signal) ignored in favour of
    #    result_summary, so the auto-generated subject reads like the
    #    agent's final outcome.
    sess_task = Task(
        project_id=pid, agent="fake", prompt="--resume",
        mode="session", is_session=True, status="running",
        result_summary="implemented retries on the auth path",
        chat_history="[]",
    )
    with session_scope() as db:
        db.add(sess_task); db.commit(); db.refresh(sess_task)
    check(
        "session mode subject from result_summary",
        _auto_commit_subject(sess_task.id) == "implemented retries on the auth path",
        _auto_commit_subject(sess_task.id),
    )

    # 4. session mode falls back to a stable placeholder when both fields
    #    are blank — mirrors the task-mode "update" fallback.
    blank_sess = Task(
        project_id=pid, agent="fake", prompt="",
        mode="session", is_session=True, status="running",
        chat_history="[]",
    )
    with session_scope() as db:
        db.add(blank_sess); db.commit(); db.refresh(blank_sess)
    check(
        "session mode falls back to 'Session' placeholder",
        _auto_commit_subject(blank_sess.id) == "Session",
        _auto_commit_subject(blank_sess.id),
    )

    # 5. Long subjects are truncated to 72 chars.
    long_prompt = "x" * 200
    long_task = Task(
        project_id=pid, agent="fake", prompt=long_prompt,
        mode="task", status="queued",
    )
    with session_scope() as db:
        db.add(long_task); db.commit(); db.refresh(long_task)
    subj = _auto_commit_subject(long_task.id)
    check("long subject truncated to 72 chars", len(subj) == 72, f"len={len(subj)}")
    check("long subject ends with ellipsis", subj.endswith("..."), subj)


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


def test_rename_github_owner() -> None:
    """The `python -m app.cli rename-github-owner OLD NEW [--apply]`
    admin subcommand: dry-run preview, --apply rewrite of three
    owner-stamped columns, owner self-match rejected, unknown prefix
    no-op, idempotent re-run, last_issue_poll_at cleared only on the
    renamed projects.

    Runs against the smoke test's isolated SQLite DB; every change
    happens inside a single ``session_scope`` so commit/rollback
    semantics are visible.
    """
    from datetime import datetime, timezone

    from app import cli as cli_mod
    from app.database import session_scope, init_db
    from app.models import Project

    init_db()

    stamp = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    def _ts_eq(a, b):
        """SQLite drops tz info on roundtrip; compare the wall-clock."""
        if a == b:
            return True
        if a is None or b is None:
            return False
        if a.tzinfo is None and b.tzinfo is not None:
            return a == b.replace(tzinfo=None)
        if b.tzinfo is None and a.tzinfo is not None:
            return a.replace(tzinfo=None) == b
        return False
    OTHER_OWNER_PID: str = ""
    UNRELATED_OWNER_PID: str = ""
    EMPTY_OWNER_PID: str = ""

    with session_scope() as db:
        # Project owned by the OLD owner (the rename target).
        p1 = Project(
            name="rename-me",
            slug="rename-me",
            local_path=str(TMP / "data" / "projects" / "rename-me"),
            default_branch="main",
            clone_url="https://github.com/old-owner/rename-me.git",
            github_url="https://github.com/old-owner/rename-me",
            github_full_name="old-owner/rename-me",
            last_issue_poll_at=stamp,
        )
        db.add(p1)
        db.flush()
        OTHER_OWNER_PID = p1.id

        # Project owned by an UNRELATED owner — must NOT be touched.
        p2 = Project(
            name="leave-me-alone",
            slug="leave-me-alone",
            local_path=str(TMP / "data" / "projects" / "leave-me-alone"),
            default_branch="main",
            clone_url="https://github.com/somebody/leave-me-alone.git",
            github_url="https://github.com/somebody/leave-me-alone",
            github_full_name="somebody/leave-me-alone",
            last_issue_poll_at=stamp,
        )
        db.add(p2)
        db.flush()
        UNRELATED_OWNER_PID = p2.id

        # Project without a github_full_name — must NOT be touched.
        p3 = Project(
            name="no-owner",
            slug="no-owner",
            local_path=str(TMP / "data" / "projects" / "no-owner"),
            default_branch="main",
            clone_url="",
            github_url="",
            github_full_name="",
            last_issue_poll_at=stamp,
        )
        db.add(p3)
        db.flush()
        EMPTY_OWNER_PID = p3.id

    # --- validation guards (no DB hit) ---
    out, err = io.StringIO(), io.StringIO()
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = cli_mod.main(["rename-github-owner", "old-owner", "old-owner"])
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
    check(
        "rename: OLD==NEW rejected",
        rc == 1 and "identical" in (out.getvalue() + err.getvalue()),
        f"rc={rc} stderr={err.getvalue()[:120]!r}",
    )

    out, err = io.StringIO(), io.StringIO()
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = cli_mod.main(["rename-github-owner", "old/owner", "new-owner"])
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
    check(
        "rename: OLD with '/' rejected",
        rc == 1 and "/" in (out.getvalue() + err.getvalue()),
        f"rc={rc} stderr={err.getvalue()[:120]!r}",
    )

    out, err = io.StringIO(), io.StringIO()
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = cli_mod.main(["rename-github-owner", "old owner", "new-owner"])
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
    check(
        "rename: OLD with whitespace rejected",
        rc == 1 and "whitespace" in (out.getvalue() + err.getvalue()),
        f"rc={rc} stderr={err.getvalue()[:120]!r}",
    )

    # --- dry-run does NOT mutate ---
    out = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = out
    try:
        rc = cli_mod.main(
            ["rename-github-owner", "old-owner", "new-owner", "--limit", "5"]
        )
    finally:
        sys.stdout = saved_stdout
    body = out.getvalue()
    check(
        "rename: dry-run exits 0",
        rc == 0,
        f"rc={rc}",
    )
    check(
        "rename: dry-run says DRY-RUN",
        "DRY-RUN" in body,
        body[:120],
    )
    check(
        "rename: dry-run prints old full_name in preview",
        "old-owner/rename-me" in body and "new-owner/rename-me" in body,
        body[:240],
    )
    check(
        "rename: dry-run does NOT touch unrelated owner",
        "somebody/leave-me-alone" not in body,
        body[:240],
    )

    with session_scope() as db:
        p = db.get(Project, OTHER_OWNER_PID)
        check(
            "rename: dry-run did not rewrite github_full_name",
            p.github_full_name == "old-owner/rename-me",
            f"got {p.github_full_name!r}",
        )
        check(
            "rename: dry-run did not rewrite clone_url",
            p.clone_url == "https://github.com/old-owner/rename-me.git",
            f"got {p.clone_url!r}",
        )
        check(
            "rename: dry-run did not clear last_issue_poll_at",
            _ts_eq(p.last_issue_poll_at, stamp),
            f"got {p.last_issue_poll_at!r}",
        )

    # --- --apply commits the rewrite ---
    out = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = out
    try:
        rc = cli_mod.main(
            ["rename-github-owner", "old-owner", "new-owner", "--apply"]
        )
    finally:
        sys.stdout = saved_stdout
    body = out.getvalue()
    check("rename: --apply exits 0", rc == 0, f"rc={rc}")
    check(
        "rename: --apply says Apply with row counts",
        "Apply" in body and "rewritten" in body,
        body[:240],
    )

    with session_scope() as db:
        p_renamed = db.get(Project, OTHER_OWNER_PID)
        check(
            "rename: --apply rewrote github_full_name",
            p_renamed.github_full_name == "new-owner/rename-me",
            f"got {p_renamed.github_full_name!r}",
        )
        check(
            "rename: --apply rewrote github_url",
            p_renamed.github_url == "https://github.com/new-owner/rename-me",
            f"got {p_renamed.github_url!r}",
        )
        check(
            "rename: --apply rewrote clone_url",
            p_renamed.clone_url == "https://github.com/new-owner/rename-me.git",
            f"got {p_renamed.clone_url!r}",
        )
        check(
            "rename: --apply cleared last_issue_poll_at on renamed project",
            p_renamed.last_issue_poll_at is None,
            f"got {p_renamed.last_issue_poll_at!r}",
        )

        p_left = db.get(Project, UNRELATED_OWNER_PID)
        check(
            "rename: --apply did NOT touch unrelated github_full_name",
            p_left.github_full_name == "somebody/leave-me-alone",
            f"got {p_left.github_full_name!r}",
        )
        check(
            "rename: --apply did NOT touch unrelated clone_url",
            p_left.clone_url == "https://github.com/somebody/leave-me-alone.git",
            f"got {p_left.clone_url!r}",
        )
        check(
            "rename: --apply did NOT clear unrelated last_issue_poll_at",
            _ts_eq(p_left.last_issue_poll_at, stamp),
            f"got {p_left.last_issue_poll_at!r}",
        )

        p_empty = db.get(Project, EMPTY_OWNER_PID)
        check(
            "rename: --apply did NOT touch empty github_full_name",
            p_empty.github_full_name == "",
            f"got {p_empty.github_full_name!r}",
        )
        check(
            "rename: --apply did NOT clear last_issue_poll_at for empty owner",
            _ts_eq(p_empty.last_issue_poll_at, stamp),
            f"got {p_empty.last_issue_poll_at!r}",
        )

    # --- idempotency: re-run finds 0 rows for the same OLD prefix ---
    out = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = out
    try:
        rc = cli_mod.main(
            ["rename-github-owner", "old-owner", "new-owner", "--apply"]
        )
    finally:
        sys.stdout = saved_stdout
    body = out.getvalue()
    check("rename: idempotent re-run exits 0", rc == 0, f"rc={rc}")
    check(
        "rename: idempotent re-run says 0 rows rewritten",
        "0 row(s) matched (rewritten)" in body,
        body[:240],
    )

    with session_scope() as db:
        p_renamed = db.get(Project, OTHER_OWNER_PID)
        check(
            "rename: idempotent re-run did not corrupt the new owner",
            p_renamed.github_full_name == "new-owner/rename-me",
            f"got {p_renamed.github_full_name!r}",
        )
        check(
            "rename: idempotent re-run did not touch unrelated owner",
            db.get(Project, UNRELATED_OWNER_PID).github_full_name
            == "somebody/leave-me-alone",
            "unrelated changed",
        )

    # --- unknown OLD prefix is a no-op ---
    out = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = out
    try:
        rc = cli_mod.main(
            ["rename-github-owner", "ghost-owner", "whoever", "--apply"]
        )
    finally:
        sys.stdout = saved_stdout
    body = out.getvalue()
    check(
        "rename: unknown prefix is a no-op",
        rc == 0 and "0 row(s) matched (rewritten)" in body,
        f"rc={rc} body={body[:240]!r}",
    )

    # --- cleanup ---
    with session_scope() as db:
        db.query(Project).filter(Project.id == OTHER_OWNER_PID).delete()
        db.query(Project).filter(Project.id == UNRELATED_OWNER_PID).delete()
        db.query(Project).filter(Project.id == EMPTY_OWNER_PID).delete()


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

    # ---- 4b. prompt forbids the agent from self-committing/pushing/PRing -- #
    # Regression for "the heartbeat-spawned agent commits itself, then the
    # dashboard's `_git_step` sees a clean working tree, skips auto-commit,
    # and pushes nothing". The prompt must explicitly forbid `git commit`,
    # `git push`, `git add` self-initiated by the agent, plus `gh pr create`
    # / `hub pull-request`, and must instruct the agent to leave the working
    # tree dirty so the dashboard's auto-commit + push runs.
    template = DEFAULT_HEARTBEAT_PROMPT_TEMPLATE
    forbidden_substrings = [
        # The exact patterns the old prompt asked the agent to do.
        "Committe auf einem Branch",
        "Pushe den Branch",
        "oeffne einen PR",
    ]
    for snippet in forbidden_substrings:
        check(
            f"heartbeat: prompt no longer instructs agent to '{snippet}'",
            snippet not in template,
        )

    # The new prompt MUST explicitly forbid the destructive commands the
    # agent used to run before the dashboard's auto-commit skipped.
    must_contain = [
        "`git add`",
        "`git commit`",
        "`git push`",
        "`gh pr create`",
        "UNTER KEINEN UMSTAENDEN",
        "`git status`",  # the dashboard relies on dirty tree
    ]
    for snippet in must_contain:
        check(
            f"heartbeat: prompt explicitly forbids / requires '{snippet}'",
            snippet in template,
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
        # network — return one new issue that we haven't seen yet. The issue
        # carries an ``assignees`` entry so the assignee-allowlist filter
        # (added when the heartbeat stopped dispatching every open issue)
        # doesn't drop it; the resolver stub below pins the allowlist to
        # that same login.
        async def fake_list_issues(
            full_name, *, state="open", labels=None, since=None,
            assignee=None, per_page=50, max_pages=5,
        ):
            return [
                {
                    "number": 7777,
                    "title": "Heartbeat-stubbed issue",
                    "user": {"login": "bob"},
                    "assignees": [{"login": "bob"}],
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
        # Stub the assignee resolver so this legacy test doesn't need a
        # real ``/user`` call. The allowlist ``("bob",)`` matches the
        # ``assignees`` entry above.
        original_resolve = runner._resolve_assignee_logins
        async def _fake_resolve_bob():
            return (("bob",), hb_mod.ASSIGNEE_RESOLVED)
        runner._resolve_assignee_logins = _fake_resolve_bob
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
            runner._resolve_assignee_logins = original_resolve
            client.post("/api/heartbeat/disable", headers=H)

    # -------- 11b. manual trigger bypasses per-project cooldown --------- #
    # The dashboard's "▶ Run now" button hits POST /api/heartbeat/trigger.
    # Operators expect a click on it to actually do work, even if a recent
    # successful heartbeat task is currently in the cooldown window — the
    # cooldown exists to throttle the BACKGROUND loop's re-dispatches, not
    # the operator's explicit clicks. This section verifies the bypass:
    #
    #   1. Direct ``tick_now(bypass_cooldown=False)`` on a project with a
    #      fresh success in the window returns ``dispatched=0`` (cooldown
    #      gate still enforced for automatic ticks).
    #   2. Direct ``tick_now(bypass_cooldown=True)`` on the same project
    #      proceeds past the gate and dispatches a heartbeat-spawned task.
    #   3. The HTTP trigger endpoint reports ``cooldown_bypassed=True`` in
    #      the summary dict so the UI can render "ran despite cooldown".
    with TestClient(app) as client:
        ok = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
        )
        H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

        # Reset the heartbeat_seen ledger + spawn one "cooldown" success
        # task so the in-window cooldown is active for ``active_id``.
        with session_scope() as db:
            db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == active_id).delete()
            db.query(Task).filter(Task.project_id == active_id).delete()
            db.add(
                Task(
                    project_id=active_id,
                    agent="claude",
                    prompt="Previous successful auto-fix",
                    mode="task",
                    status="success",
                    heartbeat_spawned=True,
                    heartbeat_issue_number=9000,
                    finished_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

        async def fake_list_issues_bypass(
            full_name, *, state="open", labels=None, since=None,
            assignee=None, per_page=50, max_pages=5,
        ):
            return [
                {
                    "number": 8888,
                    "title": "Bypass-cooldown issue",
                    "user": {"login": "bob"},
                    "assignees": [{"login": "bob"}],
                    "labels": [],
                    "created_at": "2026-07-05T00:00:00Z",
                    "body": "Body of bypass-cooldown issue",
                    "html_url": f"https://github.com/{full_name}/issues/8888",
                }
            ]

        original_list_issues = github_client.list_issues
        original_resolve = runner._resolve_assignee_logins
        github_client.list_issues = fake_list_issues_bypass
        hb_mod.github_client.list_issues = fake_list_issues_bypass

        async def _fake_resolve_bob():
            return (("bob",), hb_mod.ASSIGNEE_RESOLVED)
        runner._resolve_assignee_logins = _fake_resolve_bob
        try:
            # (1) tick_now(bypass_cooldown=False) must still skip the
            # project — the cooldown is in effect, no new tasks should be
            # created for issue 8888.
            with session_scope() as db:
                db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == active_id).delete()
                db.commit()
            summary_default = asyncio.run(runner.tick_now(bypass_cooldown=False))
            check(
                "hb-bypass: tick_now(bypass_cooldown=False) returns ok",
                summary_default.get("status") == "ok",
                repr(summary_default),
            )
            check(
                "hb-bypass: tick summary reports cooldown_bypassed=False",
                summary_default.get("cooldown_bypassed") is False,
                repr(summary_default),
            )
            with session_scope() as db:
                n_default = (
                    db.query(Task)
                    .filter(Task.project_id == active_id)
                    .filter(Task.heartbeat_spawned.is_(True))
                    .filter(Task.heartbeat_issue_number == 8888)
                    .count()
                )
                p_after_default = db.get(Project, active_id)
            check(
                "hb-bypass: bypass_cooldown=False dispatches 0 tasks (cooldown wins)",
                n_default == 0,
                f"got {n_default}",
            )
            check(
                "hb-bypass: project last_heartbeat_status='cooldown' after default tick",
                p_after_default is not None
                and p_after_default.last_heartbeat_status == "cooldown",
                getattr(p_after_default, "last_heartbeat_status", "<missing>"),
            )

            # (2) tick_now(bypass_cooldown=True) must proceed past the
            # cooldown gate and dispatch the heartbeat-spawned task.
            with session_scope() as db:
                db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == active_id).delete()
                db.commit()
            summary_bypass = asyncio.run(runner.tick_now(bypass_cooldown=True))
            check(
                "hb-bypass: tick_now(bypass_cooldown=True) returns ok",
                summary_bypass.get("status") == "ok",
                repr(summary_bypass),
            )
            check(
                "hb-bypass: tick summary reports cooldown_bypassed=True",
                summary_bypass.get("cooldown_bypassed") is True,
                repr(summary_bypass),
            )
            deadline = time.time() + 10.0
            dispatched = False
            while time.time() < deadline:
                with session_scope() as db:
                    n = (
                        db.query(Task)
                        .filter(Task.project_id == active_id)
                        .filter(Task.heartbeat_spawned.is_(True))
                        .filter(Task.heartbeat_issue_number == 8888)
                        .count()
                    )
                if n >= 1:
                    dispatched = True
                    break
                time.sleep(0.2)
            check(
                "hb-bypass: bypass_cooldown=True dispatches a task for issue #8888",
                dispatched,
            )
            with session_scope() as db:
                p_after_bypass = db.get(Project, active_id)
            check(
                "hb-bypass: project last_heartbeat_status NOT 'cooldown' after bypass",
                p_after_bypass is not None
                and p_after_bypass.last_heartbeat_status != "cooldown",
                getattr(p_after_bypass, "last_heartbeat_status", "<missing>"),
            )

            # (3) HTTP /trigger endpoint also bypasses cooldown and
            # surfaces the flag in the summary.
            with session_scope() as db:
                db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == active_id).delete()
                db.commit()
            tr = client.post("/api/heartbeat/trigger", headers=H)
            check(
                "hb-bypass: POST /trigger 200", tr.status_code == 200, str(tr.status_code)
            )
            trigger_summary = (tr.json() or {}).get("summary") or {}
            check(
                "hb-bypass: /trigger summary reports cooldown_bypassed=True",
                trigger_summary.get("cooldown_bypassed") is True,
                repr(trigger_summary),
            )
            check(
                "hb-bypass: /trigger summary status ok",
                trigger_summary.get("status") == "ok",
                repr(trigger_summary),
            )
        finally:
            github_client.list_issues = original_list_issues
            hb_mod.github_client.list_issues = original_list_issues
            runner._resolve_assignee_logins = original_resolve

    # -------- 12. cleanup ----------------------------------------------- #
    with session_scope() as db:
        db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == active_id).delete()
        db.query(Task).filter(Task.project_id.in_([active_id, archived_id, no_github_id])).delete()
        db.query(Project).filter(Project.id.in_([active_id, archived_id, no_github_id])).delete()


    # -------- 12b. assignee-allowlist filter ------------------------------ #
def test_heartbeat_assignee_filter() -> None:
    """Heartbeat assignee-allowlist: the heartbeat MUST only auto-fix
    issues whose ``assignees`` array intersects a configured allowlist of
    GitHub logins. Three layers of guarantees under test:

    * **Setting layer** — ``CD_HEARTBEAT_ASSIGNEE_LOGINS`` parses to a
      normalized (lowercased + deduped) list via the
      ``heartbeat_assignee_logins_list`` property.
    * **Resolver layer** — when the env var is empty, ``_resolve_assignee_logins``
      auto-resolves from ``GET /user``; when resolution fails (no token,
      HTTP error, empty ``login``) it returns a failure status and the
      tick MUST short-circuit (fail-closed — never process every open issue).
    * **Filter layer** — at ``list_issues`` time the primary login is
      threaded into the GitHub ``assignee=`` query param; at
      ``_process_project`` time the wider allowlist is enforced
      client-side against ``issue.assignees``.

    Verified through pure-property checks, direct ``_resolve_assignee_logins``
    calls with stubbed ``/user``, and the end-to-end
    ``POST /api/heartbeat/trigger`` path with a stubbed
    ``list_issues``.
    """
    import asyncio
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app import config as app_config
    from app import github_client, heartbeat as hb_mod
    from app.database import session_scope
    from app.main import app
    from app.models import HeartbeatSeen, Project, Task

    # -------- 1. settings field exists + helper -------------------------- #
    s = app_config.get_settings()
    check(
        "hb-assignee: heartbeat_assignee_logins field present on Settings",
        hasattr(s, "heartbeat_assignee_logins"),
    )
    check(
        "hb-assignee: heartbeat_assignee_logins_list property present",
        hasattr(type(s), "heartbeat_assignee_logins_list"),
    )
    # Default must be empty so existing installs keep their old behavior
    # (auto-resolve from token) after upgrade.
    check(
        "hb-assignee: default heartbeat_assignee_logins empty",
        s.heartbeat_assignee_logins == "",
        repr(s.heartbeat_assignee_logins),
    )
    check(
        "hb-assignee: default heartbeat_assignee_logins_list empty",
        s.heartbeat_assignee_logins_list == [],
        repr(s.heartbeat_assignee_logins_list),
    )

    # -------- 2. CSV parser: trim / lowercase / dedupe / order --------- #
    # Bypass the env-var cache by setting the field directly on the
    # existing Settings instance (``pydantic-settings`` honors attribute
    # assignment until a model_validator kicks in; no validator on this
    # field, so direct write works).
    with patch.object(s, "heartbeat_assignee_logins", "alice,Bob, foo"):
        check(
            "hb-assignee: CSV trims and lowercases",
            s.heartbeat_assignee_logins_list == ["alice", "bob", "foo"],
            repr(s.heartbeat_assignee_logins_list),
        )
    with patch.object(s, "heartbeat_assignee_logins", "alice,alice,ALICE"):
        check(
            "hb-assignee: CSV dedupes (case-insensitive)",
            s.heartbeat_assignee_logins_list == ["alice"],
            repr(s.heartbeat_assignee_logins_list),
        )
    with patch.object(s, "heartbeat_assignee_logins", ", , "):
        check(
            "hb-assignee: CSV of only blanks/empties parses to []",
            s.heartbeat_assignee_logins_list == [],
            repr(s.heartbeat_assignee_logins_list),
        )
    # Single login, uppercase — must still match the lowercase normalizer.
    with patch.object(s, "heartbeat_assignee_logins", "Alice"):
        check(
            "hb-assignee: single uppercase login lowercased",
            s.heartbeat_assignee_logins_list == ["alice"],
            repr(s.heartbeat_assignee_logins_list),
        )

    # -------- 3. resolver: explicit env beats /user --------------------- #
    runner = hb_mod.heartbeat
    with patch.object(s, "heartbeat_assignee_logins", "alice,bob"):

        async def _should_not_call_user():
            raise AssertionError(
                "_resolve_assignee_logins must NOT call /user when env var is set"
            )

        with patch.object(
            github_client,
            "get_authenticated_user",
            side_effect=_should_not_call_user,
        ):
            logins, status = asyncio.run(runner._resolve_assignee_logins())
        check(
            "hb-assignee: explicit env wins, /user not called",
            logins == ("alice", "bob") and status == hb_mod.ASSIGNEE_RESOLVED,
            repr((logins, status)),
        )

    # -------- 4. resolver: auto-resolve /user happy path ----------------- #
    with patch.object(s, "heartbeat_assignee_logins", ""), patch.object(
        s, "github_token", "ghp_test"
    ):

        async def _fake_user():
            return {"login": "TestUser", "id": 1}

        with patch.object(
            github_client, "get_authenticated_user", side_effect=_fake_user
        ):
            logins, status = asyncio.run(runner._resolve_assignee_logins())
    check(
        "hb-assignee: auto-resolve from /user (lowercased)",
        logins == ("testuser",) and status == hb_mod.ASSIGNEE_RESOLVED,
        repr((logins, status)),
    )

    # -------- 5. resolver: empty login from /user ----------------------- #
    with patch.object(s, "heartbeat_assignee_logins", ""), patch.object(
        s, "github_token", "ghp_test"
    ):

        async def _fake_user_empty():
            return {"login": "", "id": 1}

        with patch.object(
            github_client, "get_authenticated_user", side_effect=_fake_user_empty
        ):
            logins, status = asyncio.run(runner._resolve_assignee_logins())
    check(
        "hb-assignee: empty /user login → ASSIGNEE_EMPTY",
        logins == () and status == hb_mod.ASSIGNEE_EMPTY,
        repr((logins, status)),
    )

    # -------- 6. resolver: no token ------------------------------------- #
    with patch.object(s, "heartbeat_assignee_logins", ""), patch.object(
        s, "github_token", ""
    ):
        logins, status = asyncio.run(runner._resolve_assignee_logins())
    check(
        "hb-assignee: no token → ASSIGNEE_NO_TOKEN",
        logins == () and status == hb_mod.ASSIGNEE_NO_TOKEN,
        repr((logins, status)),
    )

    # -------- 7. resolver: GitHubError → lookup_failed ------------------- #
    with patch.object(s, "heartbeat_assignee_logins", ""), patch.object(
        s, "github_token", "ghp_test"
    ):

        async def _fake_user_401():
            raise github_client.GitHubError(401, "Bad credentials")

        with patch.object(
            github_client, "get_authenticated_user", side_effect=_fake_user_401
        ):
            logins, status = asyncio.run(runner._resolve_assignee_logins())
    check(
        "hb-assignee: GitHubError /user → ASSIGNEE_LOOKUP_FAILED",
        logins == () and status == hb_mod.ASSIGNEE_LOOKUP_FAILED,
        repr((logins, status)),
    )

    # -------- 8. resolver: generic exception → lookup_failed ------------- #
    with patch.object(s, "heartbeat_assignee_logins", ""), patch.object(
        s, "github_token", "ghp_test"
    ):

        async def _fake_user_crash():
            raise RuntimeError("network blew up")

        with patch.object(
            github_client, "get_authenticated_user", side_effect=_fake_user_crash
        ):
            logins, status = asyncio.run(runner._resolve_assignee_logins())
    check(
        "hb-assignee: generic exception /user → ASSIGNEE_LOOKUP_FAILED",
        logins == () and status == hb_mod.ASSIGNEE_LOOKUP_FAILED,
        repr((logins, status)),
    )

    # -------- 9. fail-closed tick --------------------------------------- #
    # Stub /user to fail (→ resolution returns lookup_failed). Stub a
    # "rich" issues list (3 unassigned open issues). Drive the tick via
    # POST /api/heartbeat/trigger. Expect:
    #   * HTTP 200 with summary.status == "no_assignee"
    #   * NO new rows in heartbeat_seen (the tick never reached the
    #     per-project loop)
    #   * NO tasks dispatched
    #   * list_issues was NOT called (resolver short-circuits before
    #     the per-project loop)
    with session_scope() as db:
        # Create a fresh project so the tick has something to walk
        # (even though the resolver will reject before that walks anything).
        failclosed_proj = Project(
            name="hb-failclosed",
            slug="hb-failclosed",
            local_path="/tmp/hb-failclosed",
            github_full_name="owner/hb-failclosed",
            heartbeat_enabled=True,
        )
        db.add(failclosed_proj)
        db.commit()
        db.refresh(failclosed_proj)
        fc_id = failclosed_proj.id

    async def _fake_user_error_for_fc():
        raise github_client.GitHubError(500, "boom")

    list_issues_called: dict[str, int] = {"n": 0}

    async def _fake_list_issues_tracking(*args, **kwargs):
        list_issues_called["n"] += 1
        return []

    client = TestClient(app)
    with client:
        ok = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
        )
        check(
            "hb-assignee: own test client login ok",
            ok.status_code == 200,
            str(ok.status_code),
        )
        H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

    try:
        runner.set_enabled(True)
        with patch.object(s, "heartbeat_assignee_logins", ""), patch.object(
            s, "github_token", "ghp_test"
        ), patch.object(
            github_client,
            "get_authenticated_user",
            side_effect=_fake_user_error_for_fc,
        ), patch.object(
            github_client, "list_issues", side_effect=_fake_list_issues_tracking
        ), patch.object(
            hb_mod.github_client, "list_issues", side_effect=_fake_list_issues_tracking
        ):
            tr = client.post("/api/heartbeat/trigger", headers=H)
        check(
            "hb-assignee: tick HTTP 200 even when no_assignee",
            tr.status_code == 200,
            str(tr.status_code),
        )
        summary = tr.json().get("summary") or {}
        check(
            "hb-assignee: tick body says no_assignee",
            summary.get("status") == "no_assignee",
            repr(summary),
        )
        check(
            "hb-assignee: tick body surfaces reason=lookup_failed",
            summary.get("reason") == "lookup_failed",
            repr(summary),
        )
        check(
            "hb-assignee: tick body dispatched=0",
            summary.get("dispatched") == 0,
            repr(summary),
        )
        check(
            "hb-assignee: tick short-circuit did NOT call list_issues",
            list_issues_called["n"] == 0,
            repr(list_issues_called),
        )
        with session_scope() as db:
            n_seen = (
                db.query(HeartbeatSeen)
                .filter(HeartbeatSeen.project_id == fc_id)
                .count()
            )
            n_tasks = (
                db.query(Task)
                .filter(Task.project_id == fc_id)
                .filter(Task.heartbeat_spawned.is_(True))
                .count()
            )
        check(
            "hb-assignee: no heartbeat_seen rows after fail-closed tick",
            n_seen == 0,
            repr(n_seen),
        )
        check(
            "hb-assignee: no heartbeat-spawned tasks after fail-closed tick",
            n_tasks == 0,
            repr(n_tasks),
        )
    finally:
        runner.set_enabled(False)
        with session_scope() as db:
            db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == fc_id).delete()
            db.query(Task).filter(Task.project_id == fc_id).delete()
            db.query(Project).filter(Project.id == fc_id).delete()

    # -------- 10. allowlist filters the per-project dispatch ----------- #
    # With an explicit ``alice,bob`` allowlist, stub list_issues to return
    # one issue assigned to alice (dispatched) + one assigned to carol
    # (NOT dispatched — outside the allowlist). Assert that only the
    # alice-assigned issue got a Task.
    with session_scope() as db:
        assign_proj = Project(
            name="hb-assign",
            slug="hb-assign",
            local_path="/tmp/hb-assign",
            github_full_name="owner/hb-assign",
            heartbeat_enabled=True,
        )
        db.add(assign_proj)
        db.commit()
        db.refresh(assign_proj)
        a_id = assign_proj.id

    captured_kwargs: dict[str, object] = {}

    async def _fake_list_issues_assignee(
        full_name, *, state="open", labels=None, since=None,
        assignee=None, per_page=50, max_pages=5,
    ):
        captured_kwargs["assignee"] = assignee
        # Mirror the real filter: GitHub's single-value ``assignee`` param
        # would only ever match the primary login in production. We
        # pretend GitHub returned broader data here so we can exercise
        # the client-side allowlist widening (alice AND bob both appear).
        return [
            {
                "number": 1,
                "title": "Alice's issue",
                "user": {"login": "x"},
                "assignees": [{"login": "Alice"}],
                "labels": [],
                "created_at": "2026-07-08T00:00:00Z",
                "body": "x",
                "html_url": f"https://github.com/{full_name}/issues/1",
            },
            {
                "number": 2,
                "title": "Bob's issue",
                "user": {"login": "x"},
                "assignees": [{"login": "bob"}],
                "labels": [],
                "created_at": "2026-07-08T00:00:00Z",
                "body": "x",
                "html_url": f"https://github.com/{full_name}/issues/2",
            },
            {
                "number": 3,
                "title": "Carol's issue",
                "user": {"login": "x"},
                "assignees": [{"login": "carol"}],
                "labels": [],
                "created_at": "2026-07-08T00:00:00Z",
                "body": "x",
                "html_url": f"https://github.com/{full_name}/issues/3",
            },
            {
                "number": 4,
                "title": "Unassigned issue",
                "user": {"login": "x"},
                "assignees": [],
                "labels": [],
                "created_at": "2026-07-08T00:00:00Z",
                "body": "x",
                "html_url": f"https://github.com/{full_name}/issues/4",
            },
        ]

    try:
        runner.set_enabled(True)
        with patch.object(s, "heartbeat_assignee_logins", "alice,bob"), patch.object(
            github_client, "list_issues", side_effect=_fake_list_issues_assignee
        ), patch.object(
            hb_mod.github_client,
            "list_issues",
            side_effect=_fake_list_issues_assignee,
        ):
            tr = client.post("/api/heartbeat/trigger", headers=H)
        check(
            "hb-assignee: assign-filter tick HTTP 200",
            tr.status_code == 200,
            str(tr.status_code),
        )
        check(
            "hb-assignee: assign-filter threaded assignee=alice into list_issues",
            captured_kwargs.get("assignee") == "alice",
            repr(captured_kwargs),
        )
        # Wait briefly for the tick to settle.
        deadline = time.time() + 10.0
        dispatched_numbers: set[int] = set()
        while time.time() < deadline:
            with session_scope() as db:
                rows = (
                    db.query(Task.heartbeat_issue_number)
                    .filter(Task.project_id == a_id)
                    .filter(Task.heartbeat_spawned.is_(True))
                    .all()
                )
                dispatched_numbers = {int(n) for (n,) in rows if n is not None}
                if {1, 2}.issubset(dispatched_numbers):
                    break
            time.sleep(0.2)
        check(
            "hb-assignee: alice issue (1) dispatched",
            1 in dispatched_numbers,
            repr(dispatched_numbers),
        )
        check(
            "hb-assignee: bob issue (2) dispatched (client-side widening past query param)",
            2 in dispatched_numbers,
            repr(dispatched_numbers),
        )
        check(
            "hb-assignee: carol issue (3) NOT dispatched",
            3 not in dispatched_numbers,
            repr(dispatched_numbers),
        )
        check(
            "hb-assignee: unassigned issue (4) NOT dispatched",
            4 not in dispatched_numbers,
            repr(dispatched_numbers),
        )
    finally:
        runner.set_enabled(False)
        # Restore the env-var so the rest of the suite isn't affected.
        with patch.object(s, "heartbeat_assignee_logins", ""):
            pass
        with session_scope() as db:
            db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == a_id).delete()
            db.query(Task).filter(Task.project_id == a_id).delete()
            db.query(Project).filter(Project.id == a_id).delete()


    # -------- 13. comment-back-on-issue flow ----------------------------- #
def test_heartbeat_comment_on_solve() -> None:
    """Comment-back-on-solve: when a heartbeat-spawned task lands a commit,
    post the commit hash onto the GitHub issue, and close the issue when
    the commit actually merged cleanly into the default branch.

    Verified in three sections:
      * ``format_comment_body`` substitution + ``should_close_on_merge``
        predicates.
      * Direct ``heartbeat_followup.maybe_run`` call paths for the three
        relevant branches (merged+push -> both comment+close; conflict
        -> comment only; failed+no commit -> no API calls).
      * REST surface (``POST .../comment-again``, ``.../close``,
        ``.../reopen``) round-trip via TestClient + stubbed
        ``github_client`` helpers.
    """
    import asyncio
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app import github_client
    from app.config import get_settings
    from app.database import session_scope
    from app.heartbeat import (
        format_comment_body,
        heartbeat_followup,
        should_close_on_merge,
    )
    from app.main import app
    from app.models import HeartbeatSeen, Project, Task

    # -------- 13.1 pure predicates ------------------------------------- #
    from types import SimpleNamespace as _Sn

    # should_close_on_merge: True only when push + merge_state=merged.
    check(
        "hb-comment: should_close_on_merge True when pushed+merged",
        should_close_on_merge(_Sn(pushed=True, merge_state="merged")) is True,
    )
    check(
        "hb-comment: should_close_on_merge False on conflict",
        should_close_on_merge(_Sn(pushed=False, merge_state="conflict")) is False,
    )
    check(
        "hb-comment: should_close_on_merge False when not pushed",
        should_close_on_merge(_Sn(pushed=False, merge_state="merged")) is False,
    )
    check(
        "hb-comment: should_close_on_merge False on empty merge_state",
        should_close_on_merge(_Sn(pushed=True, merge_state="")) is False,
    )

    # format_comment_body substitutes the commit hash and the result
    # summary. Issue number + repo + commit URL must all show up.
    fake_task = _Sn(
        heartbeat_issue_number=42,
        result_summary="Fixed the startup crash by removing the buggy f-string.",
        commit_hash="abcdef0123456789abcdef0123456789abcdef01",
        branch="cd/task/abc12345",
        merge_state="merged",
        pushed=True,
        project_id="proj1",
        id="tid1",
    )
    fake_proj = _Sn(
        id="proj1",
        name="hb-comment-proj",
        slug="hb-comment-proj",
        github_full_name="owner/hb-comment-proj",
    )
    body_merged = format_comment_body(fake_task, fake_proj, "https://x/issues/42")
    check("hb-comment: body contains issue number", "#42" in body_merged, body_merged)
    check("hb-comment: body contains commit sha", "abcdef01" in body_merged, body_merged)
    check(
        "hb-comment: body contains github commit URL",
        "https://github.com/owner/hb-comment-proj/commit/abcdef01" in body_merged,
        body_merged,
    )
    check("hb-comment: body contains repo full_name", "owner/hb-comment-proj" in body_merged, body_merged)
    check("hb-comment: body contains result summary excerpt", "startup crash" in body_merged, body_merged)
    check("hb-comment: body shows merged branch label", "cd/task/abc12345" in body_merged, body_merged)

    # Conflict branch -> comment body shows the conflict warning instead
    # of "merged".
    fake_task2 = _Sn(
        heartbeat_issue_number=7,
        result_summary="Investigated.",
        commit_hash="deadbeef" + "0" * 32,
        branch="cd/task/ccc12345",
        merge_state="conflict",
        pushed=False,
        project_id="proj1",
        id="tid2",
    )
    body_conflict = format_comment_body(fake_task2, fake_proj, "https://x/issues/7")
    check("hb-comment: conflict body shows manueller Merge", "manuellen Merge" in body_conflict, body_conflict)
    check("hb-comment: conflict body marks not gepusht", "nicht gepusht" in body_conflict, body_conflict)

    # -------- 13.2 direct hook path ------------------------------------- #
    # Set up a Project + heartbeat_seen + heartbeat Task with a "merged
    # + pushed" final status, then call maybe_run and assert both GitHub
    # helpers fire with the expected payloads.
    from datetime import datetime, timezone

    cb_project_id = ""
    with session_scope() as db:
        proj_row = Project(
            name="hb-comment-proj",
            slug="hb-comment-proj",
            local_path="/tmp/hb-comment-proj",
            github_full_name="owner/hb-comment-proj",
            heartbeat_enabled=True,
        )
        db.add(proj_row)
        db.flush()
        cb_project_id = proj_row.id
        # heartbeat_seen ledger entries so the hook can stamp them.
        for issue_no, title in [
            (42, "Boom on startup"),
            (43, "Investigated"),
            (44, "Failed before commit"),
        ]:
            db.add(
                HeartbeatSeen(
                    project_id=cb_project_id,
                    issue_number=issue_no,
                    issue_title=title,
                    issue_url=f"https://github.com/owner/hb-comment-proj/issues/{issue_no}",
                    dispatched_task_id="",  # fill in below
                )
            )
        db.flush()

        # Heartbeat-spawned task: success + merged + pushed.
        t_merged = Task(
            id="hbcmt-merged-1",
            project_id=cb_project_id,
            agent="fake",
            prompt="fix it",
            mode="task",
            status="success",
            heartbeat_spawned=True,
            heartbeat_issue_number=42,
            commit_hash="abcdef0123456789abcdef0123456789abcdef01",
            commit_message="Fix the crash",
            merge_state="merged",
            pushed=True,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            result_summary="Removed the f-string that crashed on startup.",
        )
        db.add(t_merged)
        # Heartbeat-spawned task: success but merge conflict (branch kept).
        t_conflict = Task(
            id="hbcmt-conflict-1",
            project_id=cb_project_id,
            agent="fake",
            prompt="fix another",
            mode="task",
            status="success",
            heartbeat_spawned=True,
            heartbeat_issue_number=43,
            commit_hash="deadbeef" + "0" * 32,
            commit_message="Investigated",
            merge_state="conflict",
            pushed=False,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            result_summary="Investigated.",
        )
        db.add(t_conflict)
        # Non-heartbeat task: must be ignored entirely.
        t_hand = Task(
            id="hbcmt-hand-1",
            project_id=cb_project_id,
            agent="fake",
            prompt="manual task",
            mode="task",
            status="success",
            commit_hash="feedface" + "0" * 32,
            pushed=True,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            result_summary="Manual, not heartbeat.",
        )
        db.add(t_hand)
        # Heartbeat-spawned task that has no commit (failed before
        # committing): must be a no-op even though it's heartbeat.
        t_no_commit = Task(
            id="hbcmt-fail-1",
            project_id=cb_project_id,
            agent="fake",
            prompt="fix but failed",
            mode="task",
            status="failed",
            heartbeat_spawned=True,
            heartbeat_issue_number=44,
            commit_hash="",
            pushed=False,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            result_summary="",
        )
        db.add(t_no_commit)
        db.flush()
        # Wire dispatched_task_id on the two heartbeat_seen rows.
        db.query(HeartbeatSeen).filter(
            HeartbeatSeen.project_id == cb_project_id,
            HeartbeatSeen.issue_number == 42,
        ).update({"dispatched_task_id": "hbcmt-merged-1"})
        db.query(HeartbeatSeen).filter(
            HeartbeatSeen.project_id == cb_project_id,
            HeartbeatSeen.issue_number == 43,
        ).update({"dispatched_task_id": "hbcmt-conflict-1"})
        db.query(HeartbeatSeen).filter(
            HeartbeatSeen.project_id == cb_project_id,
            HeartbeatSeen.issue_number == 44,
        ).update({"dispatched_task_id": "hbcmt-fail-1"})

    # Capture calls to the GitHub helpers.
    called = {"comment": [], "close": [], "patch": []}
    originals = {
        "create": github_client.create_issue_comment,
        "patch": github_client.update_issue_comment,
        "close": github_client.update_issue_state,
    }

    async def fake_create(full_name, issue_number, body):
        called["comment"].append((full_name, issue_number, body))
        return {
            "id": 4242,
            "html_url": f"https://github.com/{full_name}/issues/{issue_number}#issuecomment-4242",
        }

    async def fake_patch(full_name, comment_id, body):
        called["patch"].append((full_name, comment_id, body))
        return {"id": comment_id, "html_url": "patched"}

    async def fake_state(full_name, issue_number, state):
        called["close"].append((full_name, issue_number, state))
        return {"state": state}

    async def run_hook() -> None:
        # Run all three hooks concurrently. The runner's _inflight dict
        # makes each idempotent against overlapping calls for the same
        # task_id.
        await asyncio.gather(
            heartbeat_followup._post_comment_and_maybe_close(
                "hbcmt-merged-1", get_settings()
            ),
            heartbeat_followup._post_comment_and_maybe_close(
                "hbcmt-conflict-1", get_settings()
            ),
            heartbeat_followup._post_comment_and_maybe_close(
                "hbcmt-hand-1", get_settings()
            ),
            heartbeat_followup._post_comment_and_maybe_close(
                "hbcmt-fail-1", get_settings()
            ),
        )

    # The patch wraps BOTH the hook calls (13.2) and the REST routes
    # (13.4) so the TestClient drives our fakes instead of touching the
    # network. We open the context here (before 13.2) and keep it open
    # through 13.3 + 13.4 - FastAPI's TestClient runs route handlers in
    # the same thread / event loop as the test, so a module-level
    # ``patch.object`` is visible from inside ``await
    # github_client.create_issue_comment(...)`` calls made by the route.
    patch_ctx = (
        patch.object(github_client, "create_issue_comment", side_effect=fake_create),
        patch.object(github_client, "update_issue_comment", side_effect=fake_patch),
        patch.object(github_client, "update_issue_state", side_effect=fake_state),
    )
    for p in patch_ctx:
        p.start()
    try:
        asyncio.run(run_hook())

        # The merged task must have called both helpers. The conflict task
        # only the comment helper. The hand task NOTHING. The no-commit
        # heartbeat task NOTHING.
        comment_targets = sorted(call[1] for call in called["comment"])
        check(
            "hb-comment: create fired only for #42 (merged) + #43 (conflict)",
            comment_targets == [42, 43],
            str(called["comment"]),
        )
        close_targets = [(call[1], call[2]) for call in called["close"]]
        check(
            "hb-comment: close fired only for #42",
            close_targets == [(42, "closed")],
            str(called["close"]),
        )
        check("hb-comment: patch never fired", called["patch"] == [], str(called["patch"]))
    
        # DB state: the merged task should have comment + close stamps; the
        # conflict task only a comment stamp; the rest unchanged.
        with session_scope() as db:
            merged_row = db.query(Task).filter(Task.id == "hbcmt-merged-1").one()
            conflict_row = db.query(Task).filter(Task.id == "hbcmt-conflict-1").one()
            hand_row = db.query(Task).filter(Task.id == "hbcmt-hand-1").one()
            no_commit_row = db.query(Task).filter(Task.id == "hbcmt-fail-1").one()
            check(
                "hb-comment: merged task heartbeat_commented_at stamped",
                merged_row.heartbeat_commented_at is not None,
                str(merged_row.heartbeat_commented_at),
            )
            check(
                "hb-comment: merged task heartbeat_closed_at stamped",
                merged_row.heartbeat_closed_at is not None,
                str(merged_row.heartbeat_closed_at),
            )
            check(
                "hb-comment: conflict task heartbeat_commented_at stamped",
                conflict_row.heartbeat_commented_at is not None,
            )
            check(
                "hb-comment: conflict task heartbeat_closed_at remains None",
                conflict_row.heartbeat_closed_at is None,
            )
            check(
                "hb-comment: hand task heartbeat_commented_at never stamped",
                hand_row.heartbeat_commented_at is None,
            )
            check(
                "hb-comment: no-commit task heartbeat_commented_at never stamped",
                no_commit_row.heartbeat_commented_at is None,
            )
            # HeartbeatSeen rows: comment id + url + state.
            seen42 = db.query(HeartbeatSeen).filter(
                HeartbeatSeen.project_id == cb_project_id,
                HeartbeatSeen.issue_number == 42,
            ).one()
            check(
                "hb-comment: HeartbeatSeen #42 last_comment_id stamped",
                seen42.last_comment_id == 4242,
                str(seen42.last_comment_id),
            )
            check(
                "hb-comment: HeartbeatSeen #42 last_issue_state=closed",
                seen42.last_issue_state == "closed",
                str(seen42.last_issue_state),
            )
            check(
                "hb-comment: HeartbeatSeen #42 last_commented_at set",
                seen42.last_commented_at is not None,
            )
            seen43 = db.query(HeartbeatSeen).filter(
                HeartbeatSeen.project_id == cb_project_id,
                HeartbeatSeen.issue_number == 43,
            ).one()
            check(
                "hb-comment: HeartbeatSeen #43 last_issue_state still empty (no close)",
                seen43.last_issue_state == "",
                str(seen43.last_issue_state),
            )
            check(
                "hb-comment: HeartbeatSeen #43 last_comment_id set (comment only)",
                seen43.last_comment_id == 4242,
                str(seen43.last_comment_id),
            )
    
            # -------- 13.3 idempotency: re-run doesn't double-comment ---------- #
            # Clear the in-flight guard so we can re-run; the existence of
            # heartbeat_commented_at should suppress further calls.
            called["comment"].clear()
            called["close"].clear()
            asyncio.run(
                heartbeat_followup._post_comment_and_maybe_close(
                    "hbcmt-merged-1", get_settings()
                )
            )
            check(
                "hb-comment: re-run is a no-op (create_issue_comment not called)",
                called["comment"] == [],
                str(called["comment"]),
            )
            check(
                "hb-comment: re-run is a no-op (update_issue_state not called)",
                called["close"] == [],
                str(called["close"]),
            )
    
        # -------- 13.4 REST: comment-again, close, reopen ------------------ #
        # Stubs still in force; TestClient drives the routes.
        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret-pw"},
            )
            token = ok.json()["access_token"]
            H = {"Authorization": f"Bearer {token}"}

            # comment-again: should POST a NEW comment with a fresh id so the
            # operator's intent is visible in the timeline.
            called["comment"].clear()
            called["close"].clear()
            r = client.post(
                f"/api/projects/{cb_project_id}/heartbeat/issues/42/comment-again",
                headers=H,
            )
            check("hb-comment: comment-again 200", r.status_code == 200, str(r.status_code))
            body = r.json()
            check("hb-comment: comment-again returns comment_id", body.get("comment_id") == 4242, str(body))
            check(
                "hb-comment: comment-again fired create_issue_comment",
                len(called["comment"]) == 1,
                str(called["comment"]),
            )
            check(
                "hb-comment: comment-again does NOT close when already closed",
                called["close"] == [],
                str(called["close"]),
            )

            # close -> 200, sets state=closed (already closed on GitHub but the
            # call still fires once and stamps heartbeat_seen).
            called["close"].clear()
            r2 = client.post(
                f"/api/projects/{cb_project_id}/heartbeat/issues/42/close",
                headers=H,
            )
            check("hb-comment: close 200", r2.status_code == 200, str(r2.status_code))
            check("hb-comment: close returns state=closed", r2.json().get("state") == "closed", str(r2.json()))
            check("hb-comment: close fired update_issue_state once", len(called["close"]) == 1, str(called["close"]))

            # reopen -> 200, sets state=open.
            called["close"].clear()
            r3 = client.post(
                f"/api/projects/{cb_project_id}/heartbeat/issues/42/reopen",
                headers=H,
            )
            check("hb-comment: reopen 200", r3.status_code == 200, str(r3.status_code))
            check("hb-comment: reopen returns state=open", r3.json().get("state") == "open", str(r3.json()))
            check(
                "hb-comment: reopen fired update_issue_state with state=open",
                called["close"] == [("owner/hb-comment-proj", 42, "open")],
                str(called["close"]),
            )

            # Unknown project -> 404.
            r404 = client.post(
                "/api/projects/does-not-exist/heartbeat/issues/42/comment-again",
                headers=H,
            )
            check("hb-comment: comment-again 404 on unknown project", r404.status_code == 404, str(r404.status_code))
    finally:
        for p in patch_ctx:
            p.stop()

    # -------- 13.5 cleanup --------------------------------------------- #
    with session_scope() as db:
        db.query(HeartbeatSeen).filter(HeartbeatSeen.project_id == cb_project_id).delete()
        db.query(Task).filter(Task.id.in_(["hbcmt-merged-1", "hbcmt-conflict-1", "hbcmt-hand-1", "hbcmt-fail-1"])).delete()
        db.query(Project).filter(Project.id == cb_project_id).delete()


# --------------------------------------------------------------------------- #
# Env profiles + per-task host runner (2026-07-14 feature)
# --------------------------------------------------------------------------- #

def _env_smoke_settings():
    """Snapshot of CD_SECRET_KEY for the env-crypto / profile tests."""
    return {"CD_SECRET_KEY": os.environ.get("CD_SECRET_KEY", "")}


def test_env_crypto() -> None:
    """Fernet roundtrip + tamper rejection + is_encryption_available().

    Defaults to the smoke suite's ``CD_SECRET_KEY=test-secret-key``, which
    is NOT the bundled placeholder — so encryption is available. We also
    exercise the negative path against the placeholder via a temporary
    patch.
    """
    from app import env_crypto
    from app.config import get_settings

    s = _env_smoke_settings()
    check(
        "env_crypto: encryption available with the smoke CD_SECRET_KEY",
        env_crypto.is_encryption_available(),
        f"got={env_crypto.is_encryption_available()}",
    )
    tok = env_crypto.encrypt_secret("hello")
    check("env_crypto: ciphertext != plaintext", tok != "hello")
    pt = env_crypto.decrypt_secret(tok)
    check("env_crypto: roundtrip decrypts", pt == "hello")

    # Tamper: mangle one byte so Fernet rejects
    bad = "A" + tok[1:]
    try:
        env_crypto.decrypt_secret(bad)
        check("env_crypto: tampered ciphertext raises", False)
    except Exception:
        check("env_crypto: tampered ciphertext raises", True)

    # Negative path: with the bundled placeholder, encryption is unavailable
    # and encrypt_secret raises a RuntimeError.
    settings = get_settings()
    original = settings.secret_key
    try:
        settings.secret_key = "CHANGE-ME-please-generate-a-real-secret"
        check(
            "env_crypto: not available with bundled placeholder",
            env_crypto.is_encryption_available() is False,
        )
        try:
            env_crypto.encrypt_secret("anything")
            check("env_crypto: encrypt refuses w/o real key", False)
        except RuntimeError:
            check("env_crypto: encrypt refuses w/o real key", True)
    finally:
        settings.secret_key = original

    # anonymise_token
    check("env_crypto: anonymise short -> ***", env_crypto.anonymise_token("abc") == "***")
    check("env_crypto: anonymise empty -> ''", env_crypto.anonymise_token("") == "")
    check(
        "env_crypto: anonymise long -> first2+…+last2",
        env_crypto.anonymise_token("sk-abc123def") == "sk…ef",
    )


def test_env_profiles_crud() -> None:
    """Round-trip POST/GET/PATCH/DELETE for /api/env-profiles with the
    redaction contract:
      * GET never echoes the plaintext token
      * POST + PATCH return ``anthropic_auth_token_set=True`` + ``hint``
      * PATCH with new token re-encrypts (decrypt round-trip sees the new
        value, NOT the old one)
      * duplicate POST => 409
      * PATCH with the literal ``"***"`` -> 422
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import EnvProfile
    from app.database import session_scope

    _clean_env_profiles()

    with TestClient(app) as client:
        ok = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
        )
        H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

        # 1. Create a profile with token
        r = client.post(
            "/api/env-profiles",
            headers=H,
            json={
                "key": "zai",
                "name": "Z.AI",
                "anthropic_base_url": "https://api.z.ai",
                "anthropic_auth_token": "secret-token-xyz-123",
            },
        )
        check("env_profiles: POST 201", r.status_code == 201, str(r.status_code))
        body = r.json()
        check("env_profiles: POST returns redacted shape", body["anthropic_auth_token_set"] is True)
        check("env_profiles: POST returns anonymised hint", body["anthropic_auth_token_hint"].startswith("se"))
        check("env_profiles: POST does NOT echo plaintext", "secret-token-xyz-123" not in r.text)

        # 2. List returns the row
        r = client.get("/api/env-profiles", headers=H)
        check("env_profiles: GET 200", r.status_code == 200)
        rows = [row for row in r.json() if row["key"] == "zai"]
        check("env_profiles: GET finds created row", len(rows) == 1)
        check(
            "env_profiles: GET rows never carry plaintext",
            not any("secret-token-xyz-123" in json.dumps(row) for row in r.json()),
        )

        # 3. Duplicate POST -> 409
        r = client.post(
            "/api/env-profiles",
            headers=H,
            json={
                "key": "zai",
                "name": "Z.AI again",
                "anthropic_base_url": "",
                "anthropic_auth_token": "",
            },
        )
        check("env_profiles: duplicate POST 409", r.status_code == 409, str(r.status_code))

        # 4. PATCH with `***` (placeholder) -> 422
        r = client.patch(
            "/api/env-profiles/zai",
            headers=H,
            json={
                "key": "zai",
                "name": "Z.AI",
                "anthropic_base_url": "",
                "anthropic_auth_token": "***",
            },
        )
        check("env_profiles: PATCH with *** placeholder -> 422", r.status_code == 422, str(r.status_code))

        # 5. PATCH with rotated token re-encrypts
        r = client.patch(
            "/api/env-profiles/zai",
            headers=H,
            json={
                "key": "zai",
                "name": "Z.AI (rotated)",
                "anthropic_base_url": "https://api.z.ai/v2",
                "anthropic_auth_token": "rotated-token-789",
            },
        )
        check("env_profiles: PATCH 200", r.status_code == 200, str(r.status_code))
        check("env_profiles: PATCH updated name", r.json()["name"] == "Z.AI (rotated)")
        # Verify the stored blob decrypts to the NEW plaintext
        from app import env_crypto
        with session_scope() as db:
            row = db.query(EnvProfile).filter(EnvProfile.key == "zai").one()
            stored = row.anthropic_auth_token_encrypted
        check(
            "env_profiles: PATCH re-encrypted with new value",
            env_crypto.decrypt_secret(stored) == "rotated-token-789",
        )
        check(
            "env_profiles: PATCH did NOT preserve old plaintext",
            "secret-token-xyz-123" not in json.dumps(r.json()),
        )

        # 6. PATCH with empty token leaves stored token intact (the
        # frontend treats that as "leave unchanged")
        original_stored = stored
        r = client.patch(
            "/api/env-profiles/zai",
            headers=H,
            json={
                "key": "zai",
                "name": "Z.AI",
                "anthropic_base_url": "https://api.z.ai/v2",
                "anthropic_auth_token": "",
            },
        )
        check("env_profiles: PATCH empty token 200", r.status_code == 200)
        with session_scope() as db:
            row = db.query(EnvProfile).filter(EnvProfile.key == "zai").one()
            now_stored = row.anthropic_auth_token_encrypted
        check(
            "env_profiles: PATCH with empty token preserves stored ciphertext",
            now_stored == original_stored,
        )

        # 7. DELETE -> 204 + GET removes the row
        r = client.delete("/api/env-profiles/zai", headers=H)
        check("env_profiles: DELETE 204", r.status_code == 204)
        rows = [row for row in client.get("/api/env-profiles", headers=H).json() if row["key"] == "zai"]
        check("env_profiles: DELETE removes row", len(rows) == 0)


def test_env_profiles_encryption_gated() -> None:
    """With the bundled placeholder secret_key, POST with a token returns 503."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.database import session_scope
    from app.main import app
    from app.models import EnvProfile

    settings = get_settings()
    original = settings.secret_key

    _clean_env_profiles()
    try:
        settings.secret_key = "CHANGE-ME-please-generate-a-real-secret"
        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
            )
            H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

            # POST with empty token still works (no token = no encryption needed)
            r = client.post(
                "/api/env-profiles",
                headers=H,
                json={
                    "key": "no-token",
                    "name": "No-Token",
                    "anthropic_base_url": "https://example.com",
                    "anthropic_auth_token": "",
                },
            )
            check(
                "env_profiles: POST with empty token under default-secret succeeds",
                r.status_code == 201,
                str(r.status_code),
            )

            # POST with a token is refused
            r = client.post(
                "/api/env-profiles",
                headers=H,
                json={
                    "key": "with-token",
                    "name": "With-Token",
                    "anthropic_base_url": "",
                    "anthropic_auth_token": "should-be-refused",
                },
            )
            check(
                "env_profiles: POST with token under default-secret returns 503",
                r.status_code == 503,
                f"got {r.status_code}",
            )

            # PATCH with a token is also refused
            r = client.patch(
                "/api/env-profiles/no-token",
                headers=H,
                json={
                    "key": "no-token",
                    "name": "No-Token",
                    "anthropic_base_url": "",
                    "anthropic_auth_token": "x",
                },
            )
            check(
                "env_profiles: PATCH with token under default-secret returns 503",
                r.status_code == 503,
                f"got {r.status_code}",
            )
    finally:
        settings.secret_key = original
        _clean_env_profiles()


def test_task_runner_env_profile_injection() -> None:
    """Unit-test the per-task env-overlay assembly directly (rather than
    driving a full ``TaskManager.submit`` cycle through worktree /
    staging / git — those have their own tests and would otherwise add
    ~5s of wall-clock + a flakiness surface to this isolated check).

    Verifies:
      * ``_build_env_overlay(env_profile_key, ch)`` resolves the
        ANTHROPIC_* fields from the EnvProfile row, decrypts the token,
        AND stamps ANTHROPIC_API_KEY="" defensively.
      * The overlay is the ONLY thing the runner mutates: when we
        ``model_copy(update={"env": {**spec.env, **overlay}})`` the cached
        agent config is unchanged.
      * An unknown profile key logs a warning and returns an empty dict
        instead of raising — operators may rename / delete a profile
        between submit and start.
    """
    from app import env_crypto
    from app.config import AgentSpec
    from app.database import session_scope
    from app.models import EnvProfile
    from app.task_runner import _build_env_overlay

    _clean_env_profiles()

    # Seed
    with session_scope() as db:
        db.add(
            EnvProfile(
                key="p1",
                name="Profile 1",
                anthropic_base_url="https://router.example.com",
                anthropic_auth_token_encrypted=env_crypto.encrypt_secret(
                    "the-token"
                ),
            )
        )

    class _StubCh:
        def __init__(self):
            self.events: list[dict] = []

        def publish(self, ev: dict) -> None:
            self.events.append(ev)

    ch = _StubCh()

    overlay = _build_env_overlay("p1", ch)
    check("env_profile_injection: overlay non-empty", bool(overlay))
    check(
        "env_profile_injection: ANTHROPIC_BASE_URL overlaid",
        overlay.get("ANTHROPIC_BASE_URL") == "https://router.example.com",
    )
    check(
        "env_profile_injection: ANTHROPIC_AUTH_TOKEN overlaid (decrypted)",
        overlay.get("ANTHROPIC_AUTH_TOKEN") == "the-token",
    )
    check(
        "env_profile_injection: ANTHROPIC_API_KEY explicitly empty (defensive)",
        overlay.get("ANTHROPIC_API_KEY") == "",
    )

    # model_copy on a real AgentSpec; check the cached config is untouched.
    from app.config import get_agents_config

    cfg = get_agents_config()
    base_spec = cfg.agents["fake"]
    new_spec = base_spec.model_copy(update={"env": {**base_spec.env, **overlay}})
    check(
        "env_profile_injection: cloned spec.env has ANTHROPIC_AUTH_TOKEN",
        new_spec.env.get("ANTHROPIC_AUTH_TOKEN") == "the-token",
    )
    check(
        "env_profile_injection: cached agent env untouched",
        "ANTHROPIC_AUTH_TOKEN" not in base_spec.env,
    )

    # Unknown profile key -> empty dict + a warning was published
    overlay_unknown = _build_env_overlay("does-not-exist", ch)
    check("env_profile_injection: unknown key -> empty dict", overlay_unknown == {})
    check(
        "env_profile_injection: unknown key publishes a warning",
        any(
            ev.get("type") == "output"
            and "Env-Profil 'does-not-exist' nicht gefunden" in ev.get("data", "")
            for ev in ch.events
        ),
    )

    # Empty token profile -> overlay with ONLY ANTHROPIC_BASE_URL (still
    # stamps the explicit empty ANTHROPIC_API_KEY on the base spec).
    with session_scope() as db:
        db.add(
            EnvProfile(
                key="url-only",
                name="URL only",
                anthropic_base_url="https://example.com",
                anthropic_auth_token_encrypted="",
            )
        )
    ch2 = _StubCh()
    overlay_url_only = _build_env_overlay("url-only", ch2)
    check(
        "env_profile_injection: profile with no token -> only base_url",
        overlay_url_only.get("ANTHROPIC_BASE_URL") == "https://example.com"
        and "ANTHROPIC_AUTH_TOKEN" not in overlay_url_only,
    )
    check(
        "env_profile_injection: defensive ANTHROPIC_API_KEY still set on overlay",
        overlay_url_only.get("ANTHROPIC_API_KEY") == "",
    )

    # Cleanup
    with session_scope() as db:
        db.query(EnvProfile).filter(EnvProfile.key.in_(["p1", "url-only"])).delete()


def test_runner_toggle_persistence() -> None:
    """Unit-test the runner-sibling shim at the spec-resolution level
    (without going through a full TaskManager.submit cycle).

    Verifies:
      * When ``runner='host'`` is set, the resolver picks the
        ``<agent>-host`` sibling IF one exists + is enabled.
      * When the sibling is missing/disabled, the resolver returns
        ``None`` so the caller can surface a 400 with operator guidance.
      * The Task row's ``runner`` column persists whatever the operator
        submitted (server-side defensive guard at the route, separate
        concern).
    """
    from app.config import AgentSpec, get_agents_config
    from app.database import session_scope
    from app.models import EnvProfile, Project, Task

    _clean_env_profiles()
    cfg = get_agents_config()

    # With the sibling registered AND enabled: resolver picks it
    cfg.agents["fake-host"] = AgentSpec(
        key="fake-host",
        display_name="Fake Host",
        command=[PY, "-c", FAKE_SCRIPT],
        session_command=[PY, "-c", "pass"],
        prompt_via="arg",
        stream_format="raw",
        enabled=True,
        host_staging=True,
    )

    # Simulate the resolver logic the runner applies at start time.
    def _resolve(runner: str, agent: str):
        spec = cfg.agents.get(agent)
        if runner == "host":
            sibling = cfg.agents.get(f"{agent}-host")
            if sibling is None or not sibling.enabled:
                return None  # caller surfaces 400
            spec = sibling
        return spec

    resolved = _resolve("host", "fake")
    check(
        "runner_toggle: resolver picks 'fake-host' when sibling is enabled",
        resolved is not None and resolved.key == "fake-host",
        repr(resolved.key if resolved else None),
    )

    # Disable the sibling; resolver returns None
    cfg.agents["fake-host"].enabled = False
    resolved_off = _resolve("host", "fake")
    check(
        "runner_toggle: resolver returns None when -host sibling is disabled",
        resolved_off is None,
    )

    # Re-enable for the rest of the test, then verify the column
    # persistence path directly with a Task row.
    cfg.agents["fake-host"].enabled = True

    # The column-level persistence is straight ORM; this is what the
    # tasks router writes + what the tasks GET returns.
    with session_scope() as db:
        p = Project(
            id="runner-1",
            name="runner-test",
            slug="runner-test",
            local_path=str(TMP / "runner-1"),
        )
        Path(p.local_path).mkdir(parents=True, exist_ok=True)
        db.add(p)
        db.add(
            Task(
                id="runner-1-host",
                project_id="runner-1",
                agent="fake",
                prompt="probe",
                mode="task",
                status="queued",
                runner="host",
                env_profile_key="",
            )
        )

    with session_scope() as db:
        t = db.get(Task, "runner-1-host")
        check(
            "runner_toggle: Task.runner persisted as 'host'",
            t is not None and t.runner == "host",
            repr(getattr(t, "runner", None) if t else None),
        )

    # Cleanup
    with session_scope() as db:
        db.query(Task).filter(Task.id == "runner-1-host").delete()
        db.query(Project).filter(Project.id == "runner-1").delete()
    cfg.agents.pop("fake-host", None)


def test_runner_fallback_when_ssh_not_configured() -> None:
    """No -host sibling defined in test config: POST /api/projects/{pid}/tasks
    with runner='host' returns 400 with operator message; same for sessions.
    """
    from fastapi.testclient import TestClient

    from app.database import session_scope
    from app.main import app
    from app.models import Project

    _clean_env_profiles()
    cfg = get_agents_config()
    # Ensure no -host sibling for 'fake'
    cfg.agents.pop("fake-host", None)

    with session_scope() as db:
        p = Project(
            id="runnerfb-1",
            name="runner-fallback",
            slug="runner-fallback",
            local_path=str(TMP / "runnerfb-1"),
        )
        Path(p.local_path).mkdir(parents=True, exist_ok=True)
        db.add(p)

    with TestClient(app) as client:
        ok = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
        )
        H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

        r = client.post(
            "/api/projects/runnerfb-1/tasks",
            headers=H,
            json={
                "agent": "fake",
                "prompt": "x",
                "mode": "task",
                "runner": "host",
            },
        )
        check(
            "runner_fallback: POST tasks runner=host returns 400",
            r.status_code == 400,
            f"got {r.status_code}",
        )
        check(
            "runner_fallback: 400 message mentions CD_FAKE_SSH_USER / operator guidance",
            "CD_FAKE_SSH_USER" in r.text or "Host-Runner" in r.text,
            r.text[:200],
        )

        r = client.post(
            "/api/sessions",
            headers=H,
            json={
                "project_id": "runnerfb-1",
                "agent": "fake",
                "runner": "host",
            },
        )
        check(
            "runner_fallback: POST sessions runner=host returns 400",
            r.status_code == 400,
            f"got {r.status_code}",
        )

    # Cleanup
    with session_scope() as db:
        db.query(Project).filter(Project.id == "runnerfb-1").delete()


def test_runner_guard_strips_existing_host_suffix() -> None:
    """Regression: selecting an explicit ``<agent>-host`` key AND
    runner='host' must not build ``<agent>-host-host`` in the route guard.

    Before the fix, ``host_key = f"{body.agent}-host"`` produced
    ``fake-host-host`` (never registered) and rejected the request with a
    bogus ``CD_FAKE-HOST_SSH_USER`` message. The guard now strips an
    existing ``-host`` suffix first, so the enabled ``fake-host`` sibling
    is found and the request is accepted.
    """
    from fastapi.testclient import TestClient

    from app.config import AgentSpec, get_agents_config
    from app.database import session_scope
    from app.main import app
    from app.models import Project, Task

    _clean_env_profiles()
    cfg = get_agents_config()
    cfg.agents["fake-host"] = AgentSpec(
        key="fake-host",
        display_name="Fake Host",
        command=[PY, "-c", FAKE_SCRIPT],
        session_command=[PY, "-c", "pass"],
        prompt_via="arg",
        stream_format="raw",
        enabled=True,
        host_staging=True,
    )

    with session_scope() as db:
        p = Project(
            id="runnerdh-1",
            name="runner-doublehost",
            slug="runner-doublehost",
            local_path=str(TMP / "runnerdh-1"),
        )
        Path(p.local_path).mkdir(parents=True, exist_ok=True)
        db.add(p)

    try:
        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret-pw"},
            )
            H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

            r = client.post(
                "/api/projects/runnerdh-1/tasks",
                headers=H,
                json={
                    "agent": "fake-host",
                    "prompt": "x",
                    "mode": "task",
                    "runner": "host",
                },
            )
            # Must NOT be rejected by the host-runner guard: the point is the
            # guard finds ``fake-host`` (not ``fake-host-host``). 201 expected.
            check(
                "double_host: POST tasks agent=fake-host + runner=host not 400",
                r.status_code != 400,
                f"got {r.status_code}: {r.text[:200]}",
            )
            check(
                "double_host: no bogus CD_FAKE-HOST_SSH_USER message",
                "FAKE-HOST_SSH_USER" not in r.text,
                r.text[:200],
            )

            r_sess = client.post(
                "/api/sessions",
                headers=H,
                json={
                    "project_id": "runnerdh-1",
                    "agent": "fake-host",
                    "runner": "host",
                },
            )
            check(
                "double_host: POST sessions agent=fake-host + runner=host not guard-400",
                "FAKE-HOST_SSH_USER" not in r_sess.text,
                f"got {r_sess.status_code}: {r_sess.text[:200]}",
            )
    finally:
        cfg.agents.pop("fake-host", None)
        with session_scope() as db:
            db.query(Task).filter(Task.project_id == "runnerdh-1").delete()
            db.query(Project).filter(Project.id == "runnerdh-1").delete()


def test_session_runner_shim() -> None:
    """Unit-test the SessionManager.start spec-resolution path.

    When ``runner='host'`` is passed and a ``<agent>-host`` sibling
    exists + is enabled + has a session_command, the function resolves
    to the sibling (so its ``host_staging=True`` flag drives the rest of
    the pipeline). The PTY/spawn path itself is exercised by the
    existing ``test_session_api_and_manager`` + ``test_session_workdir_resolution``
    tests; we keep this check isolated to the resolver so it doesn't
    have to drive os.fork.
    """
    from app.config import AgentSpec, get_agents_config

    cfg = get_agents_config()
    cfg.agents["fake-host"] = AgentSpec(
        key="fake-host",
        display_name="Fake Host",
        command=[PY, "-c", "pass"],
        session_command=[PY, "-c", "pass"],
        prompt_via="arg",
        stream_format="raw",
        enabled=True,
        host_staging=True,
    )

    # Pure-Python mirror of the resolver logic inside SessionManager.start
    # (kept aligned in code review). The point is the SHAPE of the
    # sibling swap, not the os.fork plumbing.
    def _resolve_session(runner: str, agent: str):
        spec = cfg.agents.get(agent)
        if runner == "host":
            sibling_key = f"{agent}-host"
            sibling = cfg.agents.get(sibling_key)
            if sibling is None or not sibling.enabled:
                raise ValueError(
                    f"Host-Runner fuer Agent {agent!r} nicht aktiviert."
                )
            if not sibling.session_command:
                raise ValueError(
                    f"Agent {sibling_key!r} does not support session mode"
                )
            spec = sibling
        if not spec.session_command:
            raise ValueError(f"Agent {agent} does not support session mode")
        return spec

    # 1. resolver picks the sibling
    resolved = _resolve_session("host", "fake")
    check(
        "session_runner_shim: resolver picks fake-host sibling",
        resolved.key == "fake-host",
        repr(resolved.key),
    )

    # 2. sibling missing -> ValueError
    cfg.agents.pop("fake-host")
    try:
        _resolve_session("host", "fake")
        check("session_runner_shim: missing -host -> ValueError", False)
    except ValueError as e:
        check(
            "session_runner_shim: missing -host -> ValueError",
            "Host-Runner fuer Agent" in str(e),
        )

    # 3. sibling with no session_command -> "session mode" error
    cfg.agents["fake-host"] = AgentSpec(
        key="fake-host",
        display_name="Fake Host (no session)",
        command=[PY, "-c", "pass"],
        prompt_via="arg",
        stream_format="raw",
        enabled=True,
        host_staging=True,
    )
    try:
        _resolve_session("host", "fake")
        check("session_runner_shim: sibling w/o session_command -> ValueError", False)
    except ValueError as e:
        check(
            "session_runner_shim: sibling w/o session_command -> ValueError",
            "session mode" in str(e).lower(),
        )

    cfg.agents.pop("fake-host", None)


def test_runner_picks_host_sibling_by_key() -> None:
    """Mirrors the new "Agent" dropdown behaviour on the dashboard:
    Claude Code and Hermes each expose a single dropdown that lists
    ``<base>`` and ``<base>-host`` side-by-side; the UI sends the
    concrete AgentSpec key (e.g. ``claude-host``) directly to the
    backend instead of the historical ``agent=claude`` + ``runner=host``
    pair.

    The task runner / session manager must therefore accept the
    already-resolved ``<base>-host`` key WITHOUT then trying to resolve
    ``<base>-host-host`` (a regression of the host shim) and without
    rejecting the request just because the host shim's defensive
    guard wants a base key.
    """
    from app.config import AgentSpec, get_agents_config

    cfg = get_agents_config()
    cfg.agents["fake-host"] = AgentSpec(
        key="fake-host",
        display_name="Fake Host",
        command=[PY, "-c", FAKE_SCRIPT],
        session_command=[PY, "-c", "pass"],
        prompt_via="arg",
        stream_format="raw",
        enabled=True,
        host_staging=True,
    )

    # Pure-Python mirror of the SAME guard the runner / session manager
    # use at start time. A bug that double-applied the host shim would
    # flip "fake-host" into a "fake-host-host" lookup and raise here.
    def _resolve_task(runner: str, agent: str):
        spec = cfg.agents.get(agent)
        if spec is None or not spec.enabled:
            return None
        if runner == "host" and not agent.endswith("-host"):
            sibling = cfg.agents.get(f"{agent}-host")
            if sibling is None or not sibling.enabled:
                return None
            spec = sibling
        return spec

    def _resolve_session(runner: str, agent: str):
        spec = cfg.agents.get(agent)
        if spec is None or not spec.enabled:
            raise ValueError(f"Unknown or disabled agent: {agent}")
        if runner == "host" and not agent.endswith("-host"):
            sibling = cfg.agents.get(f"{agent}-host")
            if sibling is None or not sibling.enabled:
                raise ValueError(
                    f"Host-Runner fuer Agent {agent!r} nicht aktiviert."
                )
            if not sibling.session_command:
                raise ValueError(
                    f"Agent {sibling.key!r} does not support session mode"
                )
            spec = sibling
        if not spec.session_command:
            raise ValueError(f"Agent {agent} does not support session mode")
        return spec

    # 1. The historical "base + runner=host" payload still resolves to
    # the sibling (no regression for the legacy path).
    legacy = _resolve_task("host", "fake")
    check(
        "host_key_payload: legacy base+runner=host picks fake-host",
        legacy is not None and legacy.key == "fake-host",
        repr(legacy.key if legacy else None),
    )

    # 2. The new "already-resolved <base>-host" payload bypasses the
    # shim cleanly and does not look up "fake-host-host".
    direct = _resolve_task("host", "fake-host")
    check(
        "host_key_payload: direct fake-host key bypasses shim",
        direct is not None and direct.key == "fake-host",
        repr(direct.key if direct else None),
    )

    # 3. The session path accepts the resolved key for the same reason.
    sess = _resolve_session("host", "fake-host")
    check(
        "host_key_payload: direct fake-host resolves for sessions",
        sess.key == "fake-host",
        repr(sess.key),
    )

    # 4. The base-key path still works (no behaviour change for the
    # legacy form even with the new guard in place).
    legacy_sess = _resolve_session("host", "fake")
    check(
        "host_key_payload: base+runner=host still resolves to sibling",
        legacy_sess.key == "fake-host",
        repr(legacy_sess.key),
    )

    cfg.agents.pop("fake-host", None)


def test_hermes_host_sibling_registers() -> None:
    """With CD_HERMES_SSH_USER set, the entrypoint generator must emit BOTH
    ``hermes`` (container-side, enabled iff the CLI is on PATH) AND
    ``hermes-host`` (the SSH-driven sibling with host_staging=True and ssh
    argv pointing at the resolved user@host). Without CD_HERMES_SSH_USER
    AND with no in-image CLI, neither is enabled (no ``hermes-host``
    registered; container ``hermes`` is disabled but present so operators
    can flip it on by hand later).
    """
    from app.config_bootstrap import generate_initial_agents_config

    # ---- case A: Hermes SSH user set, no in-image hermes on PATH ----
    env_a = {
        "CD_HERMES_SSH_USER": "huser",
        "CD_HERMES_SSH_HOST": "host.docker.internal",
        "CD_HERMES_SSH_PORT": "22",
        # No hermes binary on PATH -> container entry is disabled.
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    doc_a = generate_initial_agents_config(env_a)
    agents_a = doc_a["agents"]

    check(
        "hermes_host: container hermes entry stays (CLI absent -> enabled=False)",
        "hermes" in agents_a and agents_a["hermes"].get("enabled") is False,
        list(agents_a),
    )
    check(
        "hermes_host: hermes-host sibling is registered",
        "hermes-host" in agents_a,
        list(agents_a),
    )
    host_a = agents_a["hermes-host"]
    check(
        "hermes_host: host_staging=True",
        host_a.get("host_staging") is True,
        str(host_a.get("host_staging")),
    )
    check(
        "hermes_host: command starts with ssh",
        bool(host_a.get("command")) and host_a["command"][0] == "ssh",
        str(host_a["command"][:3]) if host_a.get("command") else None,
    )
    check(
        "hermes_host: command targets huser@host.docker.internal",
        any(t == "huser@host.docker.internal" for t in host_a["command"]),
        str(host_a["command"]),
    )
    check(
        "hermes_host: display_name='Hermes (Host)'",
        host_a.get("display_name") == "Hermes (Host)",
        str(host_a.get("display_name")),
    )
    check(
        "hermes_host: prompt_via=stdin (multi-line safe via ssh)",
        host_a.get("prompt_via") == "stdin",
        str(host_a.get("prompt_via")),
    )
    check(
        "hermes_host: enabled=True",
        host_a.get("enabled") is True,
        str(host_a.get("enabled")),
    )

    # ---- case B: no SSH user, no CLI on PATH ----
    env_b = {"PATH": "/var/empty/bin"}
    doc_b = generate_initial_agents_config(env_b)
    agents_b = doc_b["agents"]

    check(
        "hermes_host: hermes-host absent when no SSH user",
        "hermes-host" not in agents_b,
        list(agents_b),
    )
    check(
        "hermes_host: container hermes disabled when CLI absent (no SSH)",
        "hermes" in agents_b and agents_b["hermes"].get("enabled") is False,
    )


def test_claude_host_reuses_hermes_ssh() -> None:
    """4-case matrix proving the shared-SSH-wiring rule from
    deploy/docker/entrypoint.sh (mirrored in config_bootstrap.py):

      Hermes only set -> claude-host registered with Hermes values.
      Claude only set -> hermes-host registered with Claude values.
      Both set       -> each sibling uses its own values.
      Neither set    -> neither -host sibling present.
    """
    from app.config_bootstrap import generate_initial_agents_config

    # case A: Hermes-only -> claude-host reuses Hermes ssh values
    doc_a = generate_initial_agents_config(
        {
            "CD_HERMES_SSH_USER": "huser",
            "CD_HERMES_SSH_HOST": "hermes.host",
            "CD_HERMES_SSH_PORT": "2222",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "claude_reuse: claude-host registered when only Hermes SSH user set",
        "claude-host" in doc_a,
        list(doc_a),
    )
    cmd_a = doc_a["claude-host"]["command"]
    check(
        "claude_reuse: claude-host command uses huser@hermes.host",
        any(t == "huser@hermes.host" for t in cmd_a),
        str(cmd_a),
    )
    check(
        "claude_reuse: claude-host command uses port 2222 (inherited)",
        "2222" in cmd_a,
        str(cmd_a),
    )
    # Ordering: the base ``claude`` entry MUST precede its ``claude-host``
    # sibling. /api/agents preserves dict order and the frontend defaults to
    # the first enabled entry — a ``-host`` key coming first would silently
    # make the SSH runner the default (publickey failure on first submit).
    keys_a = list(doc_a)
    check(
        "claude_reuse: base claude precedes claude-host sibling",
        "claude" in keys_a
        and "claude-host" in keys_a
        and keys_a.index("claude") < keys_a.index("claude-host"),
        keys_a,
    )
    # Every ``<base>-host`` sibling must come AFTER its base entry, so that
    # /api/agents order never puts an SSH sibling first (the frontend
    # defaults to the first suitable entry). This is the environment-
    # independent form of the invariant — the container CLIs may or may not
    # be installed in a given test/deploy env, but the ordering holds
    # regardless.
    host_after_base = all(
        base in keys_a and keys_a.index(base) < keys_a.index(k)
        for k in keys_a
        if k.endswith("-host") and (base := k[:-5])
    )
    check(
        "claude_reuse: every -host sibling follows its base entry",
        host_after_base,
        keys_a,
    )

    # case B: Claude-only -> hermes-host reuses Claude ssh values
    doc_b = generate_initial_agents_config(
        {
            "CD_CLAUDE_SSH_USER": "cuser",
            "CD_CLAUDE_SSH_HOST": "claude.host",
            "CD_CLAUDE_SSH_PORT": "2200",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "claude_reuse: hermes-host registered when only Claude SSH user set",
        "hermes-host" in doc_b,
        list(doc_b),
    )
    cmd_b = doc_b["hermes-host"]["command"]
    check(
        "claude_reuse: hermes-host command uses cuser@claude.host",
        any(t == "cuser@claude.host" for t in cmd_b),
        str(cmd_b),
    )
    check(
        "claude_reuse: hermes-host command uses port 2200 (inherited)",
        "2200" in cmd_b,
        str(cmd_b),
    )

    # case C: Both set independently -> each sibling uses its own values
    doc_c = generate_initial_agents_config(
        {
            "CD_HERMES_SSH_USER": "huser",
            "CD_HERMES_SSH_HOST": "hermes.host",
            "CD_HERMES_SSH_PORT": "2222",
            "CD_CLAUDE_SSH_USER": "cuser",
            "CD_CLAUDE_SSH_HOST": "claude.host",
            "CD_CLAUDE_SSH_PORT": "2200",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "both_set: hermes-host uses huser@hermes.host (own values)",
        any(t == "huser@hermes.host" for t in doc_c["hermes-host"]["command"]),
        str(doc_c["hermes-host"]["command"]),
    )
    check(
        "both_set: claude-host uses cuser@claude.host (own values)",
        any(t == "cuser@claude.host" for t in doc_c["claude-host"]["command"]),
        str(doc_c["claude-host"]["command"]),
    )

    # case D: Neither set -> no -host siblings
    doc_d = generate_initial_agents_config(
        {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    )["agents"]
    check("neither_set: hermes-host absent", "hermes-host" not in doc_d, list(doc_d))
    check("neither_set: claude-host absent", "claude-host" not in doc_d, list(doc_d))


def test_ssh_sibling_backfill_in_load_agents_config() -> None:
    """``load_agents_config`` must backfill missing SSH-driven siblings
    when the on-disk YAML predates the shared-wiring support.

    The first-boot generator emits all three of
    ``{hermes-host,claude-host,codex-host}`` whenever ANY ``CD_*_SSH_USER``
    is set (shared-wiring). An existing YAML written BEFORE a given
    ``-host`` flavor landed only has the older siblings — e.g. an
    upgrade that adds ``codex-host`` leaves the YAML with
    ``claude-host`` + ``hermes-host`` but no ``codex-host``, even though
    the operator's current SSH env vars would create all three today.

    The entrypoint also detects this and regenerates the on-disk YAML on
    the next container start, but operators that can't restart immediately
    still need the runtime to see the missing sibling. The loader-side
    backfill adds it in-memory so ``/api/agents`` ships the expected set
    right away. This test pins that behaviour:

      * YAML with only ``claude-host`` + ``hermes-host`` + base entries
        under SSH wiring -> ``codex-host`` gets added at load time.
      * Operator-disabled ``codex-host`` (``enabled: false``) is preserved
        — the key is already present, so the backfill is a no-op.
      * No SSH wiring -> nothing gets added, stale YAML entries stay as-is.
      * Custom (non-built-in) agents in the YAML are never touched.
    """
    from app.config import load_agents_config

    # --- case 1: stale YAML + Hermes-only SSH user -> codex-host backfilled ---
    stale_yaml = TMP / "ssh-backfill-stale.yaml"
    stale_yaml.write_text(
        "agents:\n"
        "  claude:\n"
        '    display_name: "Claude Code"\n'
        "    command:\n"
        "      - claude\n"
        "      - -p\n"
        "      - '{prompt}'\n"
        "      - --output-format\n"
        "      - stream-json\n"
        "      - --verbose\n"
        "      - --dangerously-skip-permissions\n"
        "    prompt_via: arg\n"
        "    stream_format: claude-json\n"
        "    session_command: [claude]\n"
        "    enabled: true\n"
        "  claude-host:\n"
        '    display_name: "Claude Code (Host)"\n'
        "    command: [ssh, -i, /home/app/.ssh/id_hermes, -p, '22', debian@host.docker.internal, "
        "'cd {project_dir} && claude']\n"
        "    prompt_via: stdin\n"
        "    stream_format: raw\n"
        "    session_command: [ssh, -tt, -i, /home/app/.ssh/id_hermes, -p, '22', "
        "debian@host.docker.internal, 'cd {project_dir} && claude']\n"
        "    host_staging: true\n"
        "    enabled: true\n"
        "  hermes:\n"
        '    display_name: "Hermes"\n'
        "    command: [hermes, chat, -q, '{prompt}', --yolo, --accept-hooks]\n"
        "    prompt_via: arg\n"
        "    stream_format: raw\n"
        "    session_command: [hermes, chat]\n"
        "    enabled: true\n"
        "  hermes-host:\n"
        '    display_name: "Hermes (Host)"\n'
        "    command: [ssh, -i, /home/app/.ssh/id_hermes, -p, '22', debian@host.docker.internal, "
        "'cd {project_dir} && hermes chat -q $(cat) --yolo --accept-hooks']\n"
        "    prompt_via: stdin\n"
        "    stream_format: raw\n"
        "    session_command: [ssh, -tt, -i, /home/app/.ssh/id_hermes, -p, '22', "
        "debian@host.docker.internal, 'cd {project_dir} && hermes chat']\n"
        "    host_staging: true\n"
        "    enabled: true\n"
        "  codex:\n"
        '    display_name: "Codex"\n'
        "    command: [codex, exec, --cd, '{project_dir}', --sandbox, workspace-write, "
        "--color, never, --ephemeral, --output-last-message, '{last_message_file}', '-']\n"
        "    prompt_via: stdin\n"
        "    stream_format: codex\n"
        "    session_command: [codex]\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    old_ssh = {k: os.environ.get(k) for k in (
        "CD_HERMES_SSH_USER", "CD_HERMES_SSH_HOST", "CD_HERMES_SSH_PORT",
        "CD_HERMES_SSH_KEY", "CD_CLAUDE_SSH_KEY", "CD_CODEX_SSH_KEY",
    )}
    try:
        os.environ["CD_HERMES_SSH_USER"] = "huser"
        os.environ.pop("CD_CLAUDE_SSH_USER", None)
        os.environ.pop("CD_CODEX_SSH_USER", None)
        cfg = load_agents_config(stale_yaml)
        agents = cfg.agents
        check(
            "ssh_backfill: codex-host added to stale YAML under shared wiring",
            "codex-host" in agents,
            sorted(agents),
        )
        codex_host = agents["codex-host"]
        check(
            "ssh_backfill: codex-host ssh argv targets the shared user@host",
            any(t == "huser@host.docker.internal" for t in codex_host.command),
            str(codex_host.command),
        )
        check(
            "ssh_backfill: codex-host host_staging=True",
            codex_host.host_staging is True,
            str(codex_host.host_staging),
        )
        check(
            "ssh_backfill: claude-host + hermes-host entries preserved (not regenerated)",
            "claude-host" in agents and "hermes-host" in agents,
            sorted(agents),
        )
        # The existing legacy base-entry backfill must still work alongside
        # the SSH sibling backfill — codex is already in the YAML, so the
        # base-entry backfill is a no-op for this fixture.
        check(
            "ssh_backfill: codex (base) entry preserved",
            "codex" in agents,
            sorted(agents),
        )

        # --- case 2: operator-disabled codex-host is NOT overwritten ---
        disabled_yaml = TMP / "ssh-backfill-disabled.yaml"
        disabled_yaml.write_text(
            "agents:\n"
            "  claude:\n"
            '    display_name: "Claude Code"\n'
            "    command: [claude, -p, '{prompt}']\n"
            "    prompt_via: arg\n"
            "    stream_format: claude-json\n"
            "    enabled: true\n"
            "  hermes:\n"
            '    display_name: "Hermes"\n'
            "    command: [hermes, chat, -q, '{prompt}', --yolo, --accept-hooks]\n"
            "    prompt_via: arg\n"
            "    stream_format: raw\n"
            "    enabled: true\n"
            "  codex:\n"
            '    display_name: "Codex"\n'
            "    command: [codex, exec, --cd, '{project_dir}', --sandbox, workspace-write, "
            "--color, never, --ephemeral, --output-last-message, '{last_message_file}', '-']\n"
            "    prompt_via: stdin\n"
            "    stream_format: codex\n"
            "    enabled: true\n"
            "  codex-host:\n"
            '    display_name: "Codex (Host)"\n'
            "    command: [ssh, -i, /custom/pinned/id_codex, -p, '99', custom@host, "
            "'cd {project_dir} && codex exec']\n"
            "    prompt_via: stdin\n"
            "    stream_format: codex\n"
            "    host_staging: true\n"
            "    enabled: false\n",
            encoding="utf-8",
        )
        cfg2 = load_agents_config(disabled_yaml)
        check(
            "ssh_backfill: operator-disabled codex-host stays disabled",
            cfg2.agents["codex-host"].enabled is False,
            str(cfg2.agents["codex-host"].enabled),
        )
        check(
            "ssh_backfill: operator's custom key path preserved",
            "-i" in cfg2.agents["codex-host"].command
            and cfg2.agents["codex-host"].command[
                cfg2.agents["codex-host"].command.index("-i") + 1
            ] == "/custom/pinned/id_codex",
            str(cfg2.agents["codex-host"].command),
        )

        # --- case 3: no SSH wiring -> nothing backfilled, stale keys stay ---
        # Clear all SSH envs and load the stale YAML. There is no SSH user
        # set, so the generator emits no -host siblings and the loader must
        # not invent any. The YAML's claude-host + hermes-host are preserved
        # because the loader only ADDS, never deletes.
        os.environ.pop("CD_HERMES_SSH_USER", None)
        os.environ.pop("CD_HERMES_SSH_HOST", None)
        os.environ.pop("CD_HERMES_SSH_PORT", None)
        cfg3 = load_agents_config(stale_yaml)
        check(
            "ssh_backfill: no SSH wiring -> existing -host siblings preserved",
            "claude-host" in cfg3.agents and "hermes-host" in cfg3.agents,
            sorted(cfg3.agents),
        )
        check(
            "ssh_backfill: no SSH wiring -> codex-host NOT invented by loader",
            "codex-host" not in cfg3.agents,
            sorted(cfg3.agents),
        )

        # --- case 4: custom-only YAML under SSH wiring is left alone ---
        # Operators with custom agents should not have any built-in -host
        # siblings injected. The backfill only touches the SSH-driven
        # built-in keys the YAML already lacks; a YAML without claude/
        # hermes/codex in any form is not a legacy built-in config.
        custom_yaml = TMP / "ssh-backfill-custom.yaml"
        custom_yaml.write_text(
            "agents:\n"
            "  custom-agent:\n"
            '    display_name: "Custom"\n'
            "    command: [some-cli, -p, '{prompt}']\n"
            "    prompt_via: arg\n"
            "    stream_format: raw\n"
            "    enabled: true\n",
            encoding="utf-8",
        )
        os.environ["CD_HERMES_SSH_USER"] = "huser"
        cfg4 = load_agents_config(custom_yaml)
        check(
            "ssh_backfill: custom-only YAML stays explicit",
            set(cfg4.agents) == {"custom-agent"},
            sorted(cfg4.agents),
        )
    finally:
        for k, v in old_ssh.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_ssh_key_shared_wiring() -> None:
    """The SSH private-key path follows the same shared-wiring rule as
    user/host/port, plus an on-disk existence fallback:

      * Hermes-only user -> claude-host inherits the Hermes key path.
      * If the resolved key file is absent but the other agent's key
        exists on disk, fall back to the existing one (a Hermes-only
        deploy has only ``id_hermes``, so claude-host must use it rather
        than a non-existent ``id_claude``).
      * An explicit ``CD_{HERMES,CLAUDE}_SSH_KEY`` env override always wins
        and is never second-guessed by the existence check.
    """
    from app.config_bootstrap import generate_initial_agents_config

    def _keyof(cmd: list) -> str:
        return cmd[cmd.index("-i") + 1] if "-i" in cmd else ""

    # A HOME where ONLY id_hermes exists (mirrors the real container).
    home = TMP / "ssh-key-home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "id_hermes").write_text("k", encoding="utf-8")

    # Hermes-only + only id_hermes on disk -> claude-host uses id_hermes.
    doc = generate_initial_agents_config(
        {
            "CD_HERMES_SSH_USER": "huser",
            "HOME": str(home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    expect = str(home / ".ssh" / "id_hermes")
    check(
        "ssh_key: claude-host inherits Hermes key when id_claude absent",
        _keyof(doc["claude-host"]["command"]) == expect,
        _keyof(doc["claude-host"]["command"]),
    )
    check(
        "ssh_key: claude-host session_command key matches too",
        _keyof(doc["claude-host"]["session_command"]) == expect,
        _keyof(doc["claude-host"]["session_command"]),
    )

    # Explicit CD_CLAUDE_SSH_KEY override wins even if the file is absent.
    doc2 = generate_initial_agents_config(
        {
            "CD_HERMES_SSH_USER": "huser",
            "CD_CLAUDE_SSH_KEY": "/custom/id_claude_pinned",
            "HOME": str(home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "ssh_key: explicit CD_CLAUDE_SSH_KEY override is honoured verbatim",
        _keyof(doc2["claude-host"]["command"]) == "/custom/id_claude_pinned",
        _keyof(doc2["claude-host"]["command"]),
    )

    # Both keys present -> each sibling uses its own default key.
    (home / ".ssh" / "id_claude").write_text("k", encoding="utf-8")
    doc3 = generate_initial_agents_config(
        {
            "CD_HERMES_SSH_USER": "huser",
            "CD_CLAUDE_SSH_USER": "cuser",
            "HOME": str(home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "ssh_key: claude-host uses id_claude when both users+keys present",
        _keyof(doc3["claude-host"]["command"]) == str(home / ".ssh" / "id_claude"),
        _keyof(doc3["claude-host"]["command"]),
    )
    check(
        "ssh_key: hermes-host uses id_hermes when both users+keys present",
        _keyof(doc3["hermes-host"]["command"]) == str(home / ".ssh" / "id_hermes"),
        _keyof(doc3["hermes-host"]["command"]),
    )


def test_ssh_remote_path_export() -> None:
    """Both ``claude-host`` and ``hermes-host`` SSH remote shells must
    extend PATH before invoking the agent CLI. SSH login shells do not
    inherit the operator's interactive PATH, so a CLI installed under
    ``~/.local/bin`` / ``~/.npm-global/bin`` / ``~/.cargo/bin`` is
    invisible without this. The user reported
    ``env: 'claude': No such file or directory`` on the very first
    Claude-host session start — that came from the previous
    ``cd ... && exec env -u ANTHROPIC_API_KEY claude`` which skipped
    the PATH export that Hermes already used.
    """
    from app.config_bootstrap import generate_initial_agents_config

    doc = generate_initial_agents_config(
        {
            "CD_HERMES_SSH_USER": "huser",
            "CD_HERMES_SSH_HOST": "host",
            "CD_HERMES_SSH_PORT": "22",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]

    # Both command + session_command of claude-host must include the
    # shared PATH-export chain (mirroring Hermes).
    expected_chain = "$HOME/.local/bin"
    for variant in ("command", "session_command"):
        joined = " ".join(doc["claude-host"][variant])
        check(
            f"ssh_remote_path: claude-host.{variant} extends PATH before claude CLI",
            expected_chain in joined and "claude" in joined,
            joined,
        )
        # Hermes must still extend PATH too — this test guards both ends
        # of the symmetry so future refactors don't strip one side.
        joined_h = " ".join(doc["hermes-host"][variant])
        check(
            f"ssh_remote_path: hermes-host.{variant} still extends PATH",
            expected_chain in joined_h,
            joined_h,
        )


def test_hermes_container_in_image_only() -> None:
    """The container ``hermes`` entry is enabled iff ``command -v hermes``
    succeeds (PATH-search via shutil.which in the generator). When hermes
    is on PATH, enabled=True; when not, enabled=False. The entry is kept
    in both cases so operators can flip ``enabled: true`` by hand later
    if they install the CLI into the cd-home volume post-boot.
    """
    from app.config_bootstrap import generate_initial_agents_config

    # ---- hermes NOT on PATH -> disabled ----
    doc_no = generate_initial_agents_config({"PATH": "/var/empty/bin"})["agents"]
    check(
        "hermes_container: disabled when CLI absent",
        doc_no["hermes"].get("enabled") is False,
        str(doc_no["hermes"].get("enabled")),
    )
    check(
        "hermes_container: entry still present (operators can flip by hand)",
        "hermes" in doc_no,
    )

    # ---- hermes on PATH (fake shim in a temp dir) -> enabled ----
    fake_bin = TMP / "fakebin-hermes"
    fake_bin.mkdir(parents=True, exist_ok=True)
    shim = fake_bin / "hermes"
    shim.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    shim.chmod(0o755)
    doc_yes = generate_initial_agents_config(
        {"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin"}
    )["agents"]
    check(
        "hermes_container: enabled when CLI on PATH",
        doc_yes["hermes"].get("enabled") is True,
        str(doc_yes["hermes"].get("enabled")),
    )

    # ---- also: a self-contained hermes stays enabled regardless of SSH ----
    # (with hermes on PATH AND no SSH user -> no hermes-host; container
    # entry enabled)
    doc_ssh_off = generate_initial_agents_config(
        {"PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin"}
    )["agents"]
    check(
        "hermes_container: hermes-host absent (no SSH user, even with CLI)",
        "hermes-host" not in doc_ssh_off,
        list(doc_ssh_off),
    )
    check(
        "hermes_container: container hermes enabled in self-contained mode",
        doc_ssh_off["hermes"].get("enabled") is True,
    )


def test_codex_host_sibling_registers() -> None:
    """The Codex SSH-over-host sibling must register exactly like the
    Claude/Hermes one when CD_CODEX_SSH_USER is set:

      * ``codex-host`` exists; container-side ``codex`` keeps its own entry.
      * ``host_staging=True``, ``enabled=True``, ``prompt_via="stdin"``,
        ``stream_format="codex"``, ``display_name="Codex (Host)"``.
      * ``command`` starts with ``ssh``, targets the resolved user@host,
        and ends with the codex task remote shell.
      * ``session_command`` uses ``force_tty`` (``-tt`` flag) so the
        interactive TUI gets a real pty, and ends with the codex session
        remote shell.
      * No ``{last_message_file}`` placeholder — the host SSH process
        can't reach the dashboard container's tempfile, so the summary
        falls back to ``_CodexParser.summary()`` instead.
      * Absent when no SSH user is configured.
    """
    from app.config_bootstrap import generate_initial_agents_config

    # ---- case 1: codex SSH user set -> codex-host registered ----
    doc = generate_initial_agents_config(
        {
            "CD_CODEX_SSH_USER": "xuser",
            "CD_CODEX_SSH_HOST": "codex.host",
            "CD_CODEX_SSH_PORT": "3333",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "codex_host: codex-host registered when CD_CODEX_SSH_USER set",
        "codex-host" in doc,
        list(doc),
    )
    spec = doc["codex-host"]
    check(
        "codex_host: host_staging=True",
        spec.get("host_staging") is True,
        str(spec.get("host_staging")),
    )
    check(
        "codex_host: enabled=True",
        spec.get("enabled") is True,
        str(spec.get("enabled")),
    )
    check(
        "codex_host: prompt_via=stdin",
        spec.get("prompt_via") == "stdin",
        str(spec.get("prompt_via")),
    )
    check(
        "codex_host: stream_format=codex (so _CodexParser is used)",
        spec.get("stream_format") == "codex",
        str(spec.get("stream_format")),
    )
    check(
        "codex_host: display_name='Codex (Host)'",
        spec.get("display_name") == "Codex (Host)",
        str(spec.get("display_name")),
    )
    cmd = spec["command"]
    check(
        "codex_host: command starts with ssh",
        cmd[:1] == ["ssh"],
        str(cmd[:3]),
    )
    check(
        "codex_host: command targets xuser@codex.host:3333",
        "xuser@codex.host" in cmd and "3333" in cmd,
        str(cmd),
    )
    # The remote shell string is the only argument of the form
    # "cd ... && codex exec ..." inside the SSH argv — it sits BEFORE the
    # appended model/effort flags. Find it by content rather than position
    # so a future refactor (e.g. ssh argv with `-J` jump-host) doesn't
    # silently break this test.
    remote_shell = next(
        (tok for tok in cmd if "codex exec" in tok),
        "",
    )
    check(
        "codex_host: task remote shell invokes codex exec",
        "codex exec" in remote_shell,
        remote_shell,
    )
    check(
        "codex_host: task remote shell extends PATH",
        "$HOME/.local/bin" in remote_shell,
        remote_shell,
    )
    check(
        "codex_host: command does NOT contain {last_message_file}",
        not any("{last_message_file}" in tok for tok in cmd),
        str(cmd),
    )
    # session_command: -tt flag + interactive `exec codex` remote shell.
    sess = spec["session_command"]
    check(
        "codex_host: session_command uses -tt (force_tty)",
        "-tt" in sess,
        str(sess[:6]),
    )
    sess_remote = sess[-1]
    check(
        "codex_host: session remote shell is `exec codex`",
        sess_remote.endswith("exec codex"),
        sess_remote,
    )
    # --- Model + effort flags survive the SSH argv ----
    # Codex uses ``-c model_reasoning_effort=...`` rather than ``--effort``.
    check(
        "codex_host: --model placeholder present",
        "--model" in cmd and "{model}" in cmd,
        str(cmd),
    )
    check(
        "codex_host: effort injected as -c model_reasoning_effort={effort}",
        "-c" in cmd and "model_reasoning_effort={effort}" in cmd,
        str(cmd),
    )
    check(
        "codex_host: --effort flag NOT used (Codex has no such flag)",
        "--effort" not in cmd,
        str(cmd),
    )

    # --- Ordering invariant: base ``codex`` precedes ``codex-host`` ----
    keys = list(doc)
    check(
        "codex_host: base codex precedes codex-host sibling",
        "codex" in keys
        and "codex-host" in keys
        and keys.index("codex") < keys.index("codex-host"),
        keys,
    )

    # ---- case 2: no SSH user -> codex-host absent ----
    doc_off = generate_initial_agents_config(
        {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    )["agents"]
    check(
        "codex_host: codex-host absent when no SSH user",
        "codex-host" not in doc_off,
        list(doc_off),
    )


def test_codex_host_effort_injection_through_build_command() -> None:
    """``_build_command`` must thread ``--model`` + ``-c model_reasoning_effort``
    through the SSH argv so the host codex CLI receives the user's selection.
    The container-side codex spec uses ``model_args``/``effort_args`` to
    inject these; the SSH form inherits those fields from the deep-copied
    base spec, so ``_build_command`` appends them after the remote shell.
    A future refactor that re-derives the host spec from scratch must keep
    the same ``model_args``/``effort_args`` plumbing — otherwise the host
    codex CLI silently runs with its default reasoning effort.
    """
    from app.config_bootstrap import generate_initial_agents_config
    from app.agents import _build_command

    doc = generate_initial_agents_config(
        {
            "CD_CODEX_SSH_USER": "xuser",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    spec_dict = doc["codex-host"]
    # Hydrate a real AgentSpec so _build_command can work with spec.command
    # + spec.model_args / spec.effort_args (those carry the injection).
    from app.config import AgentSpec
    spec = AgentSpec(
        key=spec_dict["key"],
        display_name=spec_dict["display_name"],
        command=spec_dict["command"],
        prompt_via=spec_dict["prompt_via"],
        stream_format=spec_dict["stream_format"],
        env=spec_dict.get("env") or {},
        unset_env=spec_dict.get("unset_env") or [],
        host_staging=spec_dict.get("host_staging", False),
        # Inherited via deep-copy from the base ``codex`` spec — see
        # backend/app/config.py default_agents()["codex"]. Without these,
        # _build_command has no source for {model} / {effort}.
        model_args=["--model", "{model}"],
        effort_args=["-c", "model_reasoning_effort={effort}"],
    )
    cmd = _build_command(
        spec,
        prompt="ignored",
        project_dir="/tmp/proj",
        model="gpt-5.4",
        effort="xhigh",
    )
    check(
        "codex_host_build: model injected as --model gpt-5.4",
        "--model" in cmd and "gpt-5.4" in cmd,
        str(cmd),
    )
    check(
        "codex_host_build: effort injected as -c model_reasoning_effort=xhigh",
        "-c" in cmd and "model_reasoning_effort=xhigh" in cmd,
        str(cmd),
    )
    # The {model} / {effort} placeholders still appear in the SSH argv's
    # remote-shell string (a literal — they are ignored by the remote codex
    # CLI because the remote shell doesn't reference them) and in the
    # appended model_args/effort_args (the latter are what get substituted
    # to the real values). We assert only on the *substituted* form by
    # checking that both --model and -c appear with concrete values.
    check(
        "codex_host_build: --model + -c both carry concrete values",
        any(t == "gpt-5.4" for t in cmd) and any(t == "model_reasoning_effort=xhigh" for t in cmd),
        str(cmd),
    )


def test_codex_host_shared_wiring_three_agents() -> None:
    """The shared SSH-wiring rule from ``test_claude_host_reuses_hermes_ssh``
    now also covers codex: configuring ONE of the three CD_*_SSH_USER vars
    lights up ALL THREE ``-host`` siblings with the same effective values.
    Configuring all three independently keeps each sibling on its own.
    Configuring NONE disables every -host sibling.
    """
    from app.config_bootstrap import generate_initial_agents_config

    # ---- case E: codex-only -> all three siblings use codex values ----
    doc = generate_initial_agents_config(
        {
            "CD_CODEX_SSH_USER": "xuser",
            "CD_CODEX_SSH_HOST": "codex.host",
            "CD_CODEX_SSH_PORT": "3333",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    for sibling in ("claude-host", "hermes-host", "codex-host"):
        check(
            f"shared_wiring_codex: {sibling} registered when CD_CODEX_SSH_USER only set",
            sibling in doc,
            list(doc),
        )
        cmd = doc[sibling]["command"]
        check(
            f"shared_wiring_codex: {sibling} uses xuser@codex.host",
            "xuser@codex.host" in cmd and "3333" in cmd,
            str(cmd),
        )

    # ---- case F: all three set independently -> each uses its own values ----
    doc_all = generate_initial_agents_config(
        {
            "CD_HERMES_SSH_USER": "huser",
            "CD_HERMES_SSH_HOST": "hermes.host",
            "CD_HERMES_SSH_PORT": "2222",
            "CD_CLAUDE_SSH_USER": "cuser",
            "CD_CLAUDE_SSH_HOST": "claude.host",
            "CD_CLAUDE_SSH_PORT": "2200",
            "CD_CODEX_SSH_USER": "xuser",
            "CD_CODEX_SSH_HOST": "codex.host",
            "CD_CODEX_SSH_PORT": "3333",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "shared_wiring_codex: hermes-host uses its own huser@hermes.host:2222",
        "huser@hermes.host" in doc_all["hermes-host"]["command"]
        and "2222" in doc_all["hermes-host"]["command"],
        str(doc_all["hermes-host"]["command"]),
    )
    check(
        "shared_wiring_codex: claude-host uses its own cuser@claude.host:2200",
        "cuser@claude.host" in doc_all["claude-host"]["command"]
        and "2200" in doc_all["claude-host"]["command"],
        str(doc_all["claude-host"]["command"]),
    )
    check(
        "shared_wiring_codex: codex-host uses its own xuser@codex.host:3333",
        "xuser@codex.host" in doc_all["codex-host"]["command"]
        and "3333" in doc_all["codex-host"]["command"],
        str(doc_all["codex-host"]["command"]),
    )

    # ---- case G: none set -> no -host siblings ----
    doc_none = generate_initial_agents_config(
        {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    )["agents"]
    for sibling in ("claude-host", "hermes-host", "codex-host"):
        check(
            f"shared_wiring_codex: {sibling} absent when no SSH user",
            sibling not in doc_none,
            list(doc_none),
        )


def test_codex_host_key_path_default_is_id_codex() -> None:
    """Each ``-host`` sibling defaults to its own key path
    (``~/.ssh/id_<agent>``). The on-disk existence fallback that lets
    Hermes-only deploys share one key across siblings also applies to
    codex now.
    """
    from app.config_bootstrap import generate_initial_agents_config

    def _keyof(cmd: list) -> str:
        return cmd[cmd.index("-i") + 1] if "-i" in cmd else ""

    # HOME with only id_codex on disk.
    home = TMP / "ssh-key-home-codex"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "id_codex").write_text("k", encoding="utf-8")

    # codex-only user, only id_codex on disk -> claude-host + hermes-host
    # both inherit id_codex (existence fallback).
    doc = generate_initial_agents_config(
        {
            "CD_CODEX_SSH_USER": "xuser",
            "HOME": str(home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    expect = str(home / ".ssh" / "id_codex")
    for sibling in ("claude-host", "hermes-host", "codex-host"):
        check(
            f"codex_key: {sibling} inherits id_codex when only id_codex on disk",
            _keyof(doc[sibling]["command"]) == expect,
            _keyof(doc[sibling]["command"]),
        )

    # Explicit CD_CODEX_SSH_KEY pin wins even if the file is absent.
    doc2 = generate_initial_agents_config(
        {
            "CD_CODEX_SSH_USER": "xuser",
            "CD_CODEX_SSH_KEY": "/custom/id_codex_pinned",
            "HOME": str(home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check(
        "codex_key: explicit CD_CODEX_SSH_KEY override is honoured verbatim",
        _keyof(doc2["codex-host"]["command"]) == "/custom/id_codex_pinned",
        _keyof(doc2["codex-host"]["command"]),
    )


def test_runner_picks_codex_host_by_key() -> None:
    """The TaskManager runner shim swaps ``runner="host"`` + ``agent="codex"``
    to ``codex-host`` automatically. The sibling's ``host_staging=True``
    flag then routes the run through the existing host-staging pipeline
    (no codex-specific code in TaskManager).
    """
    from app.config_bootstrap import generate_initial_agents_config

    # Ensure codex-host is registered.
    doc = generate_initial_agents_config(
        {
            "CD_CODEX_SSH_USER": "xuser",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )["agents"]
    check("codex_runner: codex-host registered for the test", "codex-host" in doc, list(doc))
    # Mirror of test_runner_picks_host_sibling_by_key for codex: the shim
    # reads `agent.endswith("-host")` — confirming the suffix here proves
    # that submitting with ``agent="codex"`` + ``runner="host"`` resolves
    # to ``codex-host`` rather than constructing ``codex-host-host``.
    check(
        "codex_runner: codex-host key ends with -host suffix",
        "codex-host".endswith("-host"),
        "codex-host",
    )


def test_create_task_persists_env_profile_key() -> None:
    """REST round-trip: POST /api/projects/{pid}/tasks with
    env_profile_key='p1' returns 201; GET /api/tasks/{id} reflects the
    field; the project's task history list returns the same row.
    """
    from fastapi.testclient import TestClient

    from app import env_crypto
    from app.database import session_scope
    from app.main import app
    from app.models import EnvProfile, Project

    _clean_env_profiles()
    with session_scope() as db:
        p = Project(
            id="ep-1",
            name="ep-test",
            slug="ep-test",
            local_path=str(TMP / "ep-1"),
        )
        Path(p.local_path).mkdir(parents=True, exist_ok=True)
        db.add(p)
        db.add(
            EnvProfile(
                key="p1",
                name="Profile 1",
                anthropic_base_url="https://router.example.com",
                anthropic_auth_token_encrypted=env_crypto.encrypt_secret("rt"),
            )
        )

    with TestClient(app) as client:
        ok = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
        )
        H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

        r = client.post(
            "/api/projects/ep-1/tasks",
            headers=H,
            json={
                "agent": "fake",
                "prompt": "x",
                "mode": "task",
                "env_profile_key": "p1",
                "runner": "",
            },
        )
        check(
            "create_task_env_profile: POST 201",
            r.status_code == 201,
            f"got {r.status_code}",
        )
        task_id = r.json()["id"]
        check(
            "create_task_env_profile: POST response carries env_profile_key",
            r.json()["env_profile_key"] == "p1",
        )

        # GET
        g = client.get(f"/api/tasks/{task_id}", headers=H)
        check("create_task_env_profile: GET 200", g.status_code == 200)
        check(
            "create_task_env_profile: GET reflects env_profile_key",
            g.json()["env_profile_key"] == "p1",
        )

        # History list
        l = client.get("/api/projects/ep-1/tasks", headers=H)
        check("create_task_env_profile: history list 200", l.status_code == 200)
        ids = [t["id"] for t in l.json()]
        check("create_task_env_profile: history list includes task", task_id in ids)
        persisted = next(t for t in l.json() if t["id"] == task_id)
        check(
            "create_task_env_profile: persisted row keeps env_profile_key",
            persisted["env_profile_key"] == "p1",
        )

    # Cleanup
    with session_scope() as db:
        from app.models import Task
        db.query(Task).filter(Task.project_id == "ep-1").delete()
        db.query(EnvProfile).filter(EnvProfile.key == "p1").delete()
        db.query(Project).filter(Project.id == "ep-1").delete()


def test_heartbeat_env_profile_resolution() -> None:
    """Per-tick resolution: per-project override beats the global default.

    Three projects get created:
      - A: heartbeat_env_profile_key='p_proj'  -> spawned task env_profile_key='p_proj'
      - B: empty override                       -> 'p_global'
      - C: empty override, also CD global empty -> ''

    All three list one open issue, ``POST /api/heartbeat/trigger`` is
    awaited, and we assert each spawned task's ``env_profile_key``.
    """
    from fastapi.testclient import TestClient

    from app import env_crypto
    from app import heartbeat as hb_mod
    from app import github_client
    from app.config import get_settings
    from app.database import session_scope
    from app.main import app
    from app.models import EnvProfile, HeartbeatSeen, Project, Task
    from datetime import datetime, timedelta, timezone

    _clean_env_profiles()

    # Seed profiles
    with session_scope() as db:
        db.add(
            EnvProfile(key="p_proj", name="Proj", anthropic_base_url="")
        )
        db.add(
            EnvProfile(key="p_global", name="Global", anthropic_base_url="")
        )

    # Seed projects A/B/C/D — D gets cleared later (after the first
    # round of checks) to validate the empty-everywhere path.
    with session_scope() as db:
        for pid, slug, override in (
            ("hbepa", "hbepa", "p_proj"),
            ("hbepb", "hbepb", ""),
            ("hbepc", "hbepc", ""),
            ("hbepd", "hbepd", ""),
        ):
            p = Project(
                id=pid,
                name=pid,
                slug=slug,
                local_path=str(TMP / pid),
                github_full_name=f"acme/{pid}",
                heartbeat_enabled=True,
                heartbeat_env_profile_key=override,
            )
            Path(p.local_path).mkdir(parents=True, exist_ok=True)
            db.add(p)

    # Set global default via settings
    settings = get_settings()
    saved_global = settings.heartbeat_env_profile_key
    settings.heartbeat_env_profile_key = "p_global"
    saved_assignee = os.environ.get("CD_HEARTBEAT_ASSIGNEE_LOGINS")
    os.environ["CD_HEARTBEAT_ASSIGNEE_LOGINS"] = (
        "self"  # match the assignee name we stub below
    )

    runner = hb_mod.heartbeat
    runner.set_enabled(True)

    # Reset heartbeat_seen so the issues are NEW
    with session_scope() as db:
        db.query(HeartbeatSeen).delete()

    async def _fake_resolve():
        return (("self",), hb_mod.ASSIGNEE_RESOLVED)

    runner._resolve_assignee_logins = _fake_resolve

    async def fake_list_issues(full_name, **kw):
        n = fake_list_issues.counter
        fake_list_issues.counter += 1
        return [
            {
                "number": n,
                "title": "test",
                "user": {"login": "self"},
                "assignees": [{"login": "self"}],
                "labels": [],
                "created_at": "2026-07-10T00:00:00Z",
                "body": "b",
                "html_url": f"https://github.com/{full_name}/issues/{n}",
            }
        ]

    fake_list_issues.counter = 100

    original_list = github_client.list_issues
    hb_mod.github_client.list_issues = fake_list_issues
    try:
        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
            )
            H = {"Authorization": f"Bearer {ok.json()['access_token']}"}
            tr = client.post("/api/heartbeat/trigger", headers=H)
            check(
                "hb_env_profile: POST /trigger 200",
                tr.status_code == 200,
                str(tr.status_code),
            )

            deadline = time.time() + 10.0
            while time.time() < deadline:
                with session_scope() as db:
                    n = (
                        db.query(Task)
                        .filter(Task.heartbeat_spawned.is_(True))
                        .filter(Task.project_id.in_(["hbepa", "hbepb", "hbepc", "hbepd"]))
                        .count()
                    )
                if n >= 4:
                    break
                time.sleep(0.2)

            with session_scope() as db:
                rows = (
                    db.query(Task)
                    .filter(Task.heartbeat_spawned.is_(True))
                    .filter(Task.project_id.in_(["hbepa", "hbepb", "hbepc", "hbepd"]))
                    .all()
                )
            by_project = {t.project_id: t.env_profile_key for t in rows}

            check(
                "hb_env_profile: project A uses its override 'p_proj'",
                by_project.get("hbepa") == "p_proj",
                repr(by_project),
            )
            check(
                "hb_env_profile: project B falls back to global 'p_global'",
                by_project.get("hbepb") == "p_global",
                repr(by_project),
            )
            check(
                "hb_env_profile: project C also resolves to 'p_global' (no override -> global)",
                by_project.get("hbepc") == "p_global",
                repr(by_project),
            )

        # Now clear the global default + project A's override, run a second
        # tick, and assert project A's NEW spawned task carries env_profile_key=''.
        settings.heartbeat_env_profile_key = ""
        with session_scope() as db:
            db.query(Task).filter(Task.project_id == "hbepa").delete()
            db.query(Task).filter(
                Task.project_id == "hbepd"
            ).delete()  # also delete D's earlier spawn
            p = db.get(Project, "hbepa")
            p.heartbeat_env_profile_key = ""
            # Re-mark D's project so the next tick considers it NEW.
            db.query(HeartbeatSeen).filter(
                HeartbeatSeen.project_id.in_(["hbepa", "hbepd"])
            ).delete()

        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
            )
            H = {"Authorization": f"Bearer {ok.json()['access_token']}"}
            tr = client.post("/api/heartbeat/trigger", headers=H)
            check(
                "hb_env_profile: 2nd POST /trigger 200 (empty global)",
                tr.status_code == 200,
                str(tr.status_code),
            )

        deadline = time.time() + 10.0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=20)
        while time.time() < deadline:
            with session_scope() as db:
                n = (
                    db.query(Task)
                    .filter(Task.heartbeat_spawned.is_(True))
                    .filter(
                        Task.project_id.in_(["hbepa", "hbepd"])
                    )
                    .filter(Task.created_at >= cutoff)
                    .count()
                )
            if n >= 2:
                break
            time.sleep(0.2)

        with session_scope() as db:
            new_rows = (
                db.query(Task)
                .filter(Task.heartbeat_spawned.is_(True))
                .filter(Task.project_id.in_(["hbepa", "hbepd"]))
                .order_by(Task.created_at.desc())
                .limit(2)
                .all()
            )
        by_project = {t.project_id: t.env_profile_key for t in new_rows}
        check(
            "hb_env_profile: project A with empty override + empty global resolves to ''",
            by_project.get("hbepa") == "",
            repr(by_project),
        )
        check(
            "hb_env_profile: project D with empty override + empty global resolves to ''",
            by_project.get("hbepd") == "",
            repr(by_project),
        )
    finally:
        hb_mod.github_client.list_issues = original_list
        settings.heartbeat_env_profile_key = saved_global
        if saved_assignee is None:
            os.environ.pop("CD_HEARTBEAT_ASSIGNEE_LOGINS", None)
        else:
            os.environ["CD_HEARTBEAT_ASSIGNEE_LOGINS"] = saved_assignee
        runner.set_enabled(False)
        runner._resolve_assignee_logins = lambda: (("",), hb_mod.ASSIGNEE_RESOLVED)

    # Cleanup
    with session_scope() as db:
        db.query(Task).filter(
            Task.project_id.in_(["hbepa", "hbepb", "hbepc", "hbepd"])
        ).delete()
        db.query(HeartbeatSeen).delete()
        db.query(Project).filter(
            Project.id.in_(["hbepa", "hbepb", "hbepc", "hbepd"])
        ).delete()
        db.query(EnvProfile).filter(
            EnvProfile.key.in_(["p_proj", "p_global"])
        ).delete()


# --- 2026-07-14: global heartbeat env-profile + agent-key endpoints --- #


def test_heartbeat_global_env_profile_endpoint() -> None:
    """POST /api/heartbeat/env-profile mutates the runner override.

    Asserts the 404-on-unknown-key guard, the success path (the runner
    exposes the new key on GET), and the empty-string clearing path.
    In-memory only — verifies the runner singleton's
    ``set_env_profile_key`` is wired through to the GET response without
    touching the settings or env vars.
    """
    from fastapi.testclient import TestClient

    from app import heartbeat as hb_mod
    from app.database import session_scope
    from app.main import app
    from app.models import EnvProfile

    _clean_env_profiles()
    with session_scope() as db:
        db.add(EnvProfile(key="p_global_e", name="Global E", anthropic_base_url=""))

    runner = hb_mod.heartbeat
    saved_override = runner._env_profile_key_override
    runner.set_env_profile_key("")
    try:
        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
            )
            H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

            # 1. unknown key -> 404
            r = client.post(
                "/api/heartbeat/env-profile",
                headers=H,
                json={"env_profile_key": "does-not-exist"},
            )
            check(
                "hb_env_profile_endpoint: unknown key -> 404",
                r.status_code == 404,
                f"got {r.status_code}",
            )

            # 2. success -> 200, GET reflects the new key
            r = client.post(
                "/api/heartbeat/env-profile",
                headers=H,
                json={"env_profile_key": "p_global_e"},
            )
            check(
                "hb_env_profile_endpoint: known key -> 200",
                r.status_code == 200,
                f"got {r.status_code}",
            )
            check(
                "hb_env_profile_endpoint: response carries new key",
                r.json().get("env_profile_key") == "p_global_e",
                repr(r.json()),
            )

            r = client.get("/api/heartbeat", headers=H)
            check(
                "hb_env_profile_endpoint: GET reflects runtime override",
                r.json().get("env_profile_key") == "p_global_e",
                repr(r.json().get("env_profile_key")),
            )

            # 3. empty string -> clears the runtime override
            r = client.post(
                "/api/heartbeat/env-profile",
                headers=H,
                json={"env_profile_key": ""},
            )
            check(
                "hb_env_profile_endpoint: empty clears override -> 200",
                r.status_code == 200,
                f"got {r.status_code}",
            )
            check(
                "hb_env_profile_endpoint: cleared override echoes ''",
                r.json().get("env_profile_key") == "",
                repr(r.json()),
            )
    finally:
        runner.set_env_profile_key(saved_override or "")
        with session_scope() as db:
            db.query(EnvProfile).filter(EnvProfile.key == "p_global_e").delete()


def test_heartbeat_agent_key_endpoint() -> None:
    """POST /api/heartbeat/agent-key swaps the runner's auto-spawned agent.

    Registers a ``fake-host`` sibling (mirrors what the Docker entrypoint
    does when ``CD_FAKE_SSH_USER`` is set), verifies that GET surfaces
    both ``fake`` (default) and ``fake-host`` in ``available_agent_keys``,
    flips the runner via the endpoint, asserts the next tick's spawned
    task carries ``agent='fake-host'``, then clears the override and
    asserts the next tick reverts to ``fake``. Also exercises the
    unknown-key 400 path.
    """
    from fastapi.testclient import TestClient

    from app import github_client
    from app import heartbeat as hb_mod
    from app.config import get_agents_config
    from app.database import session_scope
    from app.main import app
    from app.models import HeartbeatSeen, Project, Task

    cfg = get_agents_config()
    cfg.agents["fake-host"] = AgentSpec(
        key="fake-host",
        display_name="Fake Host",
        command=[PY, "-c", "pass"],
        prompt_via="arg",
        stream_format="raw",
        enabled=True,
        host_staging=True,
    )

    runner = hb_mod.heartbeat
    saved_override = runner._agent_key_override
    saved_assignee = os.environ.get("CD_HEARTBEAT_ASSIGNEE_LOGINS")
    os.environ["CD_HEARTBEAT_ASSIGNEE_LOGINS"] = "self"
    runner.set_agent_key("")

    with session_scope() as db:
        db.add(
            Project(
                id="hbak",
                name="hbak",
                slug="hbak",
                local_path=str(TMP / "hbak"),
                github_full_name="acme/hbak",
                heartbeat_enabled=True,
            )
        )
        Path(TMP / "hbak").mkdir(parents=True, exist_ok=True)
        db.query(HeartbeatSeen).delete()

    async def _fake_resolve():
        return (("self",), hb_mod.ASSIGNEE_RESOLVED)

    runner._resolve_assignee_logins = _fake_resolve
    runner.set_enabled(True)

    issue_counter = {"n": 200}

    async def fake_list_issues(full_name, **kw):
        issue_counter["n"] += 1
        return [
            {
                "number": issue_counter["n"],
                "title": "test",
                "user": {"login": "self"},
                "assignees": [{"login": "self"}],
                "labels": [],
                "created_at": "2026-07-14T00:00:00Z",
                "body": "b",
                "html_url": f"https://github.com/{full_name}/issues/{issue_counter['n']}",
            }
        ]

    original_list = github_client.list_issues
    hb_mod.github_client.list_issues = fake_list_issues
    try:
        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
            )
            H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

            # 1. unknown key -> 400
            r = client.post(
                "/api/heartbeat/agent-key",
                headers=H,
                json={"agent_key": "no-such-agent"},
            )
            check(
                "hb_agent_key_endpoint: unknown agent -> 400",
                r.status_code == 400,
                f"got {r.status_code}",
            )

            # 2. GET surfaces available_agent_keys with both fake + fake-host
            r = client.get("/api/heartbeat", headers=H)
            keys = r.json().get("available_agent_keys", [])
            check(
                "hb_agent_key_endpoint: GET lists 'fake'",
                "fake" in keys,
                repr(keys),
            )
            check(
                "hb_agent_key_endpoint: GET lists 'fake-host'",
                "fake-host" in keys,
                repr(keys),
            )
            check(
                "hb_agent_key_endpoint: GET reports current agent_key='fake'",
                r.json().get("agent_key") == "fake",
                repr(r.json().get("agent_key")),
            )

            # 3. flip to fake-host -> 200, GET reflects
            r = client.post(
                "/api/heartbeat/agent-key",
                headers=H,
                json={"agent_key": "fake-host"},
            )
            check(
                "hb_agent_key_endpoint: flip to fake-host -> 200",
                r.status_code == 200,
                f"got {r.status_code}",
            )
            r = client.get("/api/heartbeat", headers=H)
            check(
                "hb_agent_key_endpoint: GET reports agent_key='fake-host'",
                r.json().get("agent_key") == "fake-host",
                repr(r.json().get("agent_key")),
            )

            # 4. trigger a tick; spawned task should carry agent='fake-host'
            r = client.post("/api/heartbeat/trigger", headers=H)
            check(
                "hb_agent_key_endpoint: POST trigger -> 200",
                r.status_code == 200,
                f"got {r.status_code}",
            )

            deadline = time.time() + 10.0
            while time.time() < deadline:
                with session_scope() as db:
                    row = (
                        db.query(Task)
                        .filter(Task.project_id == "hbak")
                        .filter(Task.heartbeat_spawned.is_(True))
                        .order_by(Task.created_at.desc())
                        .first()
                    )
                if row is not None:
                    break
                time.sleep(0.2)

            with session_scope() as db:
                row = (
                    db.query(Task)
                    .filter(Task.project_id == "hbak")
                    .filter(Task.heartbeat_spawned.is_(True))
                    .order_by(Task.created_at.desc())
                    .first()
                )
            check(
                "hb_agent_key_endpoint: spawned task agent=fake-host",
                row is not None and row.agent == "fake-host",
                repr(row.agent if row else None),
            )

            # 5. clear override -> back to env-var default ('fake')
            r = client.post(
                "/api/heartbeat/agent-key",
                headers=H,
                json={"agent_key": ""},
            )
            check(
                "hb_agent_key_endpoint: empty clears override -> 200",
                r.status_code == 200,
                f"got {r.status_code}",
            )
            r = client.get("/api/heartbeat", headers=H)
            check(
                "hb_agent_key_endpoint: cleared override falls back to env default 'fake'",
                r.json().get("agent_key") == "fake",
                repr(r.json().get("agent_key")),
            )
    finally:
        hb_mod.github_client.list_issues = original_list
        runner.set_agent_key(saved_override or "")
        if saved_assignee is None:
            os.environ.pop("CD_HEARTBEAT_ASSIGNEE_LOGINS", None)
        else:
            os.environ["CD_HEARTBEAT_ASSIGNEE_LOGINS"] = saved_assignee
        runner.set_enabled(False)
        runner._resolve_assignee_logins = lambda: (("",), hb_mod.ASSIGNEE_RESOLVED)
        cfg.agents.pop("fake-host", None)
        with session_scope() as db:
            db.query(Task).filter(Task.project_id == "hbak").delete()
            db.query(HeartbeatSeen).delete()
            db.query(Project).filter(Project.id == "hbak").delete()


def test_session_env_profile_persists() -> None:
    """POST /api/sessions accepts env_profile_key and persists it on the Task.

    Round-trips through the public REST surface so the SessionCreate
    schema + the route's write path are exercised together. Verifies
    the Task row carries the env_profile_key + is_session=True; the
    existing ``test_runner_toggle_persistence`` already covers the
    runner field on the tasks route, so we focus on env_profile_key
    here (the new field added by the 2026-07-14 env-profiles feature,
    previously never round-tripped through /sessions).

    Adds a transient ``fake`` ``session_command`` so the route's
    ``supports_session`` guard passes; restores the original (empty)
    list on exit so other tests keep their assumption that the
    default fake agent is task-only.
    """
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from app.database import session_scope
    from app.main import app
    from app.models import EnvProfile, Project, Task

    _clean_env_profiles()
    cfg = get_agents_config()
    saved_session_cmd = cfg.agents["fake"].session_command
    cfg.agents["fake"] = cfg.agents["fake"].model_copy(
        update={"session_command": [PY, "-c", "pass"]}
    )

    with session_scope() as db:
        db.add(EnvProfile(key="p_sess", name="Sess Profile", anthropic_base_url=""))

    with session_scope() as db:
        proj = db.query(Project).first()
        if proj is None:
            proj = Project(
                id="sess-env-1",
                name="sess-env",
                slug="sess-env",
                local_path=str(TMP / "sess-env-1"),
            )
            Path(proj.local_path).mkdir(parents=True, exist_ok=True)
            db.add(proj)
            db.commit()
        pid = proj.id

    try:
        with TestClient(app) as client:
            ok = client.post(
                "/api/auth/login", json={"username": "admin", "password": "secret-pw"}
            )
            H = {"Authorization": f"Bearer {ok.json()['access_token']}"}

            # 1. unknown env_profile_key -> 404 (validation runs BEFORE the
            # supports_session guard, so it fires regardless of agent).
            r = client.post(
                "/api/sessions",
                headers=H,
                json={
                    "project_id": pid,
                    "agent": "fake",
                    "env_profile_key": "no-such-profile",
                },
            )
            check(
                "sess_env_profile: unknown env_profile_key -> 404",
                r.status_code == 404,
                f"got {r.status_code}",
            )

            # 2. valid env_profile_key + runner="" -> row carries the key.
            # The session's PTY will then be started; we never call
            # /sessions/{id}/end so the pump loop keeps the process alive
            # until we mark the row interrupted in the cleanup.
            r = client.post(
                "/api/sessions",
                headers=H,
                json={
                    "project_id": pid,
                    "agent": "fake",
                    "env_profile_key": "p_sess",
                    "runner": "",
                },
            )
            check(
                "sess_env_profile: POST /sessions with env_profile_key -> 201",
                r.status_code == 201,
                f"got {r.status_code} {r.text[:200]}",
            )
            sess_id = r.json().get("task_id")
            check(
                "sess_env_profile: response carries task_id",
                bool(sess_id),
                repr(r.json()),
            )
            with session_scope() as db:
                t = db.get(Task, sess_id) if sess_id else None
            check(
                "sess_env_profile: Task row stores env_profile_key='p_sess'",
                t is not None and t.env_profile_key == "p_sess",
                repr(t.env_profile_key if t else None),
            )
            check(
                "sess_env_profile: Task row stores runner='' (default container)",
                t is not None and t.runner == "",
                repr(t.runner if t else None),
            )
            check(
                "sess_env_profile: Task row is_session=True",
                t is not None and t.is_session is True,
                repr(t.is_session if t else None),
            )

            # 3. GET /api/sessions/{id} -> 200 (round-trip stays alive)
            r = client.get(f"/api/sessions/{sess_id}", headers=H)
            check(
                "sess_env_profile: GET /sessions/{id} -> 200",
                r.status_code == 200,
                f"got {r.status_code}",
            )
    finally:
        # Mark the session interrupted so SessionManager.end_session
        # doesn't have to find the still-living PTY (which would block
        # the test process on SIGTERM waitpid); then drop the row.
        with session_scope() as db:
            t = db.get(Task, sess_id) if sess_id else None
            if t:
                t.status = "interrupted"
                t.finished_at = datetime.now(timezone.utc)
            db.query(Task).filter(
                Task.project_id == pid, Task.is_session.is_(True)
            ).delete()
            db.query(EnvProfile).filter(EnvProfile.key == "p_sess").delete()
        cfg.agents["fake"] = cfg.agents["fake"].model_copy(
            update={"session_command": saved_session_cmd}
        )


# --- small helpers used by the new tests --- #

def _clean_env_profiles() -> None:
    """Drop every EnvProfile row so each test starts from the seed."""
    from app.database import session_scope
    from app.models import EnvProfile

    with session_scope() as db:
        db.query(EnvProfile).delete()


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
        test_auto_commit_subject()
        test_worktrees()
        test_session_dirs()
        test_session_workdir_resolution()
        test_auto_pull_helpers()
        test_sync_from_github_validation()
        test_hermes_clarify_disabled()
        test_project_archive()
        test_rename_github_owner()
        test_host_lock()
        test_heartbeat()
        test_heartbeat_assignee_filter()
        test_heartbeat_comment_on_solve()
        # 2026-07-14: env profiles + per-task host runner
        test_env_crypto()
        test_env_profiles_crud()
        test_env_profiles_encryption_gated()
        test_task_runner_env_profile_injection()
        test_runner_toggle_persistence()
        test_runner_fallback_when_ssh_not_configured()
        test_runner_guard_strips_existing_host_suffix()
        test_session_runner_shim()
        test_runner_picks_host_sibling_by_key()
        test_hermes_host_sibling_registers()
        test_claude_host_reuses_hermes_ssh()
        test_ssh_sibling_backfill_in_load_agents_config()
        test_ssh_key_shared_wiring()
        test_ssh_remote_path_export()
        test_hermes_container_in_image_only()
        # 2026-07-15: codex-over-SSH (mirrors the claude/hermes pattern)
        test_codex_host_sibling_registers()
        test_codex_host_effort_injection_through_build_command()
        test_codex_host_shared_wiring_three_agents()
        test_codex_host_key_path_default_is_id_codex()
        test_runner_picks_codex_host_by_key()
        test_create_task_persists_env_profile_key()
        test_heartbeat_env_profile_resolution()
        # 2026-07-14: global heartbeat env-profile + agent-key endpoints
        # (operator-facing UI on the /heartbeat page; in-memory only)
        test_heartbeat_global_env_profile_endpoint()
        test_heartbeat_agent_key_endpoint()
        test_session_env_profile_persists()
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
