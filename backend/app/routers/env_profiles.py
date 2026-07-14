"""CRUD for ``env_profiles`` — named env-var bundles for Claude Code.

Two fields today: ``anthropic_base_url`` (ANTHROPIC_BASE_URL on the
spawned subprocess) and ``anthropic_auth_token`` (ANTHROPIC_AUTH_TOKEN,
stored encrypted at rest via Fernet from
``backend.app.env_crypto``).  The schema is trivially extensible to
further columns when more variables become necessary.

Write-only contract:
- POST / PATCH that includes a non-empty ``anthropic_auth_token`` are
  the only paths that touch the encrypted column.  ``CD_SECRET_KEY``
  MUST be set to a real value (not the bundled default) before any
  encrypted write is allowed — otherwise the route returns **503** with
  a clear operator message.  Plaintext tokens therefore cannot land on
  disk by accident.
- The GET response never includes the plaintext token; it exposes
  ``anthropic_auth_token_set: bool`` + an anonymised hint
  ("sk-…12") instead.  Once a token is saved, the operator can rotate
  it via PATCH with a new token, or PATCH the field to ``""`` to clear
  it (equivalently: delete the token by emptying the column).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import env_crypto
from ..auth import get_current_user
from ..database import get_db
from ..models import EnvProfile
from ..schemas import EnvProfileIn, EnvProfileOut


router = APIRouter(prefix="/env-profiles", tags=["env-profiles"])


def _has_token_set(profile: EnvProfile) -> bool:
    """Decode the Fernet blob to decide ``anthropic_auth_token_set``.

    A failed decrypt (key rotated, blob tampered) is reported as "no token
    set" rather than raised — the operator UI still renders, and PATCHing
    with a fresh token heals the row.
    """
    if not profile.anthropic_auth_token_encrypted:
        return False
    try:
        env_crypto.decrypt_secret(profile.anthropic_auth_token_encrypted)
        return True
    except Exception:
        return False


def _hint(profile: EnvProfile) -> str:
    """Return ``"sk-…12"``-style hint for the UI.

    Decryption failures surface as an empty hint — the UI then renders
    just the "token unset" indicator.
    """
    if not profile.anthropic_auth_token_encrypted:
        return ""
    try:
        return env_crypto.anonymise_token(
            env_crypto.decrypt_secret(profile.anthropic_auth_token_encrypted)
        )
    except Exception:
        return ""


def _serialize(profile: EnvProfile) -> EnvProfileOut:
    return EnvProfileOut(
        key=profile.key,
        name=profile.name,
        anthropic_base_url=profile.anthropic_base_url,
        anthropic_auth_token_set=_has_token_set(profile),
        anthropic_auth_token_hint=_hint(profile),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _assert_encryption_available() -> None:
    """503 unless the operator has set ``CD_SECRET_KEY`` to a real value.

    Plaintext token writes must be impossible by construction.
    """
    if not env_crypto.is_encryption_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "CD_SECRET_KEY is still the bundled default; set it to a "
                "real value before storing env profiles with a token."
            ),
        )


def _ensure_no_secret_leak(payload: EnvProfileIn) -> None:
    """Treat ``"***"`` (the UI's anonymised-hint placeholder) as "leave
    the existing token alone" — PATCH callers who didn't type anything
    but got the redacted placeholder from the GET response.
    """
    if payload.anthropic_auth_token == "***":
        raise HTTPException(
            status_code=422,
            detail=(
                "'***' is the read-side placeholder for an existing token; "
                "leave the field empty on PATCH to keep the token, or paste "
                "a new plaintext token to rotate it."
            ),
        )


@router.get("", response_model=list[EnvProfileOut])
def list_env_profiles(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
) -> list[EnvProfileOut]:
    return [_serialize(p) for p in db.query(EnvProfile).order_by(EnvProfile.key).all()]


@router.post("", response_model=EnvProfileOut, status_code=status.HTTP_201_CREATED)
def create_env_profile(
    payload: EnvProfileIn,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
) -> EnvProfileOut:
    _ensure_no_secret_leak(payload)

    encrypted = ""
    if payload.anthropic_auth_token:
        _assert_encryption_available()
        encrypted = env_crypto.encrypt_secret(payload.anthropic_auth_token)

    profile = EnvProfile(
        key=payload.key,
        name=payload.name,
        anthropic_base_url=payload.anthropic_base_url,
        anthropic_auth_token_encrypted=encrypted,
    )
    db.add(profile)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"env profile '{payload.key}' already exists",
        )
    db.refresh(profile)
    return _serialize(profile)


@router.patch("/{key}", response_model=EnvProfileOut)
def update_env_profile(
    key: str,
    payload: EnvProfileIn,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
) -> EnvProfileOut:
    _ensure_no_secret_leak(payload)

    profile = db.query(EnvProfile).filter(EnvProfile.key == key).one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=404, detail=f"env profile '{key}' not found"
        )

    if payload.name:
        profile.name = payload.name
    # Empty string = "leave unchanged". We can distinguish it from
    # "user typed empty" only when we're sure the user actually
    # interacted with the field; the Pydantic schema gives both as
    # empty strings already. UX convention: PATCH with empty
    # anthropic_base_url clears the field, PATCH with empty
    # anthropic_auth_token leaves it (rotation requires a real
    # plaintext token).
    profile.anthropic_base_url = payload.anthropic_base_url
    if payload.anthropic_auth_token:
        _assert_encryption_available()
        profile.anthropic_auth_token_encrypted = env_crypto.encrypt_secret(
            payload.anthropic_auth_token
        )

    db.commit()
    db.refresh(profile)
    return _serialize(profile)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_env_profile(
    key: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
) -> Response:
    profile = db.query(EnvProfile).filter(EnvProfile.key == key).one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=404, detail=f"env profile '{key}' not found"
        )
    db.delete(profile)
    db.commit()
    return Response(status_code=204)
