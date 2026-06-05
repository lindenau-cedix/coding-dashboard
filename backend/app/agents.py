"""Run a coding-agent CLI as a subprocess and stream its output."""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .config import AgentSpec

# Strip ANSI escape sequences (colors, cursor moves, spinners) so raw agent
# output renders cleanly in the plain-text web console.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))")

OutputCallback = Callable[[str], Awaitable[None]]

# Extra PATH entries so a minimal systemd PATH still finds the agent binaries.
_EXTRA_PATH = [
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/local/sbin",
    "/usr/sbin",
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / ".claude" / "local"),
    str(Path.home() / ".npm-global" / "bin"),
]


@dataclass
class AgentResult:
    exit_code: int
    transcript: str  # human-readable log (stored as Task.output)
    summary: str  # short final result text
    is_error: bool = False


def _build_env(spec: AgentSpec) -> dict[str, str]:
    env = dict(os.environ)
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for p in _EXTRA_PATH:
        if p and p not in parts:
            parts.append(p)
    env["PATH"] = os.pathsep.join(parts)
    env.update(spec.env)
    for key in spec.unset_env:
        env.pop(key, None)
    return env


def _build_command(spec: AgentSpec, prompt: str, project_dir: str) -> list[str]:
    out: list[str] = []
    for tok in spec.command:
        tok = tok.replace("{project_dir}", project_dir)
        if spec.prompt_via == "arg":
            tok = tok.replace("{prompt}", prompt)
        out.append(tok)
    return out


def _tail(text: str, n: int = 600) -> str:
    text = text.strip()
    return text[-n:] if len(text) > n else text


# --------------------------------------------------------------------------- #
# Output parsers
# --------------------------------------------------------------------------- #
class _RawParser:
    is_error = False

    def feed(self, line: str) -> str:
        return _ANSI_RE.sub("", line)

    def summary(self) -> str:
        return ""


class _ClaudeJSONParser:
    """Turns Claude Code's ``--output-format stream-json`` into readable text."""

    def __init__(self) -> None:
        self.is_error = False
        self._summary = ""

    def feed(self, line: str) -> str:
        line = line.rstrip("\n")
        if not line.strip():
            return ""
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return line + "\n"  # stray non-JSON log line
        if not isinstance(evt, dict):
            return ""
        return self._format(evt)

    def _format(self, evt: dict) -> str:
        t = evt.get("type")
        if t == "system":
            if evt.get("subtype") == "init":
                model = evt.get("model", "")
                return f"[claude] Session gestartet{f' ({model})' if model else ''}\n"
            return ""
        if t == "assistant":
            return self._format_message(evt.get("message", {}))
        if t == "result":
            res = evt.get("result")
            if isinstance(res, str):
                self._summary = res
            elif res is not None:
                self._summary = json.dumps(res, ensure_ascii=False)
            self.is_error = bool(evt.get("is_error"))
            return ""
        return ""

    def _format_message(self, message: dict) -> str:
        parts: list[str] = []
        for block in message.get("content", []) or []:
            bt = block.get("type")
            if bt == "text":
                parts.append(block.get("text", ""))
            elif bt == "tool_use":
                parts.append(f"\n[tool] {block.get('name', 'tool')}\n")
        text = "".join(parts)
        return text + ("\n" if text and not text.endswith("\n") else "")

    def summary(self) -> str:
        return self._summary


def _make_parser(stream_format: str):
    if stream_format == "claude-json":
        return _ClaudeJSONParser()
    return _RawParser()


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
async def run_agent(
    spec: AgentSpec, prompt: str, project_dir: str, on_output: OutputCallback
) -> AgentResult:
    cmd = _build_command(spec, prompt, project_dir)
    env = _build_env(spec)
    cwd = (spec.cwd or "{project_dir}").replace("{project_dir}", project_dir) or project_dir

    await on_output(f"$ {' '.join(spec.command)}\n")
    await on_output(f"[cwd] {cwd}\n\n")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=(
                asyncio.subprocess.PIPE
                if spec.prompt_via == "stdin"
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        msg = (
            f"[Fehler] Agent-Binary nicht gefunden: {cmd[0]!r}. "
            f"Pruefe 'command' in config.yaml und den PATH des Service-Users.\n({exc})\n"
        )
        await on_output(msg)
        return AgentResult(127, msg, "Binary nicht gefunden", is_error=True)

    if spec.prompt_via == "stdin" and proc.stdin is not None:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

    parser = _make_parser(spec.stream_format)
    transcript: list[str] = []

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            display = parser.feed(raw.decode("utf-8", errors="replace"))
            if display:
                transcript.append(display)
                await on_output(display)

    try:
        if spec.timeout_seconds:
            await asyncio.wait_for(pump(), timeout=spec.timeout_seconds)
        else:
            await pump()
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        msg = f"\n[Timeout nach {spec.timeout_seconds}s -- Agent abgebrochen]\n"
        transcript.append(msg)
        await on_output(msg)
        return AgentResult(124, "".join(transcript), "Timeout", is_error=True)
    except asyncio.CancelledError:
        proc.kill()
        raise

    exit_code = proc.returncode if proc.returncode is not None else -1
    full = "".join(transcript)
    summary = parser.summary() or _tail(full)
    is_error = exit_code != 0 or parser.is_error
    return AgentResult(exit_code, full, summary, is_error=is_error)
