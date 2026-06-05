"""Thin async GitHub REST client (create / import / delete repos)."""
from __future__ import annotations

import httpx

from .config import get_settings


class GitHubError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitHub API error {status_code}: {message}")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _request(method: str, path: str, *, json: dict | None = None) -> dict:
    settings = get_settings()
    if not settings.github_token:
        raise GitHubError(500, "GitHub token is not configured (CD_GITHUB_TOKEN)")
    url = path if path.startswith("http") else f"{settings.github_api_url}{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, url, headers=_headers(settings.github_token), json=json)
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message", resp.text)
        except Exception:
            message = resp.text
        raise GitHubError(resp.status_code, message)
    if resp.content:
        try:
            return resp.json()
        except Exception:
            return {}
    return {}


async def get_authenticated_user() -> dict:
    return await _request("GET", "/user")


async def create_repo(
    name: str,
    *,
    private: bool = True,
    description: str = "",
    auto_init: bool = True,
    org: str = "",
) -> dict:
    payload = {
        "name": name,
        "private": private,
        "description": description,
        "auto_init": auto_init,
    }
    if org:
        return await _request("POST", f"/orgs/{org}/repos", json=payload)
    return await _request("POST", "/user/repos", json=payload)


async def get_repo(full_name: str) -> dict:
    return await _request("GET", f"/repos/{full_name}")


async def delete_repo(full_name: str) -> None:
    await _request("DELETE", f"/repos/{full_name}")
