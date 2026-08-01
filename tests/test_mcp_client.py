"""The MCP server's Athena client (and the MCP wiring on top of it).

The MCP server is a client of Athena like the web UI is — it goes through the REST
API with a scoped bearer token. These tests exercise AthenaClient against the REAL
app (an injected TestClient + a real minted token), so the full path tool ->
client -> HTTP -> API -> DB is covered, including auth.

The MCP-SDK wiring itself is tested separately below. Nothing here skips: the
`dev` dependency group always installs the `mcp` extra (see pyproject.toml), so
the wiring tests import the SDK directly and FAIL on an incomplete environment
rather than reporting a green run with the product differentiator untested.
"""

import httpx
import pickle
import pytest
from fastapi.testclient import TestClient

from athena.main import create_app
from athena.aegis import automation, issues
from athena.core import db, dispatch
from athena.mcp.client import AthenaClient, AthenaError


LEASE_GENERATION = "a" * 32
HANDOFF_ARGUMENTS = {
    "attempted_work": "Reproduced the blocker.",
    "evidence": ["focused test failed"],
    "blocking_question": "Which behavior should win?",
    "resume_instructions": "Choose the behavior and rerun the focused test.",
}


def _client(tmp_path, name) -> tuple[TestClient, AthenaClient]:
    """A TestClient with a real admin bearer token set, wrapped in an AthenaClient.
    Returns both so a test can still bootstrap via the raw client if needed."""
    app = create_app(tmp_path / name)
    tc = TestClient(app)
    tc.__enter__()  # run lifespan (migrate); torn down by the fixture-less caller
    # Bootstrap the first user (becomes admin), then mint a token through the
    # trusted-actor path (enabled in tests by conftest).
    tc.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    raw = tc.post(
        "/tokens",
        json={"name": "mcp", "scopes": ["admin"]},
        headers={"X-Athena-Actor": "1"},
    ).json()["token"]
    tc.headers.update({"Authorization": f"Bearer {raw}"})
    return tc, AthenaClient(client=tc)


class _RecordingClient:
    """Small injected transport that records the exact per-call HTTP kwargs."""

    def __init__(self):
        self.calls = []

    def _response(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request(method, f"http://athena.test{path}"),
        )

    def post(self, path, **kwargs):
        return self._response("POST", path, **kwargs)

    def get(self, path, **kwargs):
        self.calls.append(("GET", path, kwargs))
        return httpx.Response(
            200,
            json={"issue": {"id": 7}},
            headers={"ETag": '"context-v1"'},
            request=httpx.Request("GET", f"http://athena.test{path}"),
        )

    def patch(self, path, **kwargs):
        return self._response("PATCH", path, **kwargs)

    def put(self, path, **kwargs):
        return self._response("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self._response("DELETE", path, **kwargs)


MUTATION_CASES = [
    (
        "create_issue",
        "POST",
        "/issues",
        lambda c, k: c.create_issue(title="x", idempotency_key=k),
    ),
    (
        "update_issue",
        "PATCH",
        "/issues/7",
        lambda c, k: c.update_issue(7, title="x", idempotency_key=k),
    ),
    (
        "set_issue_placement",
        "PATCH",
        "/issues/7",
        lambda c, k: c.set_issue_placement(
            7, project_id=None, sprint_id=None, idempotency_key=k
        ),
    ),
    (
        "assign_issue",
        "PUT",
        "/issues/7/assignee",
        lambda c, k: c.assign_issue(7, None, idempotency_key=k),
    ),
    (
        "delegate_issue",
        "POST",
        "/issues/7/delegate",
        lambda c, k: c.delegate_issue(7, 9, idempotency_key=k),
    ),
    (
        "claim_issue",
        "POST",
        "/issues/7/claim",
        lambda c, k: c.claim_issue(7, if_match='"issue-v1"', idempotency_key=k),
    ),
    (
        "yield_claim",
        "POST",
        "/issues/7/yield",
        lambda c, k: c.yield_claim(
            7,
            generation=LEASE_GENERATION,
            reason="blocked",
            note="waiting",
            idempotency_key=k,
            **HANDOFF_ARGUMENTS,
        ),
    ),
    (
        "resume_claim_handoff",
        "POST",
        "/issues/7/claim-handoffs/" + ("b" * 32) + "/resume",
        lambda c, k: c.resume_claim_handoff(
            7,
            "b" * 32,
            generation=LEASE_GENERATION,
            idempotency_key=k,
        ),
    ),
    (
        "complete_claim",
        "POST",
        "/issues/7/complete",
        lambda c, k: c.complete_claim(
            7, generation=LEASE_GENERATION, idempotency_key=k
        ),
    ),
    (
        "decline_delegation",
        "POST",
        "/issues/7/decline",
        lambda c, k: c.decline_delegation(7, idempotency_key=k),
    ),
    (
        "comment_on_issue",
        "POST",
        "/issues/7/comments",
        lambda c, k: c.comment_on_issue(7, "x", idempotency_key=k),
    ),
    (
        "archive_issue",
        "POST",
        "/issues/7/archive",
        lambda c, k: c.archive_issue(7, idempotency_key=k),
    ),
    (
        "unarchive_issue",
        "POST",
        "/issues/7/unarchive",
        lambda c, k: c.unarchive_issue(7, idempotency_key=k),
    ),
    (
        "bulk_update_issues",
        "POST",
        "/issues/bulk",
        lambda c, k: c.bulk_update_issues([7], status="done", idempotency_key=k),
    ),
    (
        "set_issue_parent",
        "PUT",
        "/issues/7/parent",
        lambda c, k: c.set_issue_parent(7, None, idempotency_key=k),
    ),
    (
        "link_issues",
        "POST",
        "/issues/7/links",
        lambda c, k: c.link_issues(7, "9", "blocks", idempotency_key=k),
    ),
    (
        "unlink_issues",
        "DELETE",
        "/issues/7/links/blocks/9",
        lambda c, k: c.unlink_issues(7, "blocks", 9, idempotency_key=k),
    ),
    (
        "create_sprint",
        "POST",
        "/projects/4/sprints",
        lambda c, k: c.create_sprint(4, name="Cycle 1", idempotency_key=k),
    ),
    (
        "update_sprint",
        "PATCH",
        "/sprints/7",
        lambda c, k: c.update_sprint(7, name="Cycle One", idempotency_key=k),
    ),
    (
        "start_sprint",
        "POST",
        "/sprints/7/start",
        lambda c, k: c.start_sprint(7, idempotency_key=k),
    ),
    (
        "complete_sprint",
        "POST",
        "/sprints/7/complete",
        lambda c, k: c.complete_sprint(7, idempotency_key=k),
    ),
    (
        "delete_sprint",
        "DELETE",
        "/sprints/7",
        lambda c, k: c.delete_sprint(7, idempotency_key=k),
    ),
    (
        "set_issue_sprint",
        "PUT",
        "/issues/7/sprint",
        lambda c, k: c.set_issue_sprint(7, None, idempotency_key=k),
    ),
    (
        "create_label",
        "POST",
        "/labels",
        lambda c, k: c.create_label("bug", idempotency_key=k),
    ),
    (
        "attach_label",
        "POST",
        "/issues/7/labels",
        lambda c, k: c.attach_label(7, 9, idempotency_key=k),
    ),
    (
        "detach_label",
        "DELETE",
        "/issues/7/labels/9",
        lambda c, k: c.detach_label(7, 9, idempotency_key=k),
    ),
    (
        "create_page",
        "POST",
        "/spaces/4/pages",
        lambda c, k: c.create_page(space_id=4, title="x", idempotency_key=k),
    ),
    (
        "update_page",
        "PATCH",
        "/pages/4",
        lambda c, k: c.update_page(4, title="x", idempotency_key=k),
    ),
    (
        "archive_page",
        "POST",
        "/pages/4/archive",
        lambda c, k: c.archive_page(4, idempotency_key=k),
    ),
    (
        "unarchive_page",
        "POST",
        "/pages/4/unarchive",
        lambda c, k: c.unarchive_page(4, idempotency_key=k),
    ),
    (
        "restore_page_version",
        "POST",
        "/pages/4/versions/2/restore",
        lambda c, k: c.restore_page_version(4, 2, idempotency_key=k),
    ),
    (
        "dispatch_to_icarus",
        "POST",
        "/issues/5/dispatch",
        lambda c, k: c.dispatch_to_icarus(
            5, repo="r", base_commit="c", capability="repo.edit", idempotency_key=k
        ),
    ),
    (
        "record_run_learning",
        "POST",
        "/issues/5/learnings",
        lambda c, k: c.record_run_learning(5, summary="learned", idempotency_key=k),
    ),
    (
        "worker_heartbeat",
        "PUT",
        "/workers/heartbeat",
        lambda c, k: c.worker_heartbeat(worker_key="w-1", idempotency_key=k),
    ),
    (
        "request_worker_kill",
        "POST",
        "/workers/3/kill",
        lambda c, k: c.request_worker_kill(3, idempotency_key=k),
    ),
    (
        "cancel_worker_kill",
        "DELETE",
        "/workers/3/kill",
        lambda c, k: c.cancel_worker_kill(3, idempotency_key=k),
    ),
    (
        "undo_action",
        "POST",
        "/activity/12/undo",
        lambda c, k: c.undo_action(12, idempotency_key=k),
    ),
    (
        "decide_approval",
        "POST",
        "/approvals/7/decision",
        lambda c, k: c.decide_approval(
            7, decision="approve", note="ok", idempotency_key=k
        ),
    ),
    (
        "set_approval_policy",
        "PUT",
        "/approvals/policies/9",
        lambda c, k: c.set_approval_policy(
            9, action_kind="issue.close", idempotency_key=k
        ),
    ),
    (
        "set_agent_budget",
        "PUT",
        "/users/9/budget",
        lambda c, k: c.set_agent_budget(
            9, window="day", action_limit=50, idempotency_key=k
        ),
    ),
    (
        "clear_agent_budget",
        "DELETE",
        "/users/9/budget",
        lambda c, k: c.clear_agent_budget(9, idempotency_key=k),
    ),
    (
        "label_page",
        "POST",
        "/pages/4/labels",
        lambda c, k: c.label_page(4, 9, idempotency_key=k),
    ),
    (
        "unlabel_page",
        "DELETE",
        "/pages/4/labels/9",
        lambda c, k: c.unlabel_page(4, 9, idempotency_key=k),
    ),
    (
        "create_automation_rule",
        "POST",
        "/automation/rules",
        lambda c, k: c.create_automation_rule(
            name="rule",
            trigger_verb="created",
            action_type="comment",
            idempotency_key=k,
        ),
    ),
    (
        "set_automation_rule_enabled",
        "PATCH",
        "/automation/rules/7",
        lambda c, k: c.set_automation_rule_enabled(7, False, idempotency_key=k),
    ),
    (
        "delete_automation_rule",
        "DELETE",
        "/automation/rules/7",
        lambda c, k: c.delete_automation_rule(7, idempotency_key=k),
    ),
    (
        "post_room_event",
        "POST",
        "/rooms/11/events",
        lambda c, k: c.post_room_event(
            11, event_kind="message", body="x", idempotency_key=k
        ),
    ),
]

MUTATION_TOOL_NAMES = {case[0] for case in MUTATION_CASES}
REQUIRED_IF_MATCH_TOOL_NAMES = {"claim_issue"}
OPTIONAL_IF_MATCH_TOOL_NAMES = {
    "update_issue",
    "set_issue_placement",
    "assign_issue",
    "set_issue_sprint",
    "update_page",
}
IF_MATCH_TOOL_NAMES = REQUIRED_IF_MATCH_TOOL_NAMES | OPTIONAL_IF_MATCH_TOOL_NAMES
MCP_MUTATION_CASES = [
    ("create_issue", {"title": "x"}),
    ("update_issue", {"issue_id": 7}),
    (
        "set_issue_placement",
        {"issue_id": 7, "project_id": None, "sprint_id": None},
    ),
    ("assign_issue", {"issue_id": 7}),
    ("delegate_issue", {"issue_id": 7, "agent_user_id": 9}),
    ("claim_issue", {"issue_id": 7, "if_match": '"issue-v1"'}),
    (
        "yield_claim",
        {
            "issue_id": 7,
            "generation": LEASE_GENERATION,
            "reason": "blocked",
            "note": "waiting",
            **HANDOFF_ARGUMENTS,
        },
    ),
    (
        "resume_claim_handoff",
        {
            "issue_id": 7,
            "handoff_token": "b" * 32,
            "generation": LEASE_GENERATION,
        },
    ),
    ("complete_claim", {"issue_id": 7, "generation": LEASE_GENERATION}),
    ("decline_delegation", {"issue_id": 7}),
    ("comment_on_issue", {"issue_id": 7, "body": "x"}),
    ("archive_issue", {"issue_id": 7}),
    ("unarchive_issue", {"issue_id": 7}),
    ("bulk_update_issues", {"ids": [7]}),
    ("set_issue_parent", {"issue_id": 7}),
    (
        "link_issues",
        {"issue_id": 7, "target_ref": "9", "relation": "blocks"},
    ),
    (
        "unlink_issues",
        {"issue_id": 7, "relation": "blocks", "target_id": 9},
    ),
    ("create_sprint", {"project_id": 4, "name": "Cycle 1"}),
    ("update_sprint", {"sprint_id": 7, "name": "Cycle One"}),
    ("start_sprint", {"sprint_id": 7}),
    ("complete_sprint", {"sprint_id": 7}),
    (
        "delete_sprint",
        {"sprint_id": 7, "confirm_permanent": True},
    ),
    ("set_issue_sprint", {"issue_id": 7}),
    ("create_label", {"name": "bug"}),
    ("attach_label", {"issue_id": 7, "label_id": 9}),
    ("detach_label", {"issue_id": 7, "label_id": 9}),
    ("create_page", {"space_id": 4, "title": "x"}),
    ("update_page", {"page_id": 4}),
    ("archive_page", {"page_id": 4}),
    ("unarchive_page", {"page_id": 4}),
    ("restore_page_version", {"page_id": 4, "version": 2}),
    (
        "dispatch_to_icarus",
        {"issue_id": 5, "repo": "r", "base_commit": "c", "capability": "repo.edit"},
    ),
    ("record_run_learning", {"issue_id": 5, "summary": "learned"}),
    ("worker_heartbeat", {"worker_key": "w-1"}),
    ("request_worker_kill", {"worker_id": 3}),
    ("cancel_worker_kill", {"worker_id": 3}),
    ("undo_action", {"event_id": 12}),
    ("decide_approval", {"request_id": 7, "decision": "approve"}),
    ("set_approval_policy", {"user_id": 9, "action_kind": "issue.close"}),
    ("set_agent_budget", {"user_id": 9, "window": "day", "action_limit": 50}),
    ("clear_agent_budget", {"user_id": 9}),
    ("label_page", {"page_id": 4, "label_id": 9}),
    ("unlabel_page", {"page_id": 4, "label_id": 9}),
    (
        "create_automation_rule",
        {"name": "rule", "trigger_verb": "created", "action_type": "comment"},
    ),
    (
        "set_automation_rule_enabled",
        {"rule_id": 7, "enabled": False},
    ),
    ("delete_automation_rule", {"rule_id": 7}),
    (
        "post_room_event",
        {"room_id": 11, "event_kind": "message", "body": "x"},
    ),
]
MCP_IF_MATCH_CASES = [
    case for case in MCP_MUTATION_CASES if case[0] in IF_MATCH_TOOL_NAMES
]
MCP_SPRINT_RESOURCE_CASES = [
    ("list_sprints", "project_id", {}),
    ("get_sprint", "sprint_id", {}),
    ("create_sprint", "project_id", {"name": "Cycle 1"}),
    ("update_sprint", "sprint_id", {"name": "Cycle One"}),
    ("start_sprint", "sprint_id", {}),
    ("complete_sprint", "sprint_id", {}),
    ("delete_sprint", "sprint_id", {"confirm_permanent": True}),
    ("set_issue_sprint", "sprint_id", {"issue_id": 7}),
]


class _MCPRecordingAthenaClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in {"yield_claim", "delete_sprint"}:
                return None
            return [] if name in {"delegate_issue", "list_sprints"} else {}

        return record


class _MCPFailingAthenaClient(_MCPRecordingAthenaClient):
    def create_issue(self, **kwargs):
        raise AthenaError(
            method="POST",
            path="/issues",
            status_code=409,
            detail="still running",
            code="idempotency_in_progress",
            retry_after="7",
            current_etag='"current"',
        )


class _MCPFailingReadAthenaClient(_MCPRecordingAthenaClient):
    def get_issue(self, ref):
        raise AthenaError(
            method="GET",
            path=f"/issues/{ref}",
            status_code=429,
            detail="slow down",
            code="rate_limited",
            retry_after="11",
            current_etag='"fresh"',
        )


@pytest.mark.parametrize(
    ("name", "method", "path", "invoke"),
    MUTATION_CASES,
    ids=[case[0] for case in MUTATION_CASES],
)
def test_every_client_mutation_forwards_only_explicit_idempotency_keys(
    name, method, path, invoke
):
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    for key, expected_headers in (
        ("stable-key", {"Idempotency-Key": "stable-key"}),
        (None, None),
        ("", {"Idempotency-Key": ""}),
    ):
        assert invoke(client, key) == {"ok": True}
        recorded_method, recorded_path, kwargs = transport.calls.pop()
        assert (recorded_method, recorded_path) == (method, path), name
        expected = dict(expected_headers or {})
        if name == "claim_issue":
            expected["If-Match"] = '"issue-v1"'
        if expected:
            assert kwargs["headers"] == expected, name
        else:
            assert "headers" not in kwargs, name


def test_automation_rule_client_matches_rest_contract_and_schedule_fields():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    client.list_automation_rules()
    assert transport.calls.pop() == ("GET", "/automation/rules", {})

    client.get_automation_rule(7)
    assert transport.calls.pop() == ("GET", "/automation/rules/7", {})

    client.create_automation_rule(
        name="stale work",
        trigger_verb="scheduled",
        action_type="comment",
        conditions={"inactive_for_seconds": 3600},
        action_params={"body": "check in"},
        trigger_type="schedule",
        schedule_at="2030-01-02T03:04:05Z",
        schedule_every_seconds=3600,
    )
    method, path, kwargs = transport.calls.pop()
    assert (method, path) == ("POST", "/automation/rules")
    assert kwargs["json"] == {
        "name": "stale work",
        "trigger_verb": "scheduled",
        "action_type": "comment",
        "conditions": {"inactive_for_seconds": 3600},
        "action_params": {"body": "check in"},
        "target_kind": "issue",
        "trigger_type": "schedule",
        "schedule_at": "2030-01-02T03:04:05Z",
        "schedule_every_seconds": 3600,
    }

    client.set_automation_rule_enabled(7, False)
    assert transport.calls.pop() == (
        "PATCH",
        "/automation/rules/7",
        {"json": {"enabled": False}},
    )

    client.delete_automation_rule(7)
    assert transport.calls.pop() == ("DELETE", "/automation/rules/7", {})

    client.list_automation_failures()
    assert transport.calls.pop() == (
        "GET",
        "/automation/rules",
        {"params": {"failing_only": True}},
    )


def test_automation_rule_client_omits_unused_schedule_fields_for_event_rule():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    client.create_automation_rule(
        name="new work",
        trigger_verb="created",
        action_type="comment",
    )
    _, _, kwargs = transport.calls.pop()
    assert kwargs["json"]["trigger_type"] == "event"
    assert "schedule_at" not in kwargs["json"]
    assert "schedule_every_seconds" not in kwargs["json"]


def test_yield_client_forwards_reason_and_note():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    assert client.yield_claim(
        7,
        generation=LEASE_GENERATION,
        reason="blocked",
        note="waiting",
        **HANDOFF_ARGUMENTS,
    ) == {"ok": True}

    method, path, kwargs = transport.calls.pop()
    assert (method, path) == ("POST", "/issues/7/yield")
    assert kwargs["json"] == {
        "generation": LEASE_GENERATION,
        "reason": "blocked",
        **HANDOFF_ARGUMENTS,
        "note": "waiting",
    }


def test_mutation_helper_merges_existing_headers_case_insensitively():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    client._mutate(
        transport.post,
        "/issues",
        idempotency_key="new-key",
        if_match='"v2"',
        headers={"if-match": '"v1"', "idempotency-key": "old-key"},
        json={"title": "x"},
    )

    _, _, kwargs = transport.calls.pop()
    headers = httpx.Headers(kwargs["headers"])
    assert headers["If-Match"] == '"v2"'
    assert headers["Idempotency-Key"] == "new-key"
    assert len(headers.get_list("If-Match")) == 1
    assert len(headers.get_list("Idempotency-Key")) == 1


def test_heartbeat_client_puts_only_run_id_without_idempotency_header():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    assert client.heartbeat_agent_run("run-7") == {"ok": True}

    method, path, kwargs = transport.calls.pop()
    assert (method, path) == ("PUT", "/agent-runs/heartbeat")
    assert kwargs == {"json": {"run_id": "run-7"}}


def test_work_context_client_gets_packet_through_result_and_exposes_etag():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    assert client.get_issue_work_context("ATH-7") == {
        "issue": {"id": 7},
        "_etag": '"context-v1"',
    }
    assert transport.calls == [("GET", "/issues/ATH-7/work-context", {})]


def test_list_issues_forwards_sprint_only_when_explicit():
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    client.list_issues(sprint=7)
    assert transport.calls.pop() == ("GET", "/issues", {"params": {"sprint": 7}})

    client.list_issues(sprint=None)
    assert transport.calls.pop() == ("GET", "/issues", {"params": {}})


def test_sprint_lifecycle_client_matches_rest_contract():
    # WHY: MCP must express REST's omitted-vs-null date semantics without sending
    # an editable state field or accidental request bodies on transitions.
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    client.list_sprints(4, state="planned")
    assert transport.calls.pop() == (
        "GET",
        "/projects/4/sprints",
        {"params": {"state": "planned"}},
    )

    client.get_sprint(7)
    assert transport.calls.pop() == ("GET", "/sprints/7", {})

    client.create_sprint(
        4,
        name="Cycle 1",
        goal="ship it",
        start_date="2030-01-02",
    )
    assert transport.calls.pop() == (
        "POST",
        "/projects/4/sprints",
        {
            "json": {
                "name": "Cycle 1",
                "goal": "ship it",
                "start_date": "2030-01-02",
            }
        },
    )

    client.update_sprint(
        7,
        goal="",
        end_date="2030-01-09",
        clear_start_date=True,
    )
    assert transport.calls.pop() == (
        "PATCH",
        "/sprints/7",
        {
            "json": {
                "goal": "",
                "end_date": "2030-01-09",
                "start_date": None,
            }
        },
    )

    client.start_sprint(7)
    assert transport.calls.pop() == ("POST", "/sprints/7/start", {})

    client.complete_sprint(7)
    assert transport.calls.pop() == ("POST", "/sprints/7/complete", {})

    def delete_without_content(path, **kwargs):
        transport.calls.append(("DELETE", path, kwargs))
        return httpx.Response(
            204,
            request=httpx.Request("DELETE", f"http://athena.test{path}"),
        )

    transport.delete = delete_without_content
    assert client.delete_sprint(7) is None
    assert transport.calls.pop() == ("DELETE", "/sprints/7", {})


def test_update_sprint_rejects_conflicting_date_intent_before_dispatch():
    # WHY: a retry must not ambiguously ask to set and clear the same date.
    transport = _RecordingClient()
    client = AthenaClient(client=transport)

    with pytest.raises(ValueError, match="start_date and clear_start_date"):
        client.update_sprint(7, start_date="2030-01-02", clear_start_date=True)
    with pytest.raises(ValueError, match="end_date and clear_end_date"):
        client.update_sprint(7, end_date="2030-01-09", clear_end_date=True)

    assert transport.calls == []


def test_athena_error_preserves_legacy_construction_and_pickle_state():
    legacy = AthenaError("legacy failure")
    assert str(legacy) == "legacy failure"

    structured = AthenaError(
        method="POST",
        path="/issues",
        status_code=409,
        detail="still running",
        code="idempotency_in_progress",
        retry_after="7",
        current_etag='"current"',
    )
    restored = pickle.loads(pickle.dumps(structured))

    assert str(restored) == str(structured)
    assert restored.as_dict() == structured.as_dict()


def test_issue_lifecycle_through_the_client(tmp_path):
    tc, ath = _client(tmp_path, "iss.db")
    try:
        issue = ath.create_issue(
            title="ship it", body="see [[page:1]]", priority="high"
        )
        assert issue["title"] == "ship it" and issue["priority"] == "high"

        # Addressable by id and (once in a project) by key; here by id.
        assert ath.get_issue(str(issue["id"]))["id"] == issue["id"]

        moved = ath.update_issue(issue["id"], status="in_progress")
        assert moved["status"] == "in_progress"

        ath.comment_on_issue(issue["id"], "on it")
        assert ath.list_issues(status="in_progress")[0]["id"] == issue["id"]

        # Full-text search spans the issue we just made.
        hits = ath.search("ship")
        assert any(h["source_id"] == issue["id"] and h["kind"] == "issue" for h in hits)
    finally:
        tc.__exit__(None, None, None)


def test_client_exposes_etags_and_surfaces_stale_issue_preconditions(tmp_path):
    tc, ath = _client(tmp_path, "etag-client.db")
    try:
        created = ath.create_issue(title="before")
        assert created["_etag"].startswith('"sha256-')

        fetched = ath.get_issue(str(created["id"]))
        assert fetched["_etag"] == created["_etag"]

        updated = ath.update_issue(
            created["id"],
            title="after",
            if_match=fetched["_etag"],
            idempotency_key="guarded-update",
        )
        assert updated["title"] == "after"
        assert updated["_etag"] != fetched["_etag"]

        with pytest.raises(AthenaError) as stale:
            ath.update_issue(
                created["id"],
                title="lost update",
                if_match=fetched["_etag"],
            )
        assert stale.value.status_code == 412
        assert stale.value.code == "precondition_failed"
        assert stale.value.current_etag == updated["_etag"]
        assert ath.get_issue(str(created["id"]))["title"] == "after"
    finally:
        tc.__exit__(None, None, None)


def test_create_issue_omits_status_so_project_default_applies(tmp_path):
    # WHY: project statuses are configurable. MCP must not force the old global
    # "open" default, or agents cannot create issues in projects whose first status
    # was renamed/removed.
    tc, ath = _client(tmp_path, "custom-status.db")
    try:
        project = tc.post("/projects", json={"name": "Ops", "key": "OPS"}).json()
        assert tc.delete(f"/projects/{project['id']}/statuses/open").status_code == 200

        issue = ath.create_issue(title="use project default", project_id=project["id"])
        assert issue["status"] == "in_progress"

        explicit = ath.create_issue(
            title="explicit status", project_id=project["id"], status="done"
        )
        assert explicit["status"] == "done"
    finally:
        tc.__exit__(None, None, None)


def test_assign_and_list_users(tmp_path):
    tc, ath = _client(tmp_path, "assign.db")
    try:
        users = ath.list_users()
        assert users and users[0]["id"] == 1
        issue = ath.create_issue(title="assign me")
        assigned = ath.assign_issue(issue["id"], 1)
        assert assigned["assignee_id"] == 1
        # Unassign.
        assert ath.assign_issue(issue["id"], None)["assignee_id"] is None
    finally:
        tc.__exit__(None, None, None)


def test_delegate_issue_through_the_client(tmp_path):
    # WHY: agent delegation must be reachable through MCP as a first-class action,
    # while preserving the human assignee as the accountable owner.
    tc, ath = _client(tmp_path, "delegate.db")
    try:
        agent = tc.post(
            "/users",
            json={"email": "bot@e.com", "name": "Bot", "is_agent": True},
            headers={"X-Athena-Actor": "1"},
        ).json()
        issue = ath.create_issue(title="delegate")
        ath.assign_issue(issue["id"], 1)

        contributors = ath.delegate_issue(issue["id"], agent["id"])
        assert contributors[0]["user_id"] == agent["id"]
        assert contributors[0]["is_agent"] is True
        assert ath.get_issue(str(issue["id"]))["assignee_id"] == 1
        assert "delegated" in [
            e["verb"] for e in ath.recent_events(kind="issue")["events"]
        ]
    finally:
        tc.__exit__(None, None, None)


def test_pages_through_the_client(tmp_path):
    tc, ath = _client(tmp_path, "pages.db")
    try:
        space = tc.post("/spaces", json={"key": "ENG", "name": "Eng"}).json()
        page = ath.create_page(space_id=space["id"], title="Runbook", body="# Step")
        assert page["title"] == "Runbook"
        assert ath.get_page(page["id"])["id"] == page["id"]
        assert [p["id"] for p in ath.list_pages(space["id"])] == [page["id"]]
        updated = ath.update_page(page["id"], body="# Step 1\n# Step 2")
        assert "Step 2" in updated["body"]
        assert any(s["key"] == "ENG" for s in ath.list_spaces())
    finally:
        tc.__exit__(None, None, None)


def test_find_pages_by_title_through_the_client(tmp_path):
    # WHY: an agent addresses a page by the title it remembers, not a numeric id. The
    # client turns that title into the page(s) it names — and disambiguates by space
    # when a title is reused, so the numeric id it then hands to get_page is the right one.
    tc, ath = _client(tmp_path, "bytitle.db")
    try:
        eng = tc.post("/spaces", json={"key": "ENG", "name": "Eng"}).json()
        ops = tc.post("/spaces", json={"key": "OPS", "name": "Ops"}).json()
        eng_doc = ath.create_page(space_id=eng["id"], title="Runbook", body="eng")
        ops_doc = ath.create_page(space_id=ops["id"], title="Runbook", body="ops")
        ath.create_page(space_id=eng["id"], title="Onboarding")

        # A unique title resolves to exactly one page.
        onboarding = ath.find_pages_by_title("Onboarding")
        assert [p["id"] for p in onboarding] == [
            p["id"] for p in ath.list_pages(eng["id"]) if p["title"] == "Onboarding"
        ]

        # A reused title returns every match (case-insensitively), newest space last.
        both = ath.find_pages_by_title("runbook")
        assert {p["id"] for p in both} == {eng_doc["id"], ops_doc["id"]}

        # space_id narrows the ambiguity to one space.
        narrowed = ath.find_pages_by_title("Runbook", space_id=ops["id"])
        assert [p["id"] for p in narrowed] == [ops_doc["id"]]

        # An unknown title is an empty list, not an error.
        assert ath.find_pages_by_title("Nothing Here") == []
    finally:
        tc.__exit__(None, None, None)


def test_recent_events_envelope(tmp_path):
    tc, ath = _client(tmp_path, "ev.db")
    try:
        # Client bootstrap mints a token, which is now itself an audited event, so
        # start the cursor past that setup noise to assert only this test's actions.
        baseline = ath.recent_events()["next_after"]
        ath.create_issue(title="one")
        ath.create_issue(title="two")
        feed = ath.recent_events(after=baseline)
        assert set(feed) == {"events", "next_after", "has_more"}
        assert [e["verb"] for e in feed["events"]] == ["created", "created"]
        # Resume from the cursor: no new events yet.
        assert ath.recent_events(after=feed["next_after"])["events"] == []
    finally:
        tc.__exit__(None, None, None)


def test_mission_control_observation_through_client(tmp_path):
    # WHY: fleet supervision must traverse the same MCP-client -> REST -> DB path as
    # every other agent capability; a web-only cockpit cannot be automated or audited.
    db_file = tmp_path / "mission-control.db"
    tc, ath = _client(tmp_path, db_file.name)
    try:
        agent = tc.post(
            "/users",
            json={"email": "bot@e.com", "name": "Bot", "is_agent": True},
        ).json()
        acted = tc.post(
            "/issues",
            json={"title": "Observed work"},
            headers={
                "Authorization": "not-bearer",
                "X-Athena-Actor": str(agent["id"]),
                "X-Athena-Run": "bot-run",
            },
        )
        assert acted.status_code == 201

        rule = tc.post(
            "/automation/rules",
            json={
                "name": "broken rule",
                "trigger_verb": "created",
                "action_type": "comment",
                "action_params": {"body": "x"},
            },
        ).json()
        conn = db.connect(db_file)
        try:
            automation.record_rule_failure(
                conn, rule["id"], "RuntimeError: operator-visible"
            )
        finally:
            conn.close()

        health = ath.get_agent_run_health()
        assert health["totals"]["agents_with_activity_count"] == 1
        assert [row["user"]["id"] for row in health["agents"]] == [agent["id"]]
        assert health["agents"][0]["latest_run"]["run_id"] == "bot-run"
        assert "replay_export_command" not in health["agents"][0]["latest_run"]
        assert "has_password" not in health["agents"][0]["user"]

        filtered = ath.get_agent_run_health(agent_id=agent["id"])
        assert [row["user"]["id"] for row in filtered["agents"]] == [agent["id"]]

        failures = ath.list_automation_failures()
        assert [item["id"] for item in failures] == [rule["id"]]
        assert failures[0]["last_error"] == "RuntimeError: operator-visible"
    finally:
        tc.__exit__(None, None, None)


def test_automation_rule_lifecycle_through_client(tmp_path):
    tc, ath = _client(tmp_path, "automation-rule-client.db")
    try:
        created = ath.create_automation_rule(
            name="stale open work",
            trigger_verb="scheduled",
            action_type="comment",
            action_params={"body": "please check in"},
            trigger_type="schedule",
            schedule_at="2030-01-02T03:04:05Z",
            schedule_every_seconds=3600,
            idempotency_key="create-scheduled-rule",
        )

        assert {
            "trigger_type",
            "schedule_at",
            "schedule_every_seconds",
            "next_scheduled_at",
            "schedule_status",
        } <= set(created)
        assert created["trigger_type"] == "schedule"
        assert created["schedule_at"] == "2030-01-02T03:04:05Z"
        assert created["schedule_every_seconds"] == 3600
        assert [rule["id"] for rule in ath.list_automation_rules()] == [created["id"]]
        assert ath.get_automation_rule(created["id"])["id"] == created["id"]

        disabled = ath.set_automation_rule_enabled(
            created["id"],
            False,
            idempotency_key="disable-scheduled-rule",
        )
        assert disabled["enabled"] is False
        assert disabled["schedule_status"] == "disabled"
        assert (
            ath.set_automation_rule_enabled(
                created["id"],
                False,
                idempotency_key="disable-scheduled-rule",
            )
            == disabled
        )

        assert (
            ath.delete_automation_rule(
                created["id"], idempotency_key="delete-scheduled-rule"
            )
            is None
        )
        with pytest.raises(AthenaError) as missing:
            ath.get_automation_rule(created["id"])
        assert missing.value.status_code == 404
    finally:
        tc.__exit__(None, None, None)


def test_run_lineage_and_issue_time_travel_through_the_client(tmp_path):
    # WHY: the newest log-as-truth features must be reachable by agents over MCP,
    # not only by browser/REST users: reconstruct runs, walk run lineage, and
    # time-travel an issue's lifecycle state from the same activity log.
    tc, ath = _client(tmp_path, "runs.db")
    try:
        tc.headers.update({"X-Athena-Run": "goal"})
        issue = ath.create_issue(title="lineage")

        tc.headers.update({"X-Athena-Run": "child", "X-Athena-Parent-Run": "goal"})
        ath.update_issue(issue["id"], status="in_progress")

        events = ath.recent_events(kind="issue")["events"]
        created_id = min(e["id"] for e in events if e["verb"] == "created")

        now = ath.get_issue_state(issue["id"])
        assert now["state"]["status"] == "in_progress"
        assert now["is_current"] is True

        then = ath.get_issue_state(issue["id"], as_of_event_id=created_id)
        assert then["state"]["status"] == "open"
        assert then["is_current"] is False

        runs = ath.list_activity_runs(actor_id=1)
        assert {"goal", "child"} <= {r["run_id"] for r in runs}

        lineage = ath.get_run_lineage("goal")
        assert lineage["run"]["run_id"] == "goal"
        assert [d["run_id"] for d in lineage["descendants"]] == ["child"]
        assert [a["run_id"] for a in ath.get_run_lineage("child")["ancestors"]] == [
            "goal"
        ]

        contract = ath.get_run_fork_contract(
            "goal", fork_from_event_id=created_id, fork_run_id="goal:alt"
        )
        assert contract["parent_run_id"] == "goal"
        assert contract["fork_run_id"] == "goal:alt"
        assert contract["fork_from_event_id"] == created_id
        assert contract["headers"] == {
            "X-Athena-Run": "goal:alt",
            "X-Athena-Parent-Run": "goal",
            "X-Athena-Fork-From-Event": str(created_id),
        }
        assert [event["id"] for event in contract["shared_prefix_events"]] == [
            created_id
        ]
    finally:
        tc.__exit__(None, None, None)


def test_archive_unarchive_through_the_client(tmp_path):
    # WHY: agents must be able to archive/restore issues and SEE archived ones via
    # MCP, not just through the web/REST — the full archival surface, reachable.
    tc, ath = _client(tmp_path, "arch.db")
    try:
        keep = ath.create_issue(title="keep")
        gone = ath.create_issue(title="gone")
        assert ath.archive_issue(gone["id"])["archived_at"] is not None
        # Default listing hides it; include_archived reveals it.
        assert [i["id"] for i in ath.list_issues()] == [keep["id"]]
        assert {i["id"] for i in ath.list_issues(include_archived=True)} == {
            keep["id"],
            gone["id"],
        }
        assert ath.unarchive_issue(gone["id"])["archived_at"] is None
        assert {i["id"] for i in ath.list_issues()} == {keep["id"], gone["id"]}
    finally:
        tc.__exit__(None, None, None)


def test_bulk_update_through_the_client(tmp_path):
    # WHY: agents must move many issues in one call, not N — the bulk endpoint is
    # reachable as a single MCP tool, best-effort with per-item results.
    tc, ath = _client(tmp_path, "bulk.db")
    try:
        proj = tc.post("/projects", json={"name": "Ops", "key": "OPS"}).json()
        a = ath.create_issue(title="a", project_id=proj["id"])
        b = ath.create_issue(title="b", project_id=proj["id"])
        out = ath.bulk_update_issues(
            [a["id"], b["id"], 9999], status="in_progress", assignee_id=1
        )
        assert out["updated"] == 2 and out["failed"] == 1
        outcomes = {r["id"]: r for r in out["results"]}
        assert outcomes[a["id"]]["ok"] and not outcomes[9999]["ok"]
        # The change actually landed.
        assert ath.get_issue(str(a["id"]))["status"] == "in_progress"
    finally:
        tc.__exit__(None, None, None)


def test_atomic_issue_placement_through_the_client(tmp_path, monkeypatch):
    # WHY: an agent moving an issue between projects must send the destination
    # project and sprint in ONE request. Sequential project/sprint tools expose an
    # invalid intermediate placement and cannot express an atomic final pair.
    tc, ath = _client(tmp_path, "placement.db")
    try:
        source = tc.post("/projects", json={"name": "Source", "key": "SRC"}).json()
        target = tc.post("/projects", json={"name": "Target", "key": "TGT"}).json()
        source_sprint = tc.post(
            f"/projects/{source['id']}/sprints", json={"name": "Source sprint"}
        ).json()
        target_sprint = tc.post(
            f"/projects/{target['id']}/sprints", json={"name": "Target sprint"}
        ).json()
        issue = ath.create_issue(title="move once", project_id=source["id"])
        ath.set_issue_sprint(issue["id"], source_sprint["id"])

        patch_calls = []
        real_patch = tc.patch

        def recording_patch(path, **kwargs):
            patch_calls.append((path, kwargs.get("json")))
            return real_patch(path, **kwargs)

        monkeypatch.setattr(tc, "patch", recording_patch)

        moved = ath.set_issue_placement(
            issue["id"], project_id=target["id"], sprint_id=target_sprint["id"]
        )
        assert patch_calls == [
            (
                f"/issues/{issue['id']}",
                {"project_id": target["id"], "sprint_id": target_sprint["id"]},
            )
        ]

        assert (moved["project_id"], moved["sprint_id"]) == (
            target["id"],
            target_sprint["id"],
        )

        # Null is a value on this dedicated surface, not an omitted optional field.
        backlog = ath.set_issue_placement(issue["id"], project_id=None, sprint_id=None)
        assert patch_calls[-1] == (
            f"/issues/{issue['id']}",
            {"project_id": None, "sprint_id": None},
        )

        assert (backlog["project_id"], backlog["sprint_id"]) == (None, None)

        # The server's relationship error is preserved for the agent, and the
        # rejected pair does not partially move the issue.
        with pytest.raises(AthenaError) as exc:
            ath.set_issue_placement(
                issue["id"],
                project_id=target["id"],
                sprint_id=source_sprint["id"],
            )
        assert "422" in str(exc.value)
        assert "sprint belongs to a different project than the issue" in str(exc.value)
        unchanged = ath.get_issue(str(issue["id"]))
        assert (unchanged["project_id"], unchanged["sprint_id"]) == (None, None)
    finally:
        tc.__exit__(None, None, None)


def test_hierarchy_deps_sprints_labels_through_the_client(tmp_path):
    # WHY: these surfaces (parent/child, dependencies, sprints, labels) all have
    # REST endpoints + data layers, but were unreachable via MCP — an agent could
    # create an issue but not organize it. This drives the full path for each.
    tc, ath = _client(tmp_path, "cover.db")
    try:
        proj = tc.post("/projects", json={"name": "Ops", "key": "OPS"}).json()
        epic = ath.create_issue(title="Epic", project_id=proj["id"])
        task = ath.create_issue(title="Task", project_id=proj["id"])

        # Hierarchy: nest the task under the epic, list it, then unnest it.
        ath.set_issue_parent(task["id"], epic["id"])
        assert [c["id"] for c in ath.list_subtasks(epic["id"])] == [task["id"]]
        assert ath.set_issue_parent(task["id"], None)["parent_id"] is None

        # Dependencies: the epic blocks the task; read it back, then remove it.
        linked = ath.link_issues(epic["id"], str(task["id"]), "blocks")
        assert [b["id"] for b in linked["blocks"]] == [task["id"]]
        assert [b["id"] for b in ath.list_issue_links(epic["id"])["blocks"]] == [
            task["id"]
        ]
        ath.unlink_issues(epic["id"], "blocks", task["id"])
        assert ath.list_issue_links(epic["id"])["blocks"] == []

        # Labels: create one in the shared vocabulary, attach + detach it.
        label = ath.create_label("bug", color="#ff0000")
        assert label["name"] == "bug" and label["id"] in [
            la["id"] for la in ath.list_labels()
        ]
        attached = ath.attach_label(task["id"], label["id"])
        assert label["id"] in [la["id"] for la in attached["labels"]]
        assert ath.detach_label(task["id"], label["id"])["labels"] == []

        # Sprints: create one in the project, put the task in it, list it, clear it.
        sprint = tc.post(f"/projects/{proj['id']}/sprints", json={"name": "S1"}).json()
        assert (
            ath.set_issue_sprint(task["id"], sprint["id"])["sprint_id"] == sprint["id"]
        )
        assert [s["id"] for s in ath.list_sprints(proj["id"])] == [sprint["id"]]
        assert ath.set_issue_sprint(task["id"], None)["sprint_id"] is None
    finally:
        tc.__exit__(None, None, None)


def test_sprint_lifecycle_through_mcp_is_audited_and_idempotent(tmp_path):
    # WHY: parity means the real agent path reaches the same audited commands as
    # REST, including retry coalescing and the complete lifecycle through delete.
    import asyncio
    import json

    from athena.mcp.server import build_server

    db_file = tmp_path / "mcp-sprint-lifecycle.db"
    tc, ath = _client(tmp_path, db_file.name)
    try:
        project = tc.post("/projects", json={"name": "Delivery", "key": "DEL"}).json()
        server = build_server(ath)
        create_args = {
            "project_id": project["id"],
            "name": "Cycle 1",
            "goal": "ship it",
            "start_date": "2030-01-02",
            "end_date": "2030-01-09",
            "idempotency_key": "create-cycle-1",
        }

        first = asyncio.run(server.call_tool("create_sprint", create_args))
        replay = asyncio.run(server.call_tool("create_sprint", create_args))
        assert replay == first
        sprint = json.loads(first[0].text)
        assert sprint["state"] == "planned"
        assert [row["id"] for row in ath.list_sprints(project["id"])] == [sprint["id"]]

        read = asyncio.run(server.call_tool("get_sprint", {"sprint_id": sprint["id"]}))
        assert json.loads(read[0].text)["name"] == "Cycle 1"

        updated = asyncio.run(
            server.call_tool(
                "update_sprint",
                {
                    "sprint_id": sprint["id"],
                    "name": "Cycle One",
                    "goal": "",
                    "clear_start_date": True,
                    "clear_end_date": True,
                    "idempotency_key": "edit-cycle-1",
                },
            )
        )
        updated_sprint = json.loads(updated[0].text)
        assert updated_sprint["name"] == "Cycle One"
        assert updated_sprint["goal"] == ""
        assert updated_sprint["start_date"] is None
        assert updated_sprint["end_date"] is None

        started = asyncio.run(
            server.call_tool(
                "start_sprint",
                {
                    "sprint_id": sprint["id"],
                    "idempotency_key": "start-cycle-1",
                },
            )
        )
        started_sprint = json.loads(started[0].text)
        assert started_sprint["state"] == "active"
        assert started_sprint["start_date"] is not None

        completed = asyncio.run(
            server.call_tool(
                "complete_sprint",
                {
                    "sprint_id": sprint["id"],
                    "idempotency_key": "complete-cycle-1",
                },
            )
        )
        completed_sprint = json.loads(completed[0].text)
        assert completed_sprint["state"] == "completed"
        assert completed_sprint["end_date"] is not None

        asyncio.run(
            server.call_tool(
                "delete_sprint",
                {
                    "sprint_id": sprint["id"],
                    "confirm_permanent": True,
                    "idempotency_key": "delete-cycle-1",
                },
            )
        )
        with pytest.raises(AthenaError) as missing:
            ath.get_sprint(sprint["id"])
        assert missing.value.status_code == 404

        conn = db.connect(db_file)
        try:
            rows = conn.execute(
                "SELECT actor_id, verb, target_kind, target_id "
                "FROM activity WHERE target_kind = 'sprint' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert [row["verb"] for row in rows] == [
            "sprint_created",
            "sprint_edited",
            "sprint_started",
            "sprint_completed",
            "sprint_deleted",
        ]
        assert {row["actor_id"] for row in rows} == {1}
        assert {row["target_id"] for row in rows} == {sprint["id"]}
    finally:
        tc.__exit__(None, None, None)


def test_mcp_sprint_conflicts_preserve_state_and_structured_errors(tmp_path):
    # WHY: retryable agents need exact 409 metadata, and rejected lifecycle/delete
    # writes must leave sprint state and issue membership untouched.
    import asyncio
    import json

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    tc, ath = _client(tmp_path, "mcp-sprint-conflicts.db")
    try:
        project = tc.post("/projects", json={"name": "Conflicts", "key": "CFL"}).json()
        server = build_server(ath)

        def create(name, key):
            result = asyncio.run(
                server.call_tool(
                    "create_sprint",
                    {
                        "project_id": project["id"],
                        "name": name,
                        "idempotency_key": key,
                    },
                )
            )
            return json.loads(result[0].text)

        active = create("Active", "create-active")
        waiting = create("Waiting", "create-waiting")
        asyncio.run(
            server.call_tool(
                "start_sprint",
                {"sprint_id": active["id"], "idempotency_key": "start-active"},
            )
        )

        with pytest.raises(ToolError) as start_conflict:
            asyncio.run(
                server.call_tool(
                    "start_sprint",
                    {
                        "sprint_id": waiting["id"],
                        "idempotency_key": "start-waiting",
                    },
                )
            )
        marker = "ATHENA_ERROR_JSON="
        start_payload = json.loads(str(start_conflict.value).split(marker, 1)[1])
        assert start_payload["status_code"] == 409
        assert start_payload["path"] == f"/sprints/{waiting['id']}/start"
        assert ath.get_sprint(waiting["id"])["state"] == "planned"

        issue = ath.create_issue(title="Still here", project_id=project["id"])
        ath.set_issue_sprint(issue["id"], waiting["id"])
        with pytest.raises(ToolError) as delete_conflict:
            asyncio.run(
                server.call_tool(
                    "delete_sprint",
                    {
                        "sprint_id": waiting["id"],
                        "confirm_permanent": True,
                        "idempotency_key": "delete-waiting",
                    },
                )
            )
        delete_payload = json.loads(str(delete_conflict.value).split(marker, 1)[1])
        assert delete_payload["status_code"] == 409
        assert delete_payload["path"] == f"/sprints/{waiting['id']}"
        assert ath.get_sprint(waiting["id"])["id"] == waiting["id"]
        assert ath.get_issue(str(issue["id"]))["sprint_id"] == waiting["id"]

        ath.set_issue_sprint(issue["id"], None)
        asyncio.run(
            server.call_tool(
                "delete_sprint",
                {
                    "sprint_id": waiting["id"],
                    "confirm_permanent": True,
                    "idempotency_key": "delete-waiting-empty",
                },
            )
        )
        assert [row["id"] for row in ath.list_sprints(project["id"])] == [active["id"]]
    finally:
        tc.__exit__(None, None, None)


def test_list_issues_mcp_sprint_filter_returns_actor_kinds(tmp_path):
    # WHY: the fleet board's actor lanes also need an agent-facing query. Exercise
    # FastMCP -> client -> REST and prove sprint filtering retains True/False/None.
    import asyncio
    import json

    from athena.mcp.server import build_server

    tc, ath = _client(tmp_path, "sprint-actor-kinds.db")
    try:
        human = tc.post("/users", json={"email": "human@e.com", "name": "Human"}).json()
        agent = tc.post(
            "/users",
            json={"email": "agent@e.com", "name": "Agent", "is_agent": True},
        ).json()
        project = tc.post("/projects", json={"name": "Fleet", "key": "FLT"}).json()
        sprint = tc.post(
            f"/projects/{project['id']}/sprints", json={"name": "Now"}
        ).json()

        expected = {}
        for title, assignee_id, actor_kind in (
            ("agent sprint work", agent["id"], True),
            ("human sprint work", human["id"], False),
            ("unassigned sprint work", None, None),
        ):
            issue = ath.create_issue(title=title, project_id=project["id"])
            if assignee_id is not None:
                ath.assign_issue(issue["id"], assignee_id)
            ath.set_issue_sprint(issue["id"], sprint["id"])
            expected[title] = actor_kind
        ath.create_issue(title="outside sprint", project_id=project["id"])

        result = asyncio.run(
            build_server(ath).call_tool("list_issues", {"sprint": sprint["id"]})
        )
        rows = [json.loads(content.text) for content in result]
        assert {
            issue["title"]: issue["assignee_is_agent"] for issue in rows
        } == expected
    finally:
        tc.__exit__(None, None, None)


def test_client_replays_one_logical_mutation_and_surfaces_mismatch(tmp_path):
    tc, ath = _client(tmp_path, "idempotent-client.db")
    try:
        key = "mcp-create-once"
        first = ath.create_issue(title="one logical write", idempotency_key=key)
        replay = ath.create_issue(title="one logical write", idempotency_key=key)

        assert replay == first
        assert [issue["id"] for issue in ath.list_issues()] == [first["id"]]
        assert [
            event["verb"] for event in ath.recent_events(kind="issue")["events"]
        ] == ["created"]

        with pytest.raises(AthenaError) as mismatch:
            ath.create_issue(title="different write", idempotency_key=key)
        error = mismatch.value
        assert error.status_code == 409
        assert error.method == "POST"
        assert error.path == "/issues"
        assert error.code == "idempotency_mismatch"
        assert error.retry_after is None
        assert error.detail == "Idempotency-Key reused for a different request"
        assert str(error) == (
            "POST /issues -> 409: Idempotency-Key reused for a different request"
        )

        with pytest.raises(AthenaError) as invalid:
            ath.create_issue(title="invalid key", idempotency_key="")
        assert invalid.value.status_code == 400
        assert invalid.value.code is None
        assert "1-255 visible ASCII" in invalid.value.detail
    finally:
        tc.__exit__(None, None, None)


def test_error_preserves_retry_after_and_machine_code():
    response = httpx.Response(
        409,
        json={
            "detail": "A request with this Idempotency-Key is still in progress",
            "code": "idempotency_in_progress",
        },
        headers={"Retry-After": "1", "ETag": '"current"'},
        request=httpx.Request("POST", "http://athena.test/issues"),
    )

    with pytest.raises(AthenaError) as exc:
        AthenaClient(client=object())._result(response)

    assert exc.value.status_code == 409
    assert exc.value.code == "idempotency_in_progress"
    assert exc.value.retry_after == "1"
    assert exc.value.current_etag == '"current"'


def test_error_surfaces_status_and_detail(tmp_path):
    tc, ath = _client(tmp_path, "err.db")
    try:
        with pytest.raises(AthenaError) as exc:
            ath.get_issue("99999")
        error = exc.value
        assert "404" in str(error)
        assert error.status_code == 404
        assert error.method == "GET"
        assert error.path == "/issues/99999"
        assert error.code is None
        assert error.retry_after is None
    finally:
        tc.__exit__(None, None, None)


# --- MCP wiring (requires the `mcp` extra; installed by the dev group, so these
# fail on an incomplete environment rather than skipping) -------------------------
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    MCP_MUTATION_CASES,
    ids=[case[0] for case in MCP_MUTATION_CASES],
)
def test_every_mcp_mutation_forwards_the_optional_key(tool_name, arguments):
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    for key in ("stable-key", None):
        tool_arguments = dict(arguments)
        if key is not None:
            tool_arguments["idempotency_key"] = key
        asyncio.run(server.call_tool(tool_name, tool_arguments))

        called_name, _, kwargs = client.calls.pop()
        assert called_name == tool_name
        assert kwargs["idempotency_key"] == key


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    MCP_IF_MATCH_CASES,
    ids=[case[0] for case in MCP_IF_MATCH_CASES],
)
def test_guarded_mcp_issue_mutations_forward_if_match(tool_name, arguments):
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    asyncio.run(server.call_tool(tool_name, {**arguments, "if_match": '"current"'}))

    called_name, _, kwargs = client.calls.pop()
    assert called_name == tool_name
    assert kwargs["if_match"] == '"current"'


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("claim_issue", {"issue_id": 7}),
        (
            "yield_claim",
            {
                "issue_id": 7,
                "generation": LEASE_GENERATION,
                "reason": "other",
                **HANDOFF_ARGUMENTS,
            },
        ),
        (
            "yield_claim",
            {
                "issue_id": 7,
                "generation": LEASE_GENERATION,
                "reason": "blocked",
                "note": "x" * 501,
                **HANDOFF_ARGUMENTS,
            },
        ),
    ],
)
def test_mcp_claim_and_yield_schema_rejects_before_dispatch(tool_name, arguments):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(tool_name, arguments))
    assert client.calls == []


@pytest.mark.parametrize(
    "invalid_key",
    ["", "contains space", "é", "x" * 256],
)
def test_mcp_schema_rejects_invalid_idempotency_keys_before_dispatch(invalid_key):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "create_issue",
                {"title": "never runs", "idempotency_key": invalid_key},
            )
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "invalid_run_id",
    [
        "",
        "   ",
        "line\nbreak",
        "trailing\n",
        "delete\x7fme",
        "lone-\ud800-surrogate",
        "bidi-\u202espoof",
        "zero-\u200bwidth",
        "x" * 201,
    ],
)
def test_mcp_schema_rejects_invalid_heartbeat_run_ids_before_dispatch(
    invalid_run_id,
):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "heartbeat_agent_run",
                {"run_id": invalid_run_id},
            )
        )
    assert client.calls == []


def test_mcp_error_text_preserves_structured_retry_metadata():
    import asyncio
    import json

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    server = build_server(_MCPFailingAthenaClient())
    with pytest.raises(ToolError) as exc:
        asyncio.run(
            server.call_tool(
                "create_issue",
                {"title": "x", "idempotency_key": "stable-key"},
            )
        )

    marker = "ATHENA_ERROR_JSON="
    assert marker in str(exc.value)
    payload = json.loads(str(exc.value).split(marker, 1)[1])
    assert payload["status_code"] == 409
    assert payload["code"] == "idempotency_in_progress"
    assert payload["retry_after"] == "7"
    assert payload["current_etag"] == '"current"'
    assert payload["message"] == "POST /issues -> 409: still running"


def test_mcp_read_error_text_preserves_structured_retry_metadata():
    import asyncio
    import json

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    server = build_server(_MCPFailingReadAthenaClient())
    with pytest.raises(ToolError) as exc:
        asyncio.run(server.call_tool("get_issue", {"ref": "ATH-7"}))

    marker = "ATHENA_ERROR_JSON="
    assert marker in str(exc.value)
    payload = json.loads(str(exc.value).split(marker, 1)[1])
    assert payload["status_code"] == 429
    assert payload["code"] == "rate_limited"
    assert payload["retry_after"] == "11"
    assert payload["current_etag"] == '"fresh"'
    assert payload["message"] == "GET /issues/ATH-7 -> 429: slow down"


def test_work_context_mcp_tool_forwards_only_the_issue_ref():
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    asyncio.run(server.call_tool("get_issue_work_context", {"ref": "ATH-7"}))

    assert client.calls == [("get_issue_work_context", ("ATH-7",), {})]


@pytest.mark.parametrize(
    "sprint",
    [None, 0, 7, issues.MAX_SQLITE_INTEGER],
)
def test_list_issues_mcp_tool_forwards_strict_sprint_ids(sprint):
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    asyncio.run(server.call_tool("list_issues", {"sprint": sprint}))

    called_name, args, kwargs = client.calls.pop()
    assert called_name == "list_issues"
    assert args == ()
    assert kwargs["sprint"] == sprint


@pytest.mark.parametrize(
    "invalid_sprint",
    [
        True,
        False,
        "7",
        7.0,
        -1,
        issues.MAX_SQLITE_INTEGER + 1,
    ],
)
def test_list_issues_mcp_rejects_invalid_sprint_before_dispatch(invalid_sprint):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "list_issues",
                {"sprint": invalid_sprint},
            )
        )
    assert client.calls == []


@pytest.mark.parametrize(
    ("tool_name", "id_field", "base_arguments"),
    MCP_SPRINT_RESOURCE_CASES,
    ids=[case[0] for case in MCP_SPRINT_RESOURCE_CASES],
)
@pytest.mark.parametrize("resource_id", [1, issues.MAX_SQLITE_INTEGER])
def test_mcp_sprint_resource_ids_accept_only_positive_sqlite_boundaries(
    tool_name, id_field, base_arguments, resource_id
):
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)
    arguments = {**base_arguments, id_field: resource_id}

    asyncio.run(server.call_tool(tool_name, arguments))

    called_name, called_args, _ = client.calls.pop()
    assert called_name == tool_name
    position = -1 if tool_name == "set_issue_sprint" else 0
    assert called_args[position] == resource_id


@pytest.mark.parametrize(
    ("tool_name", "id_field", "base_arguments"),
    MCP_SPRINT_RESOURCE_CASES,
    ids=[case[0] for case in MCP_SPRINT_RESOURCE_CASES],
)
@pytest.mark.parametrize(
    "invalid_resource_id",
    [
        True,
        False,
        "7",
        7.0,
        0,
        -1,
        issues.MAX_SQLITE_INTEGER + 1,
    ],
)
def test_mcp_sprint_resource_ids_reject_coercion_before_dispatch(
    tool_name, id_field, base_arguments, invalid_resource_id
):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)
    arguments = {**base_arguments, id_field: invalid_resource_id}

    with pytest.raises(ToolError):
        asyncio.run(server.call_tool(tool_name, arguments))
    assert client.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"sprint_id": 7},
        {"sprint_id": 7, "confirm_permanent": False},
        {"sprint_id": 7, "confirm_permanent": 1},
        {"sprint_id": 7, "confirm_permanent": "true"},
    ],
)
def test_mcp_delete_sprint_requires_strict_explicit_confirmation(arguments):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("delete_sprint", arguments))
    assert client.calls == []


def test_create_automation_rule_mcp_tool_forwards_schedule_contract():
    import asyncio

    from athena.mcp.server import build_server

    client = _MCPRecordingAthenaClient()
    server = build_server(client)

    arguments = {
        "name": "stale work",
        "trigger_verb": "scheduled",
        "action_type": "comment",
        "conditions": {"inactive_for_seconds": 3600},
        "action_params": {"body": "check in"},
        "trigger_type": "schedule",
        "schedule_at": "2030-01-02T03:04:05Z",
        "schedule_every_seconds": 3600,
        "idempotency_key": "scheduled-rule",
    }
    asyncio.run(
        server.call_tool(
            "create_automation_rule",
            arguments,
        )
    )

    assert client.calls == [
        (
            "create_automation_rule",
            (),
            {
                "name": "stale work",
                "trigger_verb": "scheduled",
                "action_type": "comment",
                "conditions": {"inactive_for_seconds": 3600},
                "action_params": {"body": "check in"},
                "target_kind": "issue",
                "trigger_type": "schedule",
                "schedule_at": "2030-01-02T03:04:05Z",
                "schedule_every_seconds": 3600,
                "idempotency_key": "scheduled-rule",
            },
        )
    ]


def test_mcp_server_registers_tools_and_calls_through(tmp_path):
    import asyncio

    from athena.mcp.server import build_server

    tc, ath = _client(tmp_path, "mcp.db")
    try:
        server = build_server(ath)
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        names = set(tools)
        # The agent-facing surface is present.
        assert {
            "search",
            "list_issues",
            "get_issue",
            "get_issue_work_context",
            "create_issue",
            "update_issue",
            "set_issue_placement",
            "get_issue_state",
            "assign_issue",
            "delegate_issue",
            "get_issue_lease",
            "claim_issue",
            "yield_claim",
            "resume_claim_handoff",
            "complete_claim",
            "decline_delegation",
            "comment_on_issue",
            "archive_issue",
            "unarchive_issue",
            "bulk_update_issues",
            "recent_events",
            "heartbeat_agent_run",
            "list_activity_runs",
            "get_run_lineage",
            "get_run_fork_contract",
            "list_projects",
            "get_agent_run_health",
            "list_automation_rules",
            "get_automation_rule",
            "create_automation_rule",
            "set_automation_rule_enabled",
            "delete_automation_rule",
            "list_automation_failures",
            "list_users",
            "list_spaces",
            "list_pages",
            "get_page",
            "find_pages_by_title",
            "create_page",
            "update_page",
            # The newly-added organize-an-issue surface.
            "set_issue_parent",
            "list_subtasks",
            "list_issue_links",
            "link_issues",
            "unlink_issues",
            "list_sprints",
            "get_sprint",
            "create_sprint",
            "update_sprint",
            "start_sprint",
            "complete_sprint",
            "delete_sprint",
            "set_issue_sprint",
            "list_labels",
            "create_label",
            "attach_label",
            "detach_label",
        } <= names

        dispatch_schema = tools["dispatch_to_icarus"].inputSchema
        dispatch_issue_id = dispatch_schema["properties"]["issue_id"]
        assert dispatch_issue_id["minimum"] == 1
        assert dispatch_issue_id["maximum"] == dispatch.MAX_SQLITE_INTEGER

        dispatch_list_schema = tools["list_dispatches"].inputSchema
        dispatch_work_item = next(
            option
            for option in dispatch_list_schema["properties"]["work_item_id"]["anyOf"]
            if option["type"] == "integer"
        )
        assert dispatch_work_item["minimum"] == 1
        assert dispatch_work_item["maximum"] == dispatch.MAX_SQLITE_INTEGER
        dispatch_limit = dispatch_list_schema["properties"]["limit"]
        assert dispatch_limit["minimum"] == 1
        assert dispatch_limit["maximum"] == dispatch.MAX_LIST_LIMIT

        # Placement's two nullable values are required in the MCP contract. This
        # keeps omission distinct from an explicit backlog/no-sprint placement.
        placement_schema = tools["set_issue_placement"].inputSchema
        assert {"issue_id", "project_id", "sprint_id"} <= set(
            placement_schema["required"]
        )
        for field_name in ("project_id", "sprint_id"):
            types = {
                option["type"]
                for option in placement_schema["properties"][field_name]["anyOf"]
            }
            assert types == {"integer", "null"}

        list_schema = tools["list_issues"].inputSchema
        assert "sprint" in list_schema["properties"]
        assert "sprint" not in set(list_schema.get("required", []))
        sprint_types = {
            option["type"] for option in list_schema["properties"]["sprint"]["anyOf"]
        }
        assert sprint_types == {"integer", "null"}
        sprint_integer = next(
            option
            for option in list_schema["properties"]["sprint"]["anyOf"]
            if option["type"] == "integer"
        )
        assert sprint_integer["minimum"] == 0
        assert sprint_integer["maximum"] == issues.MAX_SQLITE_INTEGER

        create_sprint_schema = tools["create_sprint"].inputSchema
        assert {"project_id", "name"} <= set(create_sprint_schema["required"])
        update_sprint_schema = tools["update_sprint"].inputSchema
        assert {"clear_start_date", "clear_end_date"} <= set(
            update_sprint_schema["properties"]
        )
        assert "state" not in update_sprint_schema["properties"]

        for tool_name, field_name in (
            ("list_sprints", "project_id"),
            ("get_sprint", "sprint_id"),
            ("delete_sprint", "sprint_id"),
        ):
            identifier = tools[tool_name].inputSchema["properties"][field_name]
            assert identifier["minimum"] == 1
            assert identifier["maximum"] == issues.MAX_SQLITE_INTEGER

        delete_sprint_schema = tools["delete_sprint"].inputSchema
        assert {"sprint_id", "confirm_permanent"} <= set(
            delete_sprint_schema["required"]
        )
        assert delete_sprint_schema["properties"]["confirm_permanent"]["type"] == (
            "boolean"
        )
        assert MUTATION_TOOL_NAMES <= names

        work_context_tool = tools["get_issue_work_context"]
        assert work_context_tool.inputSchema["required"] == ["ref"]
        assert set(work_context_tool.inputSchema["properties"]) == {"ref"}
        work_context_description = work_context_tool.description.lower()
        for contract_term in (
            "bounded",
            "current",
            "visible issue",
            "visible supporting docs",
            "claim",
            "lease",
            "readiness",
            "unblocked",
            "liveness",
            "replayability",
        ):
            assert contract_term in work_context_description

        heartbeat_schema = tools["heartbeat_agent_run"].inputSchema
        assert heartbeat_schema["required"] == ["run_id"]
        assert set(heartbeat_schema["properties"]) == {"run_id"}
        run_id_schema = heartbeat_schema["properties"]["run_id"]
        assert run_id_schema["minLength"] == 1
        assert run_id_schema["maxLength"] == 200
        assert run_id_schema["pattern"] == (
            r"^[^\x00-\x1F\x7F]*"
            r"[^\s\x00-\x1F\x7F]"
            r"[^\x00-\x1F\x7F]*$"
        )
        automation_schema = tools["create_automation_rule"].inputSchema
        assert {
            "name",
            "trigger_verb",
            "action_type",
        } <= set(automation_schema["required"])
        assert {
            "trigger_type",
            "schedule_at",
            "schedule_every_seconds",
        } <= set(automation_schema["properties"])
        assert set(automation_schema["properties"]["trigger_type"]["enum"]) == {
            "event",
            "schedule",
        }
        schedule_at_schema = next(
            option
            for option in automation_schema["properties"]["schedule_at"]["anyOf"]
            if option["type"] == "string"
        )
        assert (
            schedule_at_schema["pattern"] == r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )
        interval_schema = next(
            option
            for option in automation_schema["properties"]["schedule_every_seconds"][
                "anyOf"
            ]
            if option["type"] == "integer"
        )
        assert interval_schema["minimum"] == automation.MIN_SCHEDULE_INTERVAL_SECONDS
        assert interval_schema["maximum"] == automation.MAX_SCHEDULE_INTERVAL_SECONDS
        for tool_name in MUTATION_TOOL_NAMES:
            schema = tools[tool_name].inputSchema
            assert "idempotency_key" in schema["properties"]
            assert "idempotency_key" not in set(schema.get("required", []))
            key_types = {
                option["type"]
                for option in schema["properties"]["idempotency_key"]["anyOf"]
            }
            assert key_types == {"string", "null"}
            string_schema = next(
                option
                for option in schema["properties"]["idempotency_key"]["anyOf"]
                if option["type"] == "string"
            )
            assert string_schema["minLength"] == 1
            assert string_schema["maxLength"] == 255
            assert string_schema["pattern"] == r"^[\x21-\x7E]+$"

        for tool_name in names - MUTATION_TOOL_NAMES:
            assert "idempotency_key" not in tools[tool_name].inputSchema["properties"]

        for tool_name in OPTIONAL_IF_MATCH_TOOL_NAMES:
            schema = tools[tool_name].inputSchema
            assert "if_match" in schema["properties"]
            assert "if_match" not in set(schema.get("required", []))
        for tool_name in REQUIRED_IF_MATCH_TOOL_NAMES:
            schema = tools[tool_name].inputSchema
            assert "if_match" in schema["properties"]
            assert "if_match" in set(schema.get("required", []))
        for tool_name in names - IF_MATCH_TOOL_NAMES:
            assert "if_match" not in tools[tool_name].inputSchema["properties"]

        yield_schema = tools["yield_claim"].inputSchema
        assert {
            "issue_id",
            "generation",
            "reason",
            "attempted_work",
            "evidence",
            "blocking_question",
            "resume_instructions",
        } <= set(yield_schema["required"])
        assert "note" not in set(yield_schema["required"])
        assert set(yield_schema["properties"]["reason"]["enum"]) == {
            "needs_input",
            "blocked",
            "capacity",
        }
        note_string = next(
            option
            for option in yield_schema["properties"]["note"]["anyOf"]
            if option["type"] == "string"
        )
        assert note_string["maxLength"] == 500
        assert yield_schema["properties"]["evidence"]["maxItems"] == 10
        assert yield_schema["properties"]["evidence"]["items"]["maxLength"] == 1000
        resume_schema = tools["resume_claim_handoff"].inputSchema
        assert {
            "issue_id",
            "handoff_token",
            "generation",
        } <= set(resume_schema["required"])

        # Read-only operator tools are wired through FastMCP, not merely client helpers.
        asyncio.run(server.call_tool("get_agent_run_health", {}))
        asyncio.run(server.call_tool("list_automation_failures", {}))

        # An omitted key remains backward-compatible and creates real data.
        asyncio.run(server.call_tool("create_issue", {"title": "via mcp"}))
        assert any(i["title"] == "via mcp" for i in ath.list_issues())

        # A caller-supplied key survives separate MCP invocations and coalesces
        # them into one logical REST mutation.
        keyed_args = {
            "title": "via mcp once",
            "idempotency_key": "mcp-create-once",
        }
        first = asyncio.run(server.call_tool("create_issue", keyed_args))
        replay = asyncio.run(server.call_tool("create_issue", keyed_args))
        assert replay == first
        assert [i["title"] for i in ath.list_issues()].count("via mcp once") == 1
        import json

        from mcp.server.fastmcp.exceptions import ToolError

        created_events = len(ath.recent_events(kind="issue")["events"])
        with pytest.raises(ToolError) as mismatch:
            asyncio.run(
                server.call_tool(
                    "create_issue",
                    {
                        "title": "different via mcp",
                        "idempotency_key": "mcp-create-once",
                    },
                )
            )
        marker = "ATHENA_ERROR_JSON="
        payload = json.loads(str(mismatch.value).split(marker, 1)[1])
        assert payload["code"] == "idempotency_mismatch"
        assert [i["title"] for i in ath.list_issues()].count("via mcp once") == 1
        assert len(ath.recent_events(kind="issue")["events"]) == created_events

        # The real tool traverses FastMCP -> AthenaClient -> REST -> command -> DB.
        # Mint the agent's token through the trusted bootstrap header, then replace
        # only this TestClient's default Authorization while the heartbeat runs.
        agent = tc.post(
            "/users",
            json={"email": "heartbeat@e.com", "name": "Heartbeat", "is_agent": True},
        ).json()
        agent_token = tc.post(
            "/tokens",
            json={"name": "heartbeat", "scopes": ["issue:write"]},
            headers={
                "Authorization": "not-bearer",
                "X-Athena-Actor": str(agent["id"]),
            },
        ).json()["token"]
        admin_authorization = tc.headers["Authorization"]
        tc.headers["Authorization"] = f"Bearer {agent_token}"
        try:
            heartbeat = asyncio.run(
                server.call_tool("heartbeat_agent_run", {"run_id": "mcp-run"})
            )
        finally:
            tc.headers["Authorization"] = admin_authorization
        assert heartbeat[0].text
        mcp_health_result = asyncio.run(
            server.call_tool("get_agent_run_health", {"agent_id": agent["id"]})
        )
        mcp_health = json.loads(mcp_health_result[0].text)
        assert mcp_health["latest_checkins"][0]["run_id"] == "mcp-run"
        assert mcp_health["totals"]["latest_reporting_recently_count"] == 1
        health = ath.get_agent_run_health(agent_id=agent["id"])
        assert health["checkins"][0]["run_id"] == "mcp-run"
        assert health["checkins"][0]["agent_id"] == agent["id"]
        assert health["latest_checkins"][0]["run_id"] == "mcp-run"
        assert health["totals"]["latest_reporting_recently_count"] == 1
    finally:
        tc.__exit__(None, None, None)


def test_mcp_guarded_claim_and_yield_reach_shared_command(tmp_path):
    import asyncio
    import json

    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    tc, ath = _client(tmp_path, "mcp-claim-yield.db")
    try:
        server = build_server(ath)
        issue = ath.create_issue(title="MCP guarded claim")
        reviewed = ath.get_issue(str(issue["id"]))

        ath.set_run("mcp-yield-run")
        asyncio.run(
            server.call_tool(
                "claim_issue",
                {"issue_id": issue["id"], "if_match": reviewed["_etag"]},
            )
        )
        lease = ath.get_issue_lease(issue["id"])
        assert lease["holder_id"] == 1

        asyncio.run(
            server.call_tool(
                "yield_claim",
                {
                    "issue_id": issue["id"],
                    "generation": lease["generation"],
                    "reason": "needs_input",
                    "note": "operator decision",
                    **HANDOFF_ARGUMENTS,
                },
            )
        )
        assert ath.get_issue_lease(issue["id"]) is None
        yielded = [
            event
            for event in ath.recent_events(kind="issue")["events"]
            if event["verb"] == "claim_yielded"
        ]
        assert len(yielded) == 1
        assert yielded[0]["run_id"] == "mcp-yield-run"

        open_handoff = ath.get_issue_work_context(str(issue["id"]))["claim_handoffs"][
            "open"
        ]
        asyncio.run(
            server.call_tool(
                "claim_issue",
                {"issue_id": issue["id"], "if_match": reviewed["_etag"]},
            )
        )
        replacement = ath.get_issue_lease(issue["id"])
        assert (
            replacement["open_claim_handoff"]["handoff_token"]
            == (open_handoff["handoff_token"])
        )
        asyncio.run(
            server.call_tool(
                "resume_claim_handoff",
                {
                    "issue_id": issue["id"],
                    "handoff_token": open_handoff["handoff_token"],
                    "generation": replacement["generation"],
                    "resume_note": "context received",
                },
            )
        )
        assert (
            ath.get_issue_work_context(str(issue["id"]))["claim_handoffs"]["open"]
            is None
        )

        stale_issue = ath.create_issue(title="MCP stale claim")
        stale = ath.get_issue(str(stale_issue["id"]))
        ath.update_issue(
            stale_issue["id"],
            title="changed",
            if_match=stale["_etag"],
        )
        current = ath.get_issue(str(stale_issue["id"]))
        with pytest.raises(ToolError) as rejected:
            asyncio.run(
                server.call_tool(
                    "claim_issue",
                    {
                        "issue_id": stale_issue["id"],
                        "if_match": stale["_etag"],
                    },
                )
            )
        payload = json.loads(str(rejected.value).split("ATHENA_ERROR_JSON=", 1)[1])
        assert payload["status_code"] == 412
        assert payload["code"] == "precondition_failed"
        assert payload["current_etag"] == current["_etag"]
    finally:
        tc.__exit__(None, None, None)
