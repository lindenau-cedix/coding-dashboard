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


async def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict | list:
    settings = get_settings()
    if not settings.github_token:
        raise GitHubError(500, "GitHub token is not configured (CD_GITHUB_TOKEN)")
    url = path if path.startswith("http") else f"{settings.github_api_url}{path}"
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
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


async def list_user_repos(
    *,
    org: str = "",
    per_page: int = 100,
    max_pages: int = 10,
    include_forks: bool = True,
    affiliation: str = "owner,collaborator,organization_member",
) -> list[dict]:
    """Page through every repo visible to the token (owner + collaborator + org).

    ``per_page`` is the GitHub cap; ``max_pages`` caps total pages so an
    account with thousands of repos doesn't burn time / memory. Returns the
    raw repo objects — the router maps the fields the UI needs.
    """
    base_path = f"/orgs/{org}/repos" if org else "/user/repos"
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params: dict[str, str] = {
            "per_page": str(per_page),
            "page": str(page),
            "sort": "full_name",
            "direction": "asc",
        }
        if not org:
            params["affiliation"] = affiliation
            params["visibility"] = "all"
        chunk = await _request("GET", base_path, params=params)
        if not isinstance(chunk, list) or not chunk:
            break
        for r in chunk:
            if not include_forks and r.get("fork"):
                continue
            out.append(r)
        if len(chunk) < per_page:
            break
    return out


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


async def list_issues(
    full_name: str,
    *,
    state: str = "open",
    labels: list[str] | None = None,
    since: str | None = None,
    per_page: int = 50,
    max_pages: int = 5,
) -> list[dict]:
    """Page through the issues of a repo. Returns RAW issue objects
    including pull requests — callers MUST filter ``r.get("pull_request")``
    if they only want real issues.

    - ``since``: ISO-8601 timestamp; GitHub returns issues updated at or
      after this moment. Used by the heartbeat for incremental polling.
    - ``labels``: comma-joined list; GitHub filters to issues that have
      AT LEAST ONE of these labels.
    - Pagination mirrors ``list_user_repos`` (per_page = GitHub cap,
      max_pages caps total pages so a runaway repo can't burn quota).
    """
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params: dict[str, str] = {
            "state": state,
            "per_page": str(per_page),
            "page": str(page),
            "sort": "updated",
            "direction": "desc",
        }
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since
        chunk = await _request("GET", f"/repos/{full_name}/issues", params=params)
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < per_page:
            break
    return out


async def create_issue_comment(full_name: str, issue_number: int, body: str) -> dict:
    """POST a comment on an issue (or pull request). Returns the raw comment
    object (``{"id": ..., "html_url": ..., ...}``). Caller MUST be
    authenticated with an issue-write-scoped token (the same `CD_GITHUB_TOKEN`
    that drives repo creation is fine)."""
    return await _request(
        "POST",
        f"/repos/{full_name}/issues/{issue_number}/comments",
        json={"body": body},
    )


async def update_issue_comment(
    full_name: str, comment_id: int, body: str
) -> dict:
    """PATCH an existing comment. Used by the "Re-comment" UI affordance so
    the operator can rewrite the dashboard's auto-posted status message
    in-place instead of stacking a second comment."""
    return await _request(
        "PATCH",
        f"/repos/{full_name}/issues/comments/{comment_id}",
        json={"body": body},
    )


async def update_issue_state(full_name: str, issue_number: int, state: str) -> dict:
    """PATCH an issue's ``state``. ``state`` is ``"open"`` or ``"closed"``.
    Used by the heartbeat's "close on merge" behavior and the dashboard's
    manual close/reopen buttons.

    Unlike ``create_issue_comment`` this DOES NOT require a write token —
    any token can close or reopen any issue it can see. Most operator
    tokens already have the scope.
    """
    if state not in ("open", "closed"):
        raise GitHubError(400, f"Invalid issue state: {state!r}")
    return await _request(
        "PATCH",
        f"/repos/{full_name}/issues/{issue_number}",
        json={"state": state},
    )
