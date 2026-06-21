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
