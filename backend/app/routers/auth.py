"""Authentication routes (single user)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import authenticate_user, get_current_user
from ..config import get_settings
from ..schemas import LoginRequest, TokenResponse
from ..security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    settings = get_settings()
    if not settings.admin_password_hash:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kein Admin-Passwort konfiguriert (CD_ADMIN_PASSWORD_HASH).",
        )
    if not authenticate_user(body.username, body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falscher Benutzername oder Passwort.",
        )
    token = create_access_token(body.username)
    return TokenResponse(access_token=token, username=body.username)


@router.get("/me")
def me(user: str = Depends(get_current_user)) -> dict:
    return {"username": user}
