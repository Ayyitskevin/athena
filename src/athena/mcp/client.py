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
        include_archived: bool = False,
    ) -> Any:
        params = self._params(
            status=status, project=project, label=label, search=search
        )
        if include_archived:  # only send it when set, so the default stays clean
            params["include_archived"] = True
        return self._result(self._client.get("/issues", params=params))

    def get_issue(self, ref: str) -> Any:
        return self._result(self._client.get(f"/issues/{ref}"))

    def get_issue_state(
        self, issue_id: int, *, as_of_event_id: int | None = None
    ) -> Any:
        """Reconstruct an issue's lifecycle state from the activity log."""
        return self._result(
            self._client.get(
                f"/issues/{issue_id}/state",
                params=self._params(as_of=as_of_event_id),
            )
        )

    def create_issue(
        self,
        *,
        title: str,
        body: str = "",
        status: str | None = None,
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

    def delegate_issue(self, issue_id: int, agent_user_id: int) -> Any:
        return self._result(
            self._client.post(
                f"/issues/{issue_id}/delegate", json={"user_id": agent_user_id}
            )
        )

    def comment_on_issue(self, issue_id: int, body: str) -> Any:
        return self._result(
            self._client.post(f"/issues/{issue_id}/comments", json={"body": body})
        )

    def archive_issue(self, issue_id: int) -> Any:
        return self._result(self._client.post(f"/issues/{issue_id}/archive"))

    def unarchive_issue(self, issue_id: int) -> Any:
        return self._result(self._client.post(f"/issues/{issue_id}/unarchive"))

    def bulk_update_issues(
        self,
        ids: list[int],
        *,
        status: str | None = None,
        priority: str | None = None,
        assignee_id: int | None = None,
        sprint_id: int | None = None,
    ) -> Any:
        # Only the fields actually given are sent (drop-None), so the bulk tool SETS
        # values; clearing an assignee/sprint stays the per-issue tools' job. The
        # endpoint is best-effort, so the result is {updated, failed, results}.
        payload = {
            "ids": ids,
            **self._params(
                status=status,
                priority=priority,
                assignee_id=assignee_id,
                sprint_id=sprint_id,
            ),
        }
        return self._result(self._client.post("/issues/bulk", json=payload))

    # --- hierarchy (parent / children) --------------------------------------

    def set_issue_parent(self, issue_id: int, parent_id: int | None) -> Any:
        # Send parent_id even when None (clear the parent) — _params would drop it.
        return self._result(
            self._client.put(
                f"/issues/{issue_id}/parent", json={"parent_id": parent_id}
            )
        )

    def list_subtasks(self, issue_id: int) -> Any:
        return self._result(self._client.get(f"/issues/{issue_id}/children"))

    # --- dependencies (blocks / blocked_by / relates) -----------------------

    def list_issue_links(self, issue_id: int) -> Any:
        return self._result(self._client.get(f"/issues/{issue_id}/links"))

    def link_issues(self, issue_id: int, target_ref: str, relation: str) -> Any:
        return self._result(
            self._client.post(
                f"/issues/{issue_id}/links",
                json={"target_ref": target_ref, "relation": relation},
            )
        )

    def unlink_issues(self, issue_id: int, relation: str, target_id: int) -> Any:
        return self._result(
            self._client.delete(f"/issues/{issue_id}/links/{relation}/{target_id}")
        )

    # --- sprints ------------------------------------------------------------

    def list_sprints(self, project_id: int, *, state: str | None = None) -> Any:
        return self._result(
            self._client.get(
                f"/projects/{project_id}/sprints", params=self._params(state=state)
            )
        )

    def set_issue_sprint(self, issue_id: int, sprint_id: int | None) -> Any:
        # Send sprint_id even when None (move to the backlog) — _params would drop it.
        return self._result(
            self._client.put(
                f"/issues/{issue_id}/sprint", json={"sprint_id": sprint_id}
            )
        )

    # --- labels -------------------------------------------------------------

    def list_labels(self) -> Any:
        return self._result(self._client.get("/labels"))

    def create_label(self, name: str, *, color: str = "#6b7280") -> Any:
        return self._result(
            self._client.post("/labels", json={"name": name, "color": color})
        )

    def attach_label(self, issue_id: int, label_id: int) -> Any:
        return self._result(
            self._client.post(
                f"/issues/{issue_id}/labels", json={"label_id": label_id}
            )
        )

    def detach_label(self, issue_id: int, label_id: int) -> Any:
        return self._result(
            self._client.delete(f"/issues/{issue_id}/labels/{label_id}")
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

    def list_activity_runs(
        self, *, actor_id: int, gap_seconds: int = 1800, limit: int = 200
    ) -> Any:
        """Reconstruct one actor's recent activity into runs."""
        return self._result(
            self._client.get(
                "/activity/runs",
                params=self._params(
                    actor_id=actor_id, gap_seconds=gap_seconds, limit=limit
                ),
            )
        )

    def get_run_lineage(self, run_id: str) -> Any:
        """Read one tagged run's causal tree."""
        return self._result(self._client.get(f"/activity/runs/{run_id}/lineage"))

    def get_run_fork_contract(
        self, run_id: str, *, fork_from_event_id: int, fork_run_id: str
    ) -> Any:
        """Validate and describe how to fork a run from one parent event."""
        return self._result(
            self._client.get(
                f"/activity/runs/{run_id}/fork",
                params=self._params(
                    from_event_id=fork_from_event_id,
                    fork_run_id=fork_run_id,
                ),
            )
        )
