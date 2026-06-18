"""Single-user authentication dependencies."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings
from .security import decode_access_token, verify_password

_bearer = HTTPBearer(auto_error=False)


def authenticate_user(username: str, password: str) -> bool:
    settings = get_settings()
    if username != settings.admin_username:
        return False
    if not settings.admin_password_hash:
        return False
    return verify_password(password, settings.admin_password_hash)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    settings = get_settings()
    if not settings.auth_enabled:
        # Auth disabled (e.g. behind a Cloudflare tunnel) -> everyone is the
        # single admin user, no token required.
        return settings.admin_username
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    payload = decode_access_token(creds.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    return str(payload["sub"])


def user_from_token(token: str | None) -> str | None:
    """Used for WebSocket auth where the token arrives as a query param."""
    settings = get_settings()
    if not settings.auth_enabled:
        return settings.admin_username
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None
