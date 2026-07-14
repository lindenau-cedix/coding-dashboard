"""Symmetric encryption for stored secrets (e.g. ``ANTHROPIC_AUTH_TOKEN``).

The dashboard lets operators CRUD *env profiles* that ship two env vars to
the spawned agent subprocess: ``ANTHROPIC_BASE_URL`` and
``ANTHROPIC_AUTH_TOKEN``.  The token is a secret and cannot live in the
SQLite DB in plaintext — particularly on shared hosts.

We derive a 32-byte Fernet key from the dashboard's existing
``Settings.secret_key`` (``CD_SECRET_KEY`` env) using HKDF-SHA256 with a
fixed module-level salt.  Rotating ``CD_SECRET_KEY`` rotates BOTH the
JWT signing key (``security.py``) AND the Fernet master in lockstep — so
a "rotate the secret" run book does not need to remember two key stores.

Failure modes are deliberate:

- ``is_encryption_available()`` returns False iff the literal
  bundled default ``CHANGE-ME-please-generate-a-real-secret`` is still in
  place.  In that state the CRUD routes MUST refuse to persist a token —
  callers get a 503 ("CD_SECRET_KEY must be set") and we cannot
  accidentally write plaintext.
- ``decrypt_secret`` raises ``InvalidToken`` on tampered ciphertext OR a
  rotated key; callers treat this as "no secret available" rather than a
  hard failure (an env profile whose token was encrypted under the old
  key degrades gracefully to "no token injected" + a warning).
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Fixed salt + info bound the Fernet key derivation to this module so an
# attacker who can read ``CD_SECRET_KEY`` cannot reuse it for unrelated
# purposes.  ``v1`` lets us rotate the KDF in place without invalidating
# all currently stored tokens (a future ``v2`` would get its own salt).
_SALT = b"coding-dashboard-env-profile-v1"
_INFO = b"fernet-key"

# The bundled placeholder.  Any other value is treated as a real secret.
_DEFAULT_SECRET_KEY = "CHANGE-ME-please-generate-a-real-secret"


def _fernet() -> Fernet:
    """Build a fresh ``Fernet`` keyed off the dashboard's ``secret_key``.

    Imported lazily so importing this module doesn't crash if
    ``cryptography`` is missing — the CRUD routes only call this when the
    operator actively tries to store a secret.
    """
    from .config import get_settings

    secret = get_settings().secret_key.encode("utf-8")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        info=_INFO,
    ).derive(secret)
    return Fernet(base64.urlsafe_b64encode(derived))


def is_encryption_available() -> bool:
    """False iff ``Settings.secret_key`` is still the bundled placeholder.

    The CRUD router uses this to refuse plaintext-token writes; the UI
    uses it to grey out the Save button + show a banner.
    """
    from .config import get_settings

    return get_settings().secret_key != _DEFAULT_SECRET_KEY


def encrypt_secret(plaintext: str) -> str:
    """Encrypt ``plaintext`` and return a urlsafe-base64 Fernet token.

    Raises ``RuntimeError`` if encryption is not available — callers
    SHOULD check ``is_encryption_available()`` first and surface a 503 to
    the UI rather than letting this exception bubble up.
    """
    if not is_encryption_available():
        raise RuntimeError(
            "CD_SECRET_KEY must be set to a real value before storing secrets"
        )
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a Fernet token produced by ``encrypt_secret``.

    Raises ``cryptography.fernet.InvalidToken`` on tampered ciphertext
    or after a key rotation — the caller decides whether to surface that
    as an error or fall back to "no secret" + a warning.
    """
    if not token:
        return ""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def anonymise_token(token: str) -> str:
    """Return ``"ab…yz"`` for a plaintext token, used by the GET responses.

    Never returns the plaintext — even when the operator typed the token
    into the UI moments earlier.  Returns an empty string when the token
    is empty or shorter than 6 characters (nothing useful to show).
    """
    if not token:
        return ""
    if len(token) < 6:
        return "***"
    return f"{token[:2]}…{token[-2:]}"
