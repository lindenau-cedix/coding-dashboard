"""Small admin CLI.

Usage (from the backend/ directory, venv active):

    python -m app.cli hash-password            # prompts, prints a pbkdf2 hash
    python -m app.cli hash-password 'secret'   # non-interactive
"""
from __future__ import annotations

import getpass
import sys

from .security import hash_password


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0

    cmd = argv[0]
    if cmd == "hash-password":
        if len(argv) > 1:
            pw = argv[1]
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

    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
