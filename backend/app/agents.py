"""Run a coding-agent CLI as a subprocess and stream its output."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
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
    str(Path.home() / ".codex" / "bin"),
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


def _build_env_for(
    spec: AgentSpec, *, extra: "dict[str, str] | None" = None
) -> dict[str, str]:
    """``_build_env`` + a per-call overlay (``extra`` later wins).

    The overlay is the channel through which ``task_runner`` layers a
    per-task env-profile (ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN) onto
    the spawned subprocess.  ``ANTHROPIC_API_KEY=""`` lives in the overlay
    by design so a host shell cannot leak an inherited upstream token.
    Empty / None ``extra`` is a no-op (returns ``_build_env(spec)``
    unchanged), so call sites that have nothing to overlay pay no cost.
    """
    env = _build_env(spec)
    if extra:
        env.update(extra)
    return env


def _write_claude_settings(effort: str) -> None:
    """Write effort to ~/.claude/settings.json so Claude Code honours it."""
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not isinstance(settings, dict):
        settings = {}
    settings["effort"] = effort
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _write_codex_config(model: str, effort: str) -> None:
    """Write model + model_reasoning_effort to ~/.codex/config.toml before Codex starts."""
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing config or start fresh
    lines: list[str] = []
    if config_path.exists():
        try:
            lines = config_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

    # Build a dict of existing key=value lines
    # Keep the raw value (with or without quotes) so formatting is preserved on rewrite.
    cfg: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip()

    # Update the relevant keys
    if model:
        cfg["model"] = f'"{model}"'
    if effort:
        cfg["model_reasoning_effort"] = f'"{effort}"'

    # Rewrite the file, preserving comments and unknown keys
    new_lines: list[str] = []
    seen_keys: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in cfg:
                new_lines.append(f"{key} = {cfg[key]}")
                seen_keys.add(key)
                continue
        new_lines.append(line)
    # Append any keys that weren't in the original file
    for key in ("model", "model_reasoning_effort"):
        if key not in seen_keys and key in cfg:
            new_lines.append(f"{key} = {cfg[key]}")

    config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _model_effort_extra(spec: AgentSpec, model: str, effort: str) -> list[str]:
    """The argv tokens injected for the user's model/effort selection.

    ``{model}`` / ``{effort}`` in the spec's ``model_args`` / ``effort_args``
    are substituted with the chosen values.  Shared by ``_build_command``
    (task/goal mode) and the interactive-session builder in ``task_runner`` so
    both apply the selection the same way.
    """
    extra: list[str] = []
    if model and spec.model_args:
        extra += [t.replace("{model}", model) for t in spec.model_args]
    if effort and spec.effort_args:
        extra += [t.replace("{effort}", effort) for t in spec.effort_args]
    return extra


def _build_command(
    spec: AgentSpec,
    prompt: str,
    project_dir: str,
    model: str = "",
    effort: str = "",
    last_message_file: str = "",
) -> list[str]:
    out: list[str] = []
    for tok in spec.command:
        tok = tok.replace("{project_dir}", project_dir)
        tok = tok.replace("{last_message_file}", last_message_file)
        if spec.prompt_via == "arg":
            tok = tok.replace("{prompt}", prompt)
        out.append(tok)

    # Inject the user's model/effort selection. Inserted before a trailing "-"
    # (stdin marker, e.g. codex) so the prompt positional stays last; appended
    # otherwise. This keeps explicit `command` lists in config.yaml working.
    extra = _model_effort_extra(spec, model, effort)
    if extra:
        if out and out[-1] == "-":
            out = out[:-1] + extra + ["-"]
        else:
            out += extra
    return out


# Box-drawing/block characters some CLIs (hermes) draw around their final answer.
_BOX_CHARS_RE = re.compile(r"[─-╿▀-▟]")
# Session footers that follow the final answer in raw transcripts.
_RAW_FOOTER_RE = re.compile(
    r"^(resume this session with:?|hermes --resume\b|codex resume\b|to continue this session\b"
    r"|session:|duration:|messages:|tokens used:?)",
    re.IGNORECASE,
)


def _final_output(text: str, max_chars: int = 2000) -> str:
    """Best-effort extraction of the agent's FINAL message from a raw transcript.

    Strips box-drawing decoration and known session footers (hermes/codex),
    then returns the last paragraph block -- i.e. only the closing answer, not
    intermediate tool output. Used when an agent provides no structured result.
    """
    cleaned: list[str] = []
    for line in text.splitlines():
        line = _BOX_CHARS_RE.sub("", line).rstrip()
        if _RAW_FOOTER_RE.match(line.strip()):
            continue
        cleaned.append(line)

    blocks: list[list[str]] = [[]]
    for line in cleaned:
        if line.strip():
            blocks[-1].append(line.strip())
        elif blocks[-1]:
            blocks.append([])
    if blocks and not blocks[-1]:
        blocks.pop()
    if not blocks:
        return ""

    # Only the last paragraph counts -- that IS the agent's final output.
    result = "\n".join(blocks[-1]).strip()
    if len(result) > max_chars:
        # Clip from the BEGINNING so the most important conclusion (which is
        # at the start of the final paragraph) is preserved, not discarded.
        cut = result[max_chars - 200 : max_chars].find("\n")
        if cut >= 0:
            result = result[: max_chars - 200 + cut]
        else:
            result = result[:max_chars]
        result = result.strip() + "\n[... gekürzt ...]"
    return result


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
                name = block.get("name", "tool")
                detail = self._tool_detail(block)
                parts.append(f"\n[tool] {name}{f': {detail}' if detail else ''}\n")
        text = "".join(parts)
        return text + ("\n" if text and not text.endswith("\n") else "")

    # Input keys that make a useful one-line preview, most specific first.
    _TOOL_PREVIEW_KEYS = (
        "command",
        "file_path",
        "path",
        "pattern",
        "query",
        "url",
        "description",
        "skill",
        "prompt",
    )

    @classmethod
    def _tool_detail(cls, block: dict) -> str:
        """One-line preview of a tool call (command, file path, ...)."""
        inp = block.get("input")
        # Fall back to json.dumps for any non-dict input (string, list, None…)
        if not isinstance(inp, dict):
            try:
                return cls._one_line(json.dumps(inp, ensure_ascii=False))
            except (TypeError, ValueError):
                return ""
        for key in cls._TOOL_PREVIEW_KEYS:
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                return cls._one_line(val)
        try:
            return cls._one_line(json.dumps(inp, ensure_ascii=False))
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _one_line(text: str, limit: int = 160) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def summary(self) -> str:
        return self._summary


class _CodexParser:
    """Declutter ``codex exec`` output for the web console.

    ``codex exec --color never`` prefixes every event with an ISO timestamp,
    prints a metadata banner (workdir/model/provider/sandbox/…), echoes the
    FULL prompt back under ``User instructions:`` (which in our case includes
    the long shared context instruction), and appends ``tokens used:`` footers.
    This parser strips that noise and turns the event markers into clean,
    readable prefixes while keeping the agent's reasoning, commands and answer.
    The exact final answer still comes from ``--output-last-message`` (handled
    by the runner), so ``summary()`` stays empty here.
    """

    # "[2026-06-13T20:00:01] <event>" — codex stamps every top-level event.
    _TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[\d:.,+\-Zz]+\]\s?(.*)$")
    # Metadata banner field lines / separators emitted near the top.
    _BANNER_RE = re.compile(
        r"^(-{3,}|workdir|model|provider|approval|sandbox|reasoning effort"
        r"|reasoning summaries|session|rollout|version)\b",
        re.IGNORECASE,
    )
    _FOOTER_RE = re.compile(r"^tokens used\b", re.IGNORECASE)

    def __init__(self) -> None:
        self.is_error = False
        self._summary = ""
        # While echoing the user prompt back we drop everything until the next event.
        self._skip_prompt_echo = False
        # The metadata banner (workdir/model/sandbox/…) only appears at the very
        # top.  Once a real event has streamed we stop banner-matching so answer
        # text that happens to start with "model"/"session"/… is never dropped.
        self._past_header = False

    def feed(self, line: str) -> str:
        line = _ANSI_RE.sub("", line).rstrip("\n")
        m = self._TS_RE.match(line)
        if m is not None:
            return self._event(m.group(1).strip())
        # Continuation line (no timestamp): part of the current section.
        if self._skip_prompt_echo:
            return ""
        if not self._past_header and self._BANNER_RE.match(line.strip()):
            return ""
        return line + "\n"

    def _event(self, rest: str) -> str:
        low = rest.lower()
        # The version banner ("OpenAI Codex v0.139.0 …") and metadata separators.
        if not self._past_header and (
            low.startswith("openai codex") or self._BANNER_RE.match(rest)
        ):
            self._skip_prompt_echo = False
            return ""
        if low.startswith("user instructions"):
            self._skip_prompt_echo = True
            self._past_header = True
            return ""
        if self._FOOTER_RE.match(rest):
            self._skip_prompt_echo = False
            return ""
        # From here on we're past the prompt echo and the header banner.
        self._skip_prompt_echo = False
        self._past_header = True
        if low == "thinking":
            return "\n[denkt nach]\n"
        if low == "codex":
            # Marker introducing the final answer; drop it, keep what follows.
            return ""
        if low.startswith("exec "):
            return self._format_exec(rest[5:].strip())
        return rest + "\n"

    @staticmethod
    def _format_exec(body: str) -> str:
        """``bash -lc 'ls -la' in /proj`` -> ``$ ls -la``."""
        body = re.sub(r"\s+in\s+\S+$", "", body)
        m = re.match(r"^bash -lc ['\"](.*)['\"]$", body, re.DOTALL)
        cmd = m.group(1) if m else body
        return f"$ {cmd}\n"

    def summary(self) -> str:
        return self._summary


def _make_parser(stream_format: str):
    if stream_format == "claude-json":
        return _ClaudeJSONParser()
    if stream_format == "codex":
        return _CodexParser()
    return _RawParser()


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
async def run_agent(
    spec: AgentSpec,
    prompt: str,
    project_dir: str,
    on_output: OutputCallback,
    model: str = "",
    effort: str = "",
) -> AgentResult:
    # Some CLIs (codex --output-last-message) can write their FINAL message to
    # a file; that beats any transcript heuristic, so prefer it as summary.
    last_message_path: Path | None = None
    if any("{last_message_file}" in tok for tok in spec.command):
        fd, tmp = tempfile.mkstemp(prefix="cd-last-msg-", suffix=".txt")
        os.close(fd)
        last_message_path = Path(tmp)

    try:
        return await _run_agent_inner(
            spec, prompt, project_dir, on_output, model, effort, last_message_path
        )
    finally:
        if last_message_path is not None:
            last_message_path.unlink(missing_ok=True)


async def _run_agent_inner(
    spec: AgentSpec,
    prompt: str,
    project_dir: str,
    on_output: OutputCallback,
    model: str,
    effort: str,
    last_message_path: Path | None,
) -> AgentResult:
    cmd = _build_command(
        spec,
        prompt,
        project_dir,
        model=model,
        effort=effort,
        last_message_file=str(last_message_path) if last_message_path else "",
    )
    env = _build_env(spec)
    cwd = (spec.cwd or "{project_dir}").replace("{project_dir}", project_dir) or project_dir

    await on_output(f"$ {' '.join(spec.command)}\n")
    if model or effort:
        sel = " ".join(x for x in (model, effort) if x)
        await on_output(f"[auswahl] {sel}\n")
    await on_output(f"[cwd] {cwd}\n\n")

    # For Claude Code, write effort to ~/.claude/settings.json so it is honoured
    # even if the --effort flag is absent from the command template.
    if effort and spec.key == "claude":
        _write_claude_settings(effort)

    # For Codex, write model + model_reasoning_effort to ~/.codex/config.toml
    # so Codex uses exactly the values the user selected in the dashboard.
    # The host-side ``codex-host`` sibling gets the same treatment so the
    # SSH-driven run on the host picks up the same selections.
    if (model or effort) and spec.key in ("codex", "codex-host"):
        _write_codex_config(model, effort)

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

    async def emit(raw_line: bytes) -> None:
        display = parser.feed(raw_line.decode("utf-8", errors="replace"))
        if display:
            transcript.append(display)
            await on_output(display)

    async def pump() -> None:
        assert proc.stdout is not None
        pending = bytearray()
        while True:
            raw = await proc.stdout.read(16384)
            if not raw:
                break
            pending.extend(raw)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                line = bytes(pending[: newline + 1])
                del pending[: newline + 1]
                await emit(line)
        if pending:
            await emit(bytes(pending))

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
    summary = _read_last_message(last_message_path) or parser.summary() or _final_output(full)
    is_error = exit_code != 0 or parser.is_error
    return AgentResult(exit_code, full, summary, is_error=is_error)


def _read_last_message(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
