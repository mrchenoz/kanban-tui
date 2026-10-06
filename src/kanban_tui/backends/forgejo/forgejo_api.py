"""Minimal Forgejo REST client (stdlib only).

Covers just what the board needs: repo labels, issue listing, and changing an
issue's state, labels, title, body and due date. Authenticates with an API token
(scopes ``write:issue`` + ``read:repository``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlencode

PAGE_SIZE = 50


class ForgejoError(Exception):
    """A Forgejo API call failed; the message says why."""


class ForgejoClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15):
        self.api_url = base_url.rstrip("/") + "/api/v1"
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        url = self.api_url + path
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"token {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise ForgejoError(f"{method} {path}: {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ForgejoError(f"{method} {path}: {exc}") from exc
        return json.loads(raw) if raw else None

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            batch = self._request(
                "GET", path, params={**params, "limit": PAGE_SIZE, "page": page}
            )
            results.extend(batch or [])
            if not batch or len(batch) < PAGE_SIZE:
                return results
            page += 1

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        return f"/repos/{quote(owner)}/{quote(repo)}"

    def list_labels(self, owner: str, repo: str) -> list[dict]:
        return self._paged(self._repo_path(owner, repo) + "/labels", {})

    def list_issues(
        self, owner: str, repo: str, state: str, since: str | None = None
    ) -> list[dict]:
        params: dict[str, Any] = {"state": state, "type": "issues"}
        if since:
            params["since"] = since
        return self._paged(self._repo_path(owner, repo) + "/issues", params)

    def get_issue(self, owner: str, repo: str, number: int) -> dict:
        return self._request("GET", f"{self._repo_path(owner, repo)}/issues/{number}")

    def create_issue(self, owner: str, repo: str, fields: dict[str, Any]) -> dict:
        return self._request(
            "POST", self._repo_path(owner, repo) + "/issues", body=fields
        )

    def edit_issue(
        self, owner: str, repo: str, number: int, fields: dict[str, Any]
    ) -> dict:
        return self._request(
            "PATCH", f"{self._repo_path(owner, repo)}/issues/{number}", body=fields
        )

    def add_labels(
        self, owner: str, repo: str, number: int, label_ids: list[int]
    ) -> list[dict]:
        return self._request(
            "POST",
            f"{self._repo_path(owner, repo)}/issues/{number}/labels",
            body={"labels": label_ids},
        )

    def remove_label(self, owner: str, repo: str, number: int, label_id: int) -> None:
        self._request(
            "DELETE",
            f"{self._repo_path(owner, repo)}/issues/{number}/labels/{label_id}",
        )
