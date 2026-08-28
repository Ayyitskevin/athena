"""A thin HTTP client over Athena's REST API, for the MCP server to call.

The MCP server is a client of Athena like the web UI is: it goes THROUGH the REST
API (with a scoped bearer token), never around it, so every action an agent takes
gets the same validation, scope enforcement, and audit trail as a human's. This
module is the boundary between "MCP tool" and "HTTP call"; it deliberately imports
only httpx (not the MCP SDK), so it can be unit-tested in CI without the optional
`mcp` dependency, by injecting a TestClient as the transport.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx


class AthenaError(RuntimeError):
    """An Athena API call returned a non-success status. The message carries the
    method, path, status, and server detail so the agent sees a useful error."""

    def __init__(
        self,
        message: str | None = None,
        *,
        method: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
        detail: Any = None,
        code: str | None = None,
        retry_after: str | None = None,
        current_etag: str | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.retry_after = retry_after
        self.current_etag = current_etag
        if message is None and None not in (method, path, status_code):
            message = f"{method} {path} -> {status_code}: {detail}"
        if message is None:
            super().__init__()
        else:
            super().__init__(message)

    def __reduce__(self) -> tuple[Any, tuple[Any, ...], dict[str, Any]]:
        """Rebuild through the positional-compatible path, then restore metadata."""
        return type(self), self.args, self.__dict__.copy()

    def as_dict(self) -> dict[str, Any]:
        """Return stable, serializable error fields for programmatic callers."""
        return {
            "message": str(self),
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "detail": self.detail,
            "code": self.code,
            "retry_after": self.retry_after,
            "current_etag": self.current_etag,
        }


# The run-identity headers RunContextMiddleware reads on every request. Managed as
# a set so set_run can replace the whole identity atomically (stale parent/fork
# headers from a previous run must never leak into the next one).
_RUN_HEADERS = ("X-Athena-Run", "X-Athena-Parent-Run", "X-Athena-Fork-From-Event")


class AthenaClient:
    """Calls Athena's REST API. Construct with a base URL + bearer token for real
    use, or inject a pre-built client (e.g. a FastAPI TestClient) for tests.

    Run identity is CLIENT state: pass ``run_id`` (and optionally
    ``parent_run_id``) at construction, or call :meth:`set_run` later, and every
    subsequent request carries the ``X-Athena-Run`` family of headers — so each
    write this client performs is attributed to that run in the activity trail,
    replayable and lineage-linked, exactly like any other tagged actor."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        client: Any | None = None,
        timeout: float = 30.0,
        run_id: str | None = None,
        parent_run_id: str | None = None,
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
        if run_id is not None or parent_run_id is not None:
            self.set_run(run_id, parent_run_id=parent_run_id)

    def set_run(
        self,
        run_id: str | None,
        *,
        parent_run_id: str | None = None,
        fork_from_event_id: int | None = None,
    ) -> dict:
        """Switch this client's run identity. Replaces ALL run headers at once —
        omitted parts are cleared, so a new run never inherits the previous run's
        parent or fork point. ``run_id=None`` clears the identity entirely (writes
        go back to untagged). Returns the now-active identity.

        This is what makes a fork contract actionable: GET /activity/runs/{id}/fork
        returns exactly these header values — feed them here and keep working."""
        for header in _RUN_HEADERS:
            self._client.headers.pop(header, None)
        if run_id is not None:
            self._client.headers["X-Athena-Run"] = run_id
            if parent_run_id is not None:
                self._client.headers["X-Athena-Parent-Run"] = parent_run_id
            if fork_from_event_id is not None:
                self._client.headers["X-Athena-Fork-From-Event"] = str(
                    fork_from_event_id
                )
        return self.current_run()

    def current_run(self) -> dict:
        """The run identity this client is currently stamping on requests."""
        fork = self._client.headers.get("X-Athena-Fork-From-Event")
        return {
            "run_id": self._client.headers.get("X-Athena-Run"),
            "parent_run_id": self._client.headers.get("X-Athena-Parent-Run"),
            "fork_from_event_id": int(fork) if fork is not None else None,
        }

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _params(**kwargs: Any) -> dict:
        """Drop None values so optional filters are simply absent from the query."""
        return {k: v for k, v in kwargs.items() if v is not None}

    def _result(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 — body may not be JSON
                payload = None
            if isinstance(payload, dict):
                detail = payload.get("detail", response.text)
                raw_code = payload.get("code")
                code = raw_code if isinstance(raw_code, str) else None
            else:
                detail = response.text
            raise AthenaError(
                method=response.request.method,
                path=response.request.url.path,
                status_code=response.status_code,
                detail=detail,
                code=code if isinstance(payload, dict) else None,
                retry_after=response.headers.get("Retry-After"),
                current_etag=response.headers.get("ETag"),
            )
        if response.status_code == 204 or not response.content:
            return None
        result = response.json()
        etag = response.headers.get("ETag")
        if isinstance(result, dict) and etag is not None:
            result["_etag"] = etag
        return result

    def _mutate(
        self,
        request: Any,
        path: str,
        *,
        idempotency_key: str | None = None,
        if_match: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Send a mutation with optional retry and optimistic-lock headers."""
        if idempotency_key is not None or if_match is not None:
            existing_headers = kwargs.get("headers")
            if existing_headers is None:
                merged_headers: dict[str, str] | httpx.Headers = {}
            else:
                merged_headers = httpx.Headers(existing_headers)
            if idempotency_key is not None:
                merged_headers["Idempotency-Key"] = idempotency_key
            if if_match is not None:
                merged_headers["If-Match"] = if_match
            kwargs["headers"] = merged_headers
        return self._result(request(path, **kwargs))

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
        sprint: int | None = None,
        label: str | None = None,
        search: str | None = None,
        include_archived: bool = False,
    ) -> Any:
        params = self._params(
            status=status,
            project=project,
            sprint=sprint,
            label=label,
            search=search,
        )
        if include_archived:  # only send it when set, so the default stays clean
            params["include_archived"] = True
        return self._result(self._client.get("/issues", params=params))

    def search_work(self, q: str, *, limit: int = 50, offset: int = 0) -> Any:
        """Run a work query. Same endpoint the browser and REST callers use."""
        return self._result(
            self._client.get(
                "/issues", params={"q": q, "limit": limit, "offset": offset}
            )
        )

    def count_work(self, q: str) -> Any:
        return self._result(self._client.get("/issues/query/count", params={"q": q}))

    def query_help(self) -> Any:
        return self._result(self._client.get("/issues/query/help"))

    def resolve_embeds(self, text: str) -> Any:
        return self._result(self._client.post("/embeds/resolve", json={"text": text}))

    def page_embeds(self, page_id: int) -> Any:
        """A page's embeds, resolved as this token's actor.

        Two REST calls — read the page, resolve its body — because the import
        contract keeps Mentor pages and Aegis issue queries in peer modules that
        may not import each other. Composed here so an agent still makes one tool
        call, and so the visibility of both halves is the caller's own.
        """
        page = self._result(self._client.get(f"/pages/{page_id}"))
        return self.resolve_embeds(page.get("body") or "")

    def embed_help(self) -> Any:
        return self._result(self._client.get("/embeds/help"))

    def list_my_delegated_work(
        self,
        *,
        include_closed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        """Read contributor assignments for the token's own actor."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if include_closed:
            params["include_closed"] = True
        return self._result(self._client.get("/delegations/me", params=params))

    def get_fleet_active_work(
        self,
        *,
        agent_id: int | None = None,
        limit: int | None = None,
        attention_state: str | None = None,
    ) -> Any:
        """Read the admin-only active claimed-work projection."""
        return self._result(
            self._client.get(
                "/fleet/active-work",
                params=self._params(
                    agent_id=agent_id, limit=limit, attention_state=attention_state
                ),
            )
        )

    def get_issue(self, ref: str) -> Any:
        return self._result(self._client.get(f"/issues/{ref}"))

    def get_issue_work_context(self, ref: str) -> Any:
        """Get the bounded work-context packet for one visible issue."""
        return self._result(self._client.get(f"/issues/{ref}/work-context"))

    def get_issue_history(self, issue_id: int, *, limit: int | None = None) -> Any:
        """Read one issue's bounded operator run narrative."""
        return self._result(
            self._client.get(
                f"/issues/{issue_id}/history",
                params=self._params(limit=limit),
            )
        )

    def get_attention_ranking(
        self,
        *,
        signals: Sequence[str] | None = None,
        window_hours: int | None = None,
        limit: int | None = None,
    ) -> Any:
        """Read the actor-filtered ranked attention queue."""
        return self._result(
            self._client.get(
                "/attention/ranking",
                params=self._params(
                    signals=",".join(signals) if signals else None,
                    window_hours=window_hours,
                    limit=limit,
                ),
            )
        )

    def get_fleet_metrics(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        project_id: int | None = None,
        actor_id: int | None = None,
        actor_limit: int | None = None,
    ) -> Any:
        """Read the exact visibility-safe REST metrics contract."""
        return self._result(
            self._client.get(
                "/fleet/metrics",
                params=self._params(
                    start=start,
                    end=end,
                    project_id=project_id,
                    actor_id=actor_id,
                    actor_limit=actor_limit,
                ),
            )
        )

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
        idempotency_key: str | None = None,
    ) -> Any:
        payload = self._params(
            title=title,
            body=body,
            status=status,
            priority=priority,
            project_id=project_id,
        )
        return self._mutate(
            self._client.post,
            "/issues",
            json=payload,
            idempotency_key=idempotency_key,
        )

    def update_issue(
        self,
        issue_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        if_match: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        payload = self._params(title=title, body=body, status=status, priority=priority)
        return self._mutate(
            self._client.patch,
            f"/issues/{issue_id}",
            json=payload,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def set_issue_placement(
        self,
        issue_id: int,
        *,
        project_id: int | None,
        sprint_id: int | None,
        if_match: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Set an issue's project and sprint as one relationship transition.

        Both keys are deliberately sent even when null: ``project_id=None`` moves
        the issue out of a project, while ``sprint_id=None`` puts it in no sprint.
        The paired PATCH lets the API validate and commit the final placement once.
        """
        return self._mutate(
            self._client.patch,
            f"/issues/{issue_id}",
            json={"project_id": project_id, "sprint_id": sprint_id},
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def assign_issue(
        self,
        issue_id: int,
        assignee_id: int | None,
        *,
        if_match: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        return self._mutate(
            self._client.put,
            f"/issues/{issue_id}/assignee",
            json={"assignee_id": assignee_id},
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def delegate_issue(
        self, issue_id: int, agent_user_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/delegate",
            json={"user_id": agent_user_id},
            idempotency_key=idempotency_key,
        )

    def get_issue_lease(self, issue_id: int) -> Any:
        """Who holds the exclusive claim on this issue right now (or null if unclaimed).
        Check it before claiming to see whether another agent is already working it."""
        return self._result(self._client.get(f"/issues/{issue_id}/lease"))

    def claim_issue(
        self,
        issue_id: int,
        *,
        if_match: str,
        generation: str | None = None,
        lease_seconds: int | None = None,
        paths: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Claim against the exact strong root issue ETag the caller reviewed.

        ``paths`` optionally fences repo-relative files against other active leases.
        """
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/claim",
            json=self._params(
                lease_seconds=lease_seconds,
                generation=generation,
                paths=paths,
            ),
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def yield_claim(
        self,
        issue_id: int,
        *,
        generation: str,
        reason: str,
        attempted_work: str,
        evidence: list[str],
        blocking_question: str,
        resume_instructions: str,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Release this caller's live claim with an audited non-completion reason."""
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/yield",
            json=self._params(
                generation=generation,
                reason=reason,
                attempted_work=attempted_work,
                evidence=evidence,
                blocking_question=blocking_question,
                resume_instructions=resume_instructions,
                note=note,
            ),
            idempotency_key=idempotency_key,
        )

    def resume_claim_handoff(
        self,
        issue_id: int,
        handoff_token: str,
        *,
        generation: str,
        resume_note: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Acknowledge receipt of a handoff as the exact current leaseholder."""
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/claim-handoffs/{handoff_token}/resume",
            json=self._params(
                generation=generation,
                resume_note=resume_note,
            ),
            idempotency_key=idempotency_key,
        )

    def complete_claim(
        self,
        issue_id: int,
        *,
        generation: str,
        idempotency_key: str | None = None,
    ) -> Any:
        """Release the lease you hold by completing the claimed work (complete)."""
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/complete",
            json={"generation": generation},
            idempotency_key=idempotency_key,
        )

    def decline_delegation(
        self,
        issue_id: int,
        *,
        generation: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Decline a delegation handed to you (decline): remove yourself from the
        contributor set so the work is visibly refused and can be re-routed."""
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/decline",
            json=self._params(generation=generation),
            idempotency_key=idempotency_key,
        )

    def comment_on_issue(
        self, issue_id: int, body: str, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/comments",
            json={"body": body},
            idempotency_key=idempotency_key,
        )

    def list_issue_comments(self, issue_id: int) -> Any:
        """Read one issue's comment thread (oldest first)."""
        return self._result(self._client.get(f"/issues/{issue_id}/comments"))

    def archive_issue(
        self, issue_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/archive",
            idempotency_key=idempotency_key,
        )

    def unarchive_issue(
        self, issue_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/unarchive",
            idempotency_key=idempotency_key,
        )

    def bulk_update_issues(
        self,
        ids: list[int],
        *,
        status: str | None = None,
        priority: str | None = None,
        assignee_id: int | None = None,
        sprint_id: int | None = None,
        idempotency_key: str | None = None,
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
        return self._mutate(
            self._client.post,
            "/issues/bulk",
            json=payload,
            idempotency_key=idempotency_key,
        )

    # --- hierarchy (parent / children) --------------------------------------

    def set_issue_parent(
        self,
        issue_id: int,
        parent_id: int | None,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        # Send parent_id even when None (clear the parent) — _params would drop it.
        return self._mutate(
            self._client.put,
            f"/issues/{issue_id}/parent",
            json={"parent_id": parent_id},
            idempotency_key=idempotency_key,
        )

    def list_subtasks(self, issue_id: int) -> Any:
        return self._result(self._client.get(f"/issues/{issue_id}/children"))

    # --- dependencies (blocks / blocked_by / relates) -----------------------

    def list_issue_links(self, issue_id: int) -> Any:
        return self._result(self._client.get(f"/issues/{issue_id}/links"))

    def link_issues(
        self,
        issue_id: int,
        target_ref: str,
        relation: str,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/links",
            json={"target_ref": target_ref, "relation": relation},
            idempotency_key=idempotency_key,
        )

    def unlink_issues(
        self,
        issue_id: int,
        relation: str,
        target_id: int,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        return self._mutate(
            self._client.delete,
            f"/issues/{issue_id}/links/{relation}/{target_id}",
            idempotency_key=idempotency_key,
        )

    # --- sprints ------------------------------------------------------------

    def list_sprints(self, project_id: int, *, state: str | None = None) -> Any:
        return self._result(
            self._client.get(
                f"/projects/{project_id}/sprints", params=self._params(state=state)
            )
        )

    def get_sprint(self, sprint_id: int) -> Any:
        return self._result(self._client.get(f"/sprints/{sprint_id}"))

    def create_sprint(
        self,
        project_id: int,
        *,
        name: str,
        goal: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/projects/{project_id}/sprints",
            json=self._params(
                name=name,
                goal=goal,
                start_date=start_date,
                end_date=end_date,
            ),
            idempotency_key=idempotency_key,
        )

    def update_sprint(
        self,
        sprint_id: int,
        *,
        name: str | None = None,
        goal: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        clear_start_date: bool = False,
        clear_end_date: bool = False,
        idempotency_key: str | None = None,
    ) -> Any:
        if start_date is not None and clear_start_date:
            raise ValueError("start_date and clear_start_date are mutually exclusive")
        if end_date is not None and clear_end_date:
            raise ValueError("end_date and clear_end_date are mutually exclusive")
        fields = self._params(
            name=name,
            goal=goal,
            start_date=start_date,
            end_date=end_date,
        )
        if clear_start_date:
            fields["start_date"] = None
        if clear_end_date:
            fields["end_date"] = None
        return self._mutate(
            self._client.patch,
            f"/sprints/{sprint_id}",
            json=fields,
            idempotency_key=idempotency_key,
        )

    def start_sprint(
        self, sprint_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/sprints/{sprint_id}/start",
            idempotency_key=idempotency_key,
        )

    def complete_sprint(
        self, sprint_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/sprints/{sprint_id}/complete",
            idempotency_key=idempotency_key,
        )

    def delete_sprint(
        self, sprint_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.delete,
            f"/sprints/{sprint_id}",
            idempotency_key=idempotency_key,
        )

    def set_issue_sprint(
        self,
        issue_id: int,
        sprint_id: int | None,
        *,
        if_match: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        # Send sprint_id even when None (move to the backlog) — _params would drop it.
        return self._mutate(
            self._client.put,
            f"/issues/{issue_id}/sprint",
            json={"sprint_id": sprint_id},
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    # --- labels -------------------------------------------------------------

    def list_labels(self) -> Any:
        return self._result(self._client.get("/labels"))

    def create_label(
        self,
        name: str,
        *,
        color: str = "#6b7280",
        idempotency_key: str | None = None,
    ) -> Any:
        return self._mutate(
            self._client.post,
            "/labels",
            json={"name": name, "color": color},
            idempotency_key=idempotency_key,
        )

    def attach_label(
        self, issue_id: int, label_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/labels",
            json={"label_id": label_id},
            idempotency_key=idempotency_key,
        )

    def detach_label(
        self, issue_id: int, label_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.delete,
            f"/issues/{issue_id}/labels/{label_id}",
            idempotency_key=idempotency_key,
        )

    # --- projects & users ---------------------------------------------------

    def list_projects(self) -> Any:
        return self._result(self._client.get("/projects"))

    def list_users(self) -> Any:
        return self._result(self._client.get("/users"))

    def healthz(self) -> Any:
        """The unauthenticated liveness read — proves the server is there at
        all, so an auth failure afterwards is attributable to the token."""
        return self._result(self._client.get("/healthz"))

    def whoami(self) -> Any:
        """Read the authenticated identity: id, email, role, agent flag, and the
        acting token's effective scopes (null when the auth is not scope-limited)."""
        return self._result(self._client.get("/users/me"))

    def onboard_agent(
        self,
        *,
        name: str,
        scopes: list[str],
        email: str | None = None,
        token_name: str | None = None,
    ) -> Any:
        """Admin: provision an agent (user + first scoped token) in one audited
        move. The response carries the one-time raw token and an MCP config block.
        Email is optional — omitted becomes ``{slug}@agents.local``.
        Deliberately NO idempotency_key: the server refuses durable replay for
        endpoints that return a one-time secret (the raw token must never sit in
        the replay store)."""
        return self._mutate(
            self._client.post,
            "/users/onboard_agent",
            json=self._params(
                email=email, name=name, scopes=scopes, token_name=token_name
            ),
        )

    # --- pages (Mentor) -----------------------------------------------------

    def list_spaces(self) -> Any:
        return self._result(self._client.get("/spaces"))

    def list_pages(self, space_id: int, *, include_archived: bool = False) -> Any:
        params: dict[str, Any] = {}
        if include_archived:  # only send it when set, so the default stays clean
            params["include_archived"] = True
        return self._result(
            self._client.get(f"/spaces/{space_id}/pages", params=params)
        )

    def get_page(self, page_id: int) -> Any:
        return self._result(self._client.get(f"/pages/{page_id}"))

    def find_pages_by_title(self, title: str, *, space_id: int | None = None) -> Any:
        """Look up pages by their TITLE instead of a numeric id — the address an agent
        can recall. Returns every exact (case-insensitive) match (titles aren't unique),
        so the caller can disambiguate; pass space_id to narrow to one space."""
        return self._result(
            self._client.get(
                "/pages/by-title",
                params=self._params(title=title, space_id=space_id),
            )
        )

    def page_backlinks(self, page_id: int) -> Any:
        """What references this page — the incoming edges of the knowledge graph."""
        return self._result(self._client.get(f"/pages/{page_id}/backlinks"))

    def page_outgoing_links(self, page_id: int) -> Any:
        """What this page references — the outgoing edges of the knowledge graph."""
        return self._result(self._client.get(f"/pages/{page_id}/outgoing-links"))

    def link_graph(
        self,
        kind: str,
        node_id: int,
        depth: int | None = None,
        max_nodes: int | None = None,
    ) -> Any:
        """The bounded neighbourhood around one issue or page, as positioned data."""
        params: dict[str, Any] = {}
        if depth is not None:
            params["depth"] = depth
        if max_nodes is not None:
            params["max_nodes"] = max_nodes
        base = "issues" if kind == "issue" else "pages"
        return self._result(
            self._client.get(f"/{base}/{node_id}/graph", params=params or None)
        )

    def related_items(self, kind: str, node_id: int, limit: int | None = None) -> Any:
        """What cites what this cites, but is not linked to it yet — co-citation."""
        params = {"limit": limit} if limit is not None else None
        base = "issues" if kind == "issue" else "pages"
        return self._result(
            self._client.get(f"/{base}/{node_id}/related", params=params)
        )

    def project_timeline(
        self,
        project_id: int,
        *,
        max_per_lane: int | None = None,
        max_items: int | None = None,
    ) -> Any:
        """One project's roadmap — sprint lanes, placed issues, dependency edges."""
        return self._result(
            self._client.get(
                f"/projects/{project_id}/timeline",
                params=self._params(max_per_lane=max_per_lane, max_items=max_items),
            )
        )

    def unlinked_mentions(
        self, kind: str, node_id: int, limit: int | None = None
    ) -> Any:
        """Documents naming this issue/page without linking to it."""
        params = {"limit": limit} if limit is not None else None
        base = "issues" if kind == "issue" else "pages"
        return self._result(
            self._client.get(f"/{base}/{node_id}/unlinked-mentions", params=params)
        )

    def link_mention(
        self, source_kind: str, source_id: int, target_kind: str, target_id: int
    ) -> Any:
        """Rewrite the SOURCE's body so its first unlinked mention becomes a link."""
        base = "issues" if source_kind == "issue" else "pages"
        return self._result(
            self._client.post(
                f"/{base}/{source_id}/link-mention",
                json={"target_kind": target_kind, "target_id": target_id},
            )
        )

    def list_page_versions(self, page_id: int) -> Any:
        """The page's superseded revisions, newest first (the live page is not one)."""
        return self._result(self._client.get(f"/pages/{page_id}/versions"))

    def get_page_version(self, page_id: int, version: int) -> Any:
        """One historical revision's title + body."""
        return self._result(self._client.get(f"/pages/{page_id}/versions/{version}"))

    def restore_page_version(
        self, page_id: int, version: int, *, idempotency_key: str | None = None
    ) -> Any:
        """Restore the page's content to a prior revision (a non-destructive edit —
        the current content is snapshotted into history first)."""
        return self._mutate(
            self._client.post,
            f"/pages/{page_id}/versions/{version}/restore",
            idempotency_key=idempotency_key,
        )

    def archive_page(self, page_id: int, *, idempotency_key: str | None = None) -> Any:
        return self._mutate(
            self._client.post,
            f"/pages/{page_id}/archive",
            idempotency_key=idempotency_key,
        )

    def unarchive_page(
        self, page_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/pages/{page_id}/unarchive",
            idempotency_key=idempotency_key,
        )

    def create_page(
        self,
        *,
        space_id: int,
        title: str,
        body: str = "",
        parent_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        payload = self._params(title=title, body=body, parent_id=parent_id)
        return self._mutate(
            self._client.post,
            f"/spaces/{space_id}/pages",
            json=payload,
            idempotency_key=idempotency_key,
        )

    def update_page(
        self,
        page_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        if_match: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        payload = self._params(title=title, body=body)
        return self._mutate(
            self._client.patch,
            f"/pages/{page_id}",
            json=payload,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    def label_page(
        self, page_id: int, label_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.post,
            f"/pages/{page_id}/labels",
            json={"label_id": label_id},
            idempotency_key=idempotency_key,
        )

    def unlabel_page(
        self, page_id: int, label_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        return self._mutate(
            self._client.delete,
            f"/pages/{page_id}/labels/{label_id}",
            idempotency_key=idempotency_key,
        )

    # --- events -------------------------------------------------------------

    def recent_events(
        self, *, after: int | None = None, kind: str | None = None, limit: int = 50
    ) -> Any:
        return self._result(
            self._client.get(
                "/events", params=self._params(after=after, kind=kind, limit=limit)
            )
        )

    def list_notifications(self, *, unread: bool = False, limit: int = 50) -> Any:
        """Read the authenticated actor's own notification inbox."""
        params: dict[str, Any] = {"limit": limit}
        if unread:
            params["unread"] = True
        return self._result(self._client.get("/notifications", params=params))

    def list_priority_notifications(
        self,
        *,
        unread: bool = False,
        min_priority: str | None = None,
        include_muted: bool = False,
        digest: bool = False,
        limit: int = 50,
    ) -> Any:
        """Read the actor's priority/mute/digest inbox projection."""
        return self._result(
            self._client.get(
                "/notifications/priority",
                params=self._params(
                    unread=unread or None,
                    min_priority=min_priority,
                    include_muted=include_muted or None,
                    digest=digest or None,
                    limit=limit,
                ),
            )
        )

    def notification_priority_summary(self, *, unread: bool = False) -> Any:
        """Count the actor's notifications by priority and mute state."""
        return self._result(
            self._client.get(
                "/notifications/priority/summary",
                params=self._params(unread=unread or None),
            )
        )

    def get_watch_preference(self, target_kind: str, target_id: int) -> Any:
        """Read the actor's preference for one active watch."""
        return self._result(
            self._client.get(f"/watches/{target_kind}/{target_id}/preference")
        )

    def set_watch_preference(
        self,
        target_kind: str,
        target_id: int,
        *,
        priority: str | None = None,
        mute_until: str | None = None,
        digest_window_minutes: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Replace the actor's preference for one active watch."""
        return self._mutate(
            self._client.put,
            f"/watches/{target_kind}/{target_id}/preference",
            json={
                "priority": priority,
                "mute_until": mute_until,
                "digest_window_minutes": digest_window_minutes,
            },
            idempotency_key=idempotency_key,
        )

    def clear_watch_preference(
        self,
        target_kind: str,
        target_id: int,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        """Delete the actor's preference and restore target/default priority."""
        return self._mutate(
            self._client.delete,
            f"/watches/{target_kind}/{target_id}/preference",
            idempotency_key=idempotency_key,
        )

    def mark_all_notifications_read(self) -> Any:
        """Clear the authenticated actor's inbox; returns how many were cleared."""
        return self._mutate(self._client.post, "/notifications/read_all")

    def search_workspace(self, query: str, *, limit_per_kind: int | None = None) -> Any:
        """One search across issues, pages, and comments, grouped by kind."""
        return self._result(
            self._client.get(
                "/search/workspace",
                params=self._params(q=query, limit_per_kind=limit_per_kind),
            )
        )

    def watch(self, target_kind: str, target_id: int) -> Any:
        """Subscribe the authenticated actor's inbox to a target (idempotent)."""
        return self._mutate(
            self._client.post,
            "/watches",
            json={"target_kind": target_kind, "target_id": target_id},
        )

    def unwatch(self, target_kind: str, target_id: int) -> Any:
        """Unsubscribe the actor's inbox. 404 if they were not watching it."""
        return self._mutate(self._client.delete, f"/watches/{target_kind}/{target_id}")

    def list_run_events(
        self, run_id: str, *, before_id: int | None = None, limit: int = 100
    ) -> Any:
        """Replay one run: exactly the activity events tagged with this run id
        (newest first; page older history with before_id)."""
        return self._result(
            self._client.get(
                "/activity",
                params=self._params(run_id=run_id, before_id=before_id, limit=limit),
            )
        )

    def heartbeat_agent_run(self, run_id: str) -> Any:
        """Refresh this authenticated agent's server-timed run check-in.

        A heartbeat is intentionally not a durable-idempotency mutation: every PUT
        must reach the server so repeated calls advance the liveness observation.
        """
        return self._result(
            self._client.put(
                "/agent-runs/heartbeat",
                json={"run_id": run_id},
            )
        )

    def get_agent_run_health(self, *, agent_id: int | None = None) -> Any:
        """Read the bounded fleet run-health rollup (admin only)."""
        return self._result(
            self._client.get(
                "/activity/agent-runs",
                params=self._params(agent_id=agent_id),
            )
        )

    # --- automation rules (admin) -------------------------------------------

    def list_automation_rules(self) -> Any:
        """List every automation rule, including schedule state and health."""
        return self._result(self._client.get("/automation/rules"))

    def get_automation_rule(self, rule_id: int) -> Any:
        """Get one automation rule, including schedule state and health."""
        return self._result(self._client.get(f"/automation/rules/{rule_id}"))

    def create_automation_rule(
        self,
        *,
        name: str,
        trigger_verb: str,
        action_type: str,
        conditions: dict | None = None,
        action_params: dict | None = None,
        target_kind: str = "issue",
        trigger_type: str = "event",
        schedule_at: str | None = None,
        schedule_every_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Create an event- or schedule-triggered automation rule."""
        payload: dict[str, Any] = {
            "name": name,
            "trigger_verb": trigger_verb,
            "action_type": action_type,
            "conditions": conditions if conditions is not None else {},
            "action_params": action_params if action_params is not None else {},
            "target_kind": target_kind,
            "trigger_type": trigger_type,
        }
        if schedule_at is not None:
            payload["schedule_at"] = schedule_at
        if schedule_every_seconds is not None:
            payload["schedule_every_seconds"] = schedule_every_seconds
        return self._mutate(
            self._client.post,
            "/automation/rules",
            json=payload,
            idempotency_key=idempotency_key,
        )

    def set_automation_rule_enabled(
        self,
        rule_id: int,
        enabled: bool,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        """Arm or disarm an automation rule without deleting it."""
        return self._mutate(
            self._client.patch,
            f"/automation/rules/{rule_id}",
            json={"enabled": enabled},
            idempotency_key=idempotency_key,
        )

    def delete_automation_rule(
        self, rule_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        """Delete an automation rule."""
        return self._mutate(
            self._client.delete,
            f"/automation/rules/{rule_id}",
            idempotency_key=idempotency_key,
        )

    def list_automation_failures(self) -> Any:
        """List only automation rules with recorded action failures (admin only)."""
        return self._result(
            self._client.get("/automation/rules", params={"failing_only": True})
        )

    def set_user_paused(self, user_id: int, paused: bool) -> Any:
        """Admin pause lever: freeze (or resume) every authenticated action for a
        user without revoking anything (admin only)."""
        return self._mutate(
            self._client.put, f"/users/{user_id}/paused", json={"paused": paused}
        )

    def revoke_agent_tokens(self, user_id: int) -> Any:
        """Admin kill switch: revoke every live token another user holds (admin only)."""
        return self._mutate(self._client.delete, f"/users/{user_id}/tokens")

    def offboard_user(self, user_id: int) -> Any:
        """Admin offboard: demote to viewer + revoke all sessions and tokens (admin only)."""
        return self._mutate(self._client.post, f"/users/{user_id}/offboard")

    def dispatch_to_icarus(
        self,
        issue_id: int,
        *,
        repo: str,
        base_commit: str,
        capability: str,
        idempotency_key: str | None = None,
    ) -> Any:
        """Ask the configured executor to do work on an issue."""
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/dispatch",
            json={
                "repo": repo,
                "base_commit": base_commit,
                "capability": capability,
            },
            idempotency_key=idempotency_key,
        )

    def list_dispatches(
        self,
        *,
        work_item_id: int | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> Any:
        """What Athena has handed to the executor, newest first."""
        return self._result(
            self._client.get(
                "/dispatches",
                params=self._params(
                    work_item_id=work_item_id, state=state, limit=limit
                ),
            )
        )

    def record_run_learning(
        self,
        issue_id: int,
        *,
        summary: str,
        run_id: str | None = None,
        space_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Append what a run learned to an issue's runbook page."""
        return self._mutate(
            self._client.post,
            f"/issues/{issue_id}/learnings",
            json=self._params(summary=summary, run_id=run_id, space_id=space_id),
            idempotency_key=idempotency_key,
        )

    def get_issue_runbook(self, issue_id: int) -> Any:
        """The issue's runbook page, or null when it has none yet."""
        return self._result(self._client.get(f"/issues/{issue_id}/runbook"))

    def list_security_events(
        self,
        *,
        verb: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> Any:
        """Recent boundary refusals — failed logins, revoked tokens, scope
        denials, paused refusals (admin only)."""
        return self._result(
            self._client.get(
                "/security/events",
                params=self._params(verb=verb, since=since, limit=limit),
            )
        )

    def start_playbook(
        self,
        page_id: int,
        *,
        project_id: int | None = None,
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Turn a playbook page's checklist into a parent issue and children."""
        return self._mutate(
            self._client.post,
            f"/pages/{page_id}/start-playbook",
            idempotency_key=idempotency_key,
            json=self._params(project_id=project_id, title=title),
        )

    def my_desk(self) -> Any:
        """Your desk: identity, what is asked of you, what you hold, and what
        changed since your cursor. Includes `office` (the one-chair cubicle)."""
        return self._result(self._client.get("/desk"))

    def my_office(self) -> Any:
        """Your cubicle: one chair, fenced paths, checkout hint."""
        return self._result(self._client.get("/office"))

    def get_project_floor(self, project_id: int) -> Any:
        """One project as a floor of chairs."""
        return self._result(self._client.get(f"/projects/{project_id}/floor"))

    def advance_desk_cursor(self, *, after_id: int) -> Any:
        """Move YOUR desk cursor forward to this activity id."""
        return self._result(
            self._client.post("/desk/cursor", json={"after_id": after_id})
        )

    def activity_chain_status(self) -> Any:
        """Where the trail's hash chain stands — anchor, head hash, coverage
        counts (admin only)."""
        return self._result(self._client.get("/activity/chain"))

    def verify_activity_chain(
        self,
        *,
        after_id: int | None = None,
        limit: int = 1000,
    ) -> Any:
        """Recompute a bounded window of the trail's hash chain (admin only).

        Loop on the returned next_after until has_more is false for a full walk."""
        return self._result(
            self._client.get(
                "/activity/chain/verify",
                params=self._params(after_id=after_id, limit=limit),
            )
        )

    def agent_answerability(self, *, agent_id: int | None = None) -> Any:
        """The ask-and-answer ledger per agent — controls, kills, approvals,
        reversals (admin only). Facts per lane, never a score."""
        return self._result(
            self._client.get(
                "/fleet/answerability", params=self._params(agent_id=agent_id)
            )
        )

    def worker_heartbeat(
        self,
        *,
        worker_key: str,
        node_label: str | None = None,
        capabilities: list[str] | None = None,
        state: str = "running",
        idempotency_key: str | None = None,
    ) -> Any:
        """Register or refresh YOUR worker, and learn whether you were asked to stop."""
        return self._mutate(
            self._client.put,
            "/workers/heartbeat",
            json=self._params(
                worker_key=worker_key,
                node_label=node_label,
                capabilities=capabilities,
                state=state,
            ),
            idempotency_key=idempotency_key,
        )

    def list_workers(self, *, agent_id: int | None = None, limit: int = 100) -> Any:
        """The worker registry: every worker for an admin, your own otherwise."""
        return self._result(
            self._client.get(
                "/workers", params=self._params(agent_id=agent_id, limit=limit)
            )
        )

    def request_worker_kill(
        self, worker_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        """Admin: ask a worker to stop. Records the instruction; cannot end a process."""
        return self._mutate(
            self._client.post,
            f"/workers/{worker_id}/kill",
            idempotency_key=idempotency_key,
        )

    def cancel_worker_kill(
        self, worker_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        """Admin: withdraw a kill request the worker has not acknowledged yet."""
        return self._mutate(
            self._client.delete,
            f"/workers/{worker_id}/kill",
            idempotency_key=idempotency_key,
        )

    def create_run_control(
        self,
        *,
        run_id: str,
        kind: str,
        payload: str | None = None,
        worker_id: int | None = None,
        ttl_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Admin: record a control request against a live run. Records the ask;
        the bound agent answers or it expires.

        The key rides both layers on purpose: in the body it becomes the
        control's domain single-flight binding, and as the Idempotency-Key
        header it gets the transport replay contract every other mutation has."""
        return self._mutate(
            self._client.post,
            "/run-controls",
            json=self._params(
                run_id=run_id,
                kind=kind,
                payload=payload,
                worker_id=worker_id,
                ttl_seconds=ttl_seconds,
                idempotency_key=idempotency_key,
            ),
            idempotency_key=idempotency_key,
        )

    def list_run_controls(
        self,
        *,
        run_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> Any:
        """Every control for an admin; your own addressed controls otherwise."""
        return self._result(
            self._client.get(
                "/run-controls",
                params=self._params(run_id=run_id, state=state, limit=limit),
            )
        )

    def get_run_control(self, control_id: int) -> Any:
        """One control, for an admin or the agent it is addressed to."""
        return self._result(self._client.get(f"/run-controls/{control_id}"))

    def acknowledge_run_control(
        self, control_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        """Record that YOU read this control. Receipt, nothing more."""
        return self._mutate(
            self._client.post,
            f"/run-controls/{control_id}/acknowledge",
            idempotency_key=idempotency_key,
        )

    def decline_run_control(
        self, control_id: int, *, reason: str, idempotency_key: str | None = None
    ) -> Any:
        """Record YOUR refusal of this control, with the reason."""
        return self._mutate(
            self._client.post,
            f"/run-controls/{control_id}/decline",
            json={"reason": reason},
            idempotency_key=idempotency_key,
        )

    def complete_run_control(
        self,
        control_id: int,
        *,
        summary: str | None = None,
        handoff: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Record YOUR completion claim for this control."""
        return self._mutate(
            self._client.post,
            f"/run-controls/{control_id}/complete",
            json=self._params(summary=summary, handoff=handoff),
            idempotency_key=idempotency_key,
        )

    def undo_action(self, event_id: int, *, idempotency_key: str | None = None) -> Any:
        """Reverse one activity event by running its registered inverse as YOU.
        Records a new compensating event; never edits history."""
        return self._mutate(
            self._client.post,
            f"/activity/{event_id}/undo",
            idempotency_key=idempotency_key,
        )

    def list_approvals(self, *, state: str | None = None, limit: int = 100) -> Any:
        """The operator's approval queue (admin only)."""
        return self._result(
            self._client.get(
                "/approvals", params=self._params(state=state, limit=limit)
            )
        )

    def decide_approval(
        self,
        request_id: int,
        *,
        decision: str,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Approve or reject a pending request (admin only)."""
        return self._mutate(
            self._client.post,
            f"/approvals/{request_id}/decision",
            json=self._params(decision=decision, note=note),
            idempotency_key=idempotency_key,
        )

    def set_approval_policy(
        self,
        user_id: int,
        *,
        action_kind: str,
        idempotency_key: str | None = None,
    ) -> Any:
        """Admin: require approval for an action kind by this user."""
        return self._mutate(
            self._client.put,
            f"/approvals/policies/{user_id}",
            json={"action_kind": action_kind},
            idempotency_key=idempotency_key,
        )

    def get_agent_budget(self, user_id: int) -> Any:
        """Read a user's durable action budget, or null when unbudgeted. Admin for
        anyone; any actor may read its own."""
        return self._result(self._client.get(f"/users/{user_id}/budget"))

    def set_agent_budget(
        self,
        user_id: int,
        *,
        window: str,
        action_limit: int,
        idempotency_key: str | None = None,
    ) -> Any:
        """Admin: cap how many metered writes a user may make per fixed window."""
        return self._mutate(
            self._client.put,
            f"/users/{user_id}/budget",
            json={"window": window, "action_limit": action_limit},
            idempotency_key=idempotency_key,
        )

    def clear_agent_budget(
        self, user_id: int, *, idempotency_key: str | None = None
    ) -> Any:
        """Admin: remove a user's budget, returning them to unlimited."""
        return self._mutate(
            self._client.delete,
            f"/users/{user_id}/budget",
            idempotency_key=idempotency_key,
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

    def get_run_replay(self, run_id: str) -> Any:
        """Freeze one visible run into its portable replay artifact."""
        return self._result(self._client.get(f"/activity/runs/{run_id}/replay"))

    def get_run_fork_contract(
        self, run_id: str, *, fork_from_event_id: int, fork_run_id: str
    ) -> Any:
        """Validate and describe how to fork a run from one parent event. The
        returned ``headers`` map onto :meth:`set_run` — apply it and every
        subsequent write continues the fork."""
        return self._result(
            self._client.get(
                f"/activity/runs/{run_id}/fork",
                params=self._params(
                    from_event_id=fork_from_event_id,
                    fork_run_id=fork_run_id,
                ),
            )
        )
