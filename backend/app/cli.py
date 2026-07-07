"""Small admin CLI.

Usage (from the backend/ directory, venv active):

    python -m app.cli hash-password            # prompts, prints a pbkdf2 hash
    python -m app.cli hash-password 'secret'   # non-interactive

    python -m app.cli rename-github-owner <OLD> <NEW> [--apply] [--limit N]
        Rewrite every project's github_full_name / github_url / clone_url
        so the GitHub owner prefix changes OLD -> NEW.  Dry-run by default
        (prints the per-row diff but does NOT commit).  Pass ``--apply``
        to commit in one transaction; the per-project last_issue_poll_at
        cursor is cleared on the renamed projects so the next heartbeat
        tick re-polls from scratch.  ``heartbeat_seen`` is preserved
        (issue numbers are stable across an account rename).

        Examples::

            python -m app.cli rename-github-owner faultierGPT lindenau-cedix
            python -m app.cli rename-github-owner faultierGPT lindenau-cedix --apply
"""
from __future__ import annotations

import getpass
import re
import sys
from datetime import datetime, timezone

from sqlalchemy import func

from .database import session_scope
from .models import Project
from .security import hash_password


def _validate_owner(value: str, label: str) -> str | None:
    """Owners are bare GitHub handles (no slash, no whitespace)."""
    if not value:
        return f"{label} must not be empty"
    if "/" in value:
        return f"{label} must not contain '/': got {value!r}"
    if any(c.isspace() for c in value):
        return f"{label} must not contain whitespace: got {value!r}"
    if not re.match(r"^[A-Za-z0-9._-]+$", value):
        return f"{label} has unsupported characters: got {value!r}"
    return None


def _format_owner_rewrite(p: Project, old: str, new: str) -> str:
    old_full = p.github_full_name or ""
    new_full = old_full.replace(f"{old}/", f"{new}/", 1) if old_full else old_full
    old_clone = p.clone_url or ""
    new_clone = old_clone.replace(f"{old}/", f"{new}/", 1) if old_clone else old_clone
    poll = p.last_issue_poll_at
    poll_str = (
        poll.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(poll, datetime)
        else "(none)"
    )
    return (
        f"  {p.id}  {p.slug}\n"
        f"      github_full_name: {old_full} -> {new_full}\n"
        f"      clone_url host   : {old_clone} -> {new_clone}\n"
        f"      last_poll_at     : {poll_str} (cleared on --apply)"
    )


def _rename_github_owner(
    old: str, new: str, apply: bool, limit: int
) -> int:
    """Shared body: dry-run preview + apply. Returns process exit code."""
    if old == new:
        print(
            f"error: OLD and NEW are identical ({old!r}); nothing to do.",
            file=sys.stderr,
        )
        return 1

    dry_label = "DRY-RUN" if not apply else "APPLY"
    print(
        f"[{dry_label}] rewrite projects.github_full_name / github_url / clone_url:"
    )
    print(f"           '{old}/' -> '{new}/'")
    print()

    if not apply:
        with session_scope() as db:
            rows = (
                db.query(Project)
                .filter(Project.github_full_name.like(f"{old}/%"))
                .order_by(Project.name)
                .all()
            )
            preview_rows = rows[: max(0, limit)]
            print(f"Would update {len(rows)} project(s):")
            for p in preview_rows:
                print(_format_owner_rewrite(p, old, new))
            if limit and len(rows) > limit:
                print(f"  ... ({len(rows) - limit} more not shown; --limit N to change)")
            print()
            total = db.query(Project).count()
            print(
                f"Projects matched: {len(rows)}  "
                f"Not matching prefix: {total - len(rows)}  Total: {total}"
            )
        print()
        print("Pass --apply to commit.")
        return 0

    # Apply path: a single transaction, one commit.
    with session_scope() as db:
        fn_count = db.query(Project).filter(
            Project.github_full_name.like(f"{old}/%")
        ).count()
        url_count = db.query(Project).filter(
            Project.github_url.like(f"%{old}/%")
        ).count()
        clone_count = db.query(Project).filter(
            Project.clone_url.like(f"%{old}/%")
        ).count()

        # Three owner-stamped columns. The ``REPLACE(str, from, to)`` SQL
        # function (SQLite + Postgres both implement it) substitutes every
        # occurrence of ``OLD/`` with ``NEW/`` in one shot; matching
        # ``f'{old}/%'`` ensures we only touch rows whose owner is exactly
        # ``OLD`` (not substring matches in description etc.).
        # NOTE: github_url / clone_url go through ``*/OLD/`` because they
        # embed the owner between slashes
        # (e.g. ``https://github.com/OLD/repo.git``).
        #
        # The per-project last_issue_poll_at cursor is cleared on the SAME
        # UPDATE that rewrites github_full_name, so the WHERE clause
        # (matching ``OLD/%`` BEFORE the rewrite) still finds the rows.
        # Doing it as a separate UPDATE later would match zero rows after
        # the rewrite flipped them to ``NEW/...``.
        cleared = db.execute(
            Project.__table__.update()
            .where(Project.github_full_name.like(f"{old}/%"))
            .values(
                github_full_name=func.replace(
                    Project.github_full_name, f"{old}/", f"{new}/"
                ),
                last_issue_poll_at=None,
            )
        ).rowcount

        db.execute(
            Project.__table__.update()
            .where(Project.github_url.like(f"%/{old}/%"))
            .values(
                github_url=func.replace(
                    Project.github_url, f"/{old}/", f"/{new}/"
                )
            )
        )
        db.execute(
            Project.__table__.update()
            .where(Project.clone_url.like(f"%/{old}/%"))
            .values(
                clone_url=func.replace(
                    Project.clone_url, f"/{old}/", f"/{new}/"
                )
            )
        )

    print(f"Apply: '{old}/' -> '{new}/'")
    print(f"  github_full_name     : {fn_count} row(s) matched (rewritten)")
    print(f"  github_url           : {url_count} row(s) matched (rewritten)")
    print(f"  clone_url            : {clone_count} row(s) matched (rewritten)")
    print(f"  last_issue_poll_at   : cleared on {cleared} row(s)")
    print(f"  heartbeat_seen       : NOT touched (issue numbers are stable)")
    print()
    print("Done.")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "hash-password":
        if len(rest) > 0:
            pw = rest[0]
        else:
            pw = getpass.getpass("Password: ")
            if pw != getpass.getpass("Repeat:   "):
                print("Passwords do not match", file=sys.stderr)
                return 1
        if not pw:
            print("Empty password", file=sys.stderr)
            return 1
        print(hash_password(pw))
        return 0

    if cmd == "rename-github-owner":
        if not rest or any(t in {"-h", "--help", "help"} for t in rest):
            print(
                "rename-github-owner <OLD> <NEW> [--apply] [--limit N]\n"
                "  Rewrite every project's github_full_name / github_url /\n"
                "  clone_url so the GitHub owner prefix changes OLD -> NEW.\n"
                "  Dry-run by default (prints the per-row diff but does NOT\n"
                "  commit). Pass --apply to commit in one transaction; the\n"
                "  per-project last_issue_poll_at cursor is cleared on the\n"
                "  renamed projects so the next heartbeat tick re-polls from\n"
                "  scratch. heartbeat_seen is preserved (issue numbers are\n"
                "  stable across an account rename)."
            )
            return 0

        apply = False
        limit = 100
        positional: list[str] = []
        for tok in rest:
            if tok == "--apply":
                apply = True
            elif tok.startswith("--limit="):
                try:
                    limit = int(tok.split("=", 1)[1])
                except ValueError:
                    print(
                        f"error: invalid --limit value: {tok!r}", file=sys.stderr
                    )
                    return 1
            elif tok == "--limit":
                # value comes on the next argv token
                # placeholder; we re-handle below
                positional.append(tok)
            else:
                positional.append(tok)

        # Handle `--limit N` (separate-token form) by pulling N out of
        # positional if it follows a --limit marker.
        cleaned: list[str] = []
        i = 0
        while i < len(positional):
            if positional[i] == "--limit":
                if i + 1 >= len(positional):
                    print(
                        "error: --limit requires an integer value",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    limit = int(positional[i + 1])
                except ValueError:
                    print(
                        f"error: invalid --limit value: {positional[i + 1]!r}",
                        file=sys.stderr,
                    )
                    return 1
                i += 2
                continue
            cleaned.append(positional[i])
            i += 1
        positional = cleaned

        old: str | None = None
        new: str | None = None
        if len(positional) >= 2:
            old, new = positional[0], positional[1]
        elif len(positional) == 1:
            old = positional[0]
        if (old is None or new is None) and sys.stdin.isatty():
            if old is None:
                old = input("OLD owner: ").strip()
            if new is None:
                new = input("NEW owner: ").strip()
        if not old or not new:
            print(
                "error: both OLD and NEW are required (rerun with --help)",
                file=sys.stderr,
            )
            return 1
        for value, label in ((old, "OLD"), (new, "NEW")):
            err = _validate_owner(value, label)
            if err:
                print(f"error: {err}", file=sys.stderr)
                return 1
        return _rename_github_owner(old, new, apply=apply, limit=limit)

    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
