"""Resolve the working directory for an interactive (session-mode) agent.

Coding-agent CLIs tie their saved conversations to the directory they were
launched in:

* **Claude Code** writes ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``
  and refuses to resume from any other directory ("No conversation found with
  session ID: …").
* **Codex** stores rollouts globally under ``~/.codex/sessions/`` but records the
  original ``cwd`` in each rollout's ``session_meta`` and filters its resume
  picker by the current directory.

That is why resuming a finished session only works when the agent is spawned in
the *same* folder the session was created in. At the same time, running every
session in the shared project folder makes true parallel work impossible (two
agents fighting over the same files / git index).

This module bridges both needs:

* :func:`parse_resume_request` detects, from the user-supplied start parameters,
  whether a session resume is requested and which session id (if any).
* :func:`resolve_recorded_cwd` looks the session up in the agent's own store and
  returns the directory it was originally created in, so the resumed agent finds
  it again — no matter which (possibly isolated) folder produced it.

New sessions, by contrast, can be placed in their own throwaway git worktree by
the caller (see ``task_runner``), keeping parallel sessions isolated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class ResumeRequest:
    """A resume intent parsed out of a session's start parameters.

    ``kind`` is one of:
      * ``"id"``       – resume a specific session (``session_id`` is set);
      * ``"last"``     – resume the most recent session (``--last``);
      * ``"continue"`` – continue the most recent session in the directory
                          (``--continue`` / bare ``resume``).
    """

    kind: str
    session_id: Optional[str] = None


# Claude / Hermes share the same flag style.
_RESUME_FLAGS = {"--resume", "-r"}
_CONTINUE_FLAGS = {"--continue", "-c"}


def parse_resume_request(agent_key: str, argv: list[str]) -> Optional[ResumeRequest]:
    """Detect a resume intent in the start-parameter ``argv`` (no binary name).

    Returns ``None`` when the start parameters do not ask to resume anything.
    """
    argv = [a for a in argv if a]
    if not argv:
        return None

    if agent_key == "codex":
        # Codex resume is a subcommand: ``resume [SESSION_ID|--last]``.
        if "resume" in argv:
            rest = argv[argv.index("resume") + 1:]
            if "--last" in rest:
                return ResumeRequest("last")
            for tok in rest:
                if not tok.startswith("-"):
                    return ResumeRequest("id", tok)
            return ResumeRequest("continue")
        if "--last" in argv:
            return ResumeRequest("last")
        return None

    # Claude / Hermes flag style.
    for i, tok in enumerate(argv):
        if tok in _RESUME_FLAGS:
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt and not nxt.startswith("-"):
                return ResumeRequest("id", nxt)
            return ResumeRequest("continue")
        if tok.startswith("--resume="):
            value = tok.split("=", 1)[1].strip()
            return ResumeRequest("id", value) if value else ResumeRequest("continue")
        if tok in _CONTINUE_FLAGS:
            return ResumeRequest("continue")
        if tok == "--last":
            return ResumeRequest("last")
    return None


def resolve_recorded_cwd(
    agent_key: str, session_id: str, *, home: Optional[Path] = None
) -> Optional[str]:
    """Return the directory a saved session was originally created in, or None.

    Looks the session up in the agent's own on-disk store. Best effort: any I/O
    or parsing problem yields ``None`` so the caller can fall back gracefully.
    """
    if not session_id:
        return None
    home = home or Path.home()
    try:
        if agent_key == "claude":
            return _claude_session_cwd(session_id, home)
        if agent_key == "codex":
            return _codex_session_cwd(session_id, home)
    except OSError:
        return None
    return None


def _iter_jsonl(path: Path, max_lines: int = 500) -> Iterator[dict]:
    """Yield parsed JSON objects from a .jsonl file, skipping junk lines."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _claude_session_cwd(session_id: str, home: Path) -> Optional[str]:
    base = home / ".claude" / "projects"
    if not base.is_dir():
        return None
    # Session files are named ``<session-id>.jsonl`` under a per-cwd directory.
    matches = list(base.glob(f"*/{session_id}.jsonl"))
    if not matches:
        matches = list(base.glob(f"*/{session_id}*.jsonl"))
    for f in matches:
        for obj in _iter_jsonl(f):
            cwd = obj.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
    return None


def _codex_session_cwd(session_id: str, home: Path) -> Optional[str]:
    base = home / ".codex" / "sessions"
    if not base.is_dir():
        return None
    # Codex embeds the session uuid in the rollout filename; use the glob only to
    # narrow the candidate set (cheaper than reading every rollout) but ALWAYS
    # verify the id inside ``session_meta`` — a filename substring match is not
    # proof of identity (one id can be a substring of another's filename).
    matches = list(base.glob(f"**/*{session_id}*.jsonl"))
    candidates = matches or sorted(base.glob("**/rollout-*.jsonl"), reverse=True)
    for f in candidates:
        for obj in _iter_jsonl(f, max_lines=1):
            if obj.get("type") != "session_meta":
                break  # only the first line carries session_meta
            payload = obj.get("payload") or {}
            if not isinstance(payload, dict):
                break
            if payload.get("id") == session_id or payload.get("name") == session_id:
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
            break  # only the first line carries session_meta
    return None
