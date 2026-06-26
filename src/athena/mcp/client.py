"""A thin HTTP client over Athena's REST API, for the MCP server to call.

The MCP server is a client of Athena like the web UI is: it goes THROUGH the REST
API (with a scoped bearer token), never around it, so every action an agent takes
gets the same validation, scope enforcement, and audit trail as a human's. This
module is the boundary between "MCP tool" and "HTTP call"; it deliberately imports
only httpx (not the MCP SDK), so it can be unit-tested in CI without the optional
`mcp` dependency, by injecting a TestClient as the transport.
"""

from __future__ import annotations

from typing import Any

import httpx


class AthenaError(RuntimeError):
    """An Athena API call returned a non-success status. The message carries the
    method, path, status, and server detail so the agent sees a useful error."""


class AthenaClient:
    """Calls Athena's REST API. Construct with a base URL + bearer token for real
    use, or inject a pre-built client (e.g. a FastAPI TestClient) for tests."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        client: Any | None = None,
        timeout: float = 30.0,
    ):
        if client is not None:
            self._client = client
        else:
            if not base_url:
                raise ValueError("base_url is required without an injected client")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            self._client = httpx.Client(
                base_url=base_url, headers=headers, timeout=timeout
            )

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _params(**kwargs: Any) -> dict:
        """Drop None values so optional filters are simply absent from the query."""
        return {k: v for k, v in kwargs.items() if v is not None}

    def _result(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:  # noqa: BLE001 — body may not be JSON
                detail = response.text
            raise AthenaError(
                f"{response.request.method} {response.request.url.path} "
                f"-> {response.status_code}: {detail}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- search -------------------------------------------------------------

    def search(self, query: str, *, kind: str | None = None, limit: int = 20) -> Any:
        return self._result(
            self._client.get(
                "/search", params=self._params(q=query, kind=kind, limit=limit)
            )
        )

    # --- issues -------------------------------------------------------------

    def list_issues(
        self,
        *,
        status: str | None = None,
        project: str | None = None,
        label: str | None = None,
        search: str | None = None,
    ) -> Any:
        return self._result(
            self._client.get(
                "/issues",
                params=self._params(
                    status=status, project=project, label=label, search=search
                ),
            )
        )

    def get_issue(self, ref: str) -> Any:
        return self._result(self._client.get(f"/issues/{ref}"))

    def create_issue(
        self,
        *,
        title: str,
        body: str = "",
        status: str = "open",
        priority: str = "medium",
        project_id: int | None = None,
    ) -> Any:
        payload = self._params(
            title=title,
            body=body,
            status=status,
            priority=priority,
            project_id=project_id,
        )
        return self._result(self._client.post("/issues", json=payload))

    def update_issue(
        self,
        issue_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> Any:
        payload = self._params(
            title=title, body=body, status=status, priority=priority
        )
        return self._result(self._client.patch(f"/issues/{issue_id}", json=payload))

    def assign_issue(self, issue_id: int, assignee_id: int | None) -> Any:
        return self._result(
            self._client.put(
                f"/issues/{issue_id}/assignee", json={"assignee_id": assignee_id}
            )
        )

    def comment_on_issue(self, issue_id: int, body: str) -> Any:
        return self._result(
            self._client.post(f"/issues/{issue_id}/comments", json={"body": body})
        )

    # --- projects & users ---------------------------------------------------

    def list_projects(self) -> Any:
        return self._result(self._client.get("/projects"))

    def list_users(self) -> Any:
        return self._result(self._client.get("/users"))

    # --- pages (Mentor) -----------------------------------------------------

    def list_spaces(self) -> Any:
        return self._result(self._client.get("/spaces"))

    def list_pages(self, space_id: int) -> Any:
        return self._result(self._client.get(f"/spaces/{space_id}/pages"))

    def get_page(self, page_id: int) -> Any:
        return self._result(self._client.get(f"/pages/{page_id}"))

    def create_page(
        self,
        *,
        space_id: int,
        title: str,
        body: str = "",
        parent_id: int | None = None,
    ) -> Any:
        payload = self._params(title=title, body=body, parent_id=parent_id)
        return self._result(
            self._client.post(f"/spaces/{space_id}/pages", json=payload)
        )

    def update_page(
        self,
        page_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
    ) -> Any:
        payload = self._params(title=title, body=body)
        return self._result(self._client.patch(f"/pages/{page_id}", json=payload))

    # --- events -------------------------------------------------------------

    def recent_events(
        self, *, after: int | None = None, kind: str | None = None, limit: int = 50
    ) -> Any:
        return self._result(
            self._client.get(
                "/events", params=self._params(after=after, kind=kind, limit=limit)
            )
        )
