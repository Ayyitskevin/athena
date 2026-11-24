"""Every refusal names a registered code, and MCP hands it over as one object (A5).

WHY: an agent decides what to do next from a refusal. Before A5 at least three
refusal body shapes coexisted and 401, 403, 404 and most 409 bodies carried no
code at all, so an agent had to read prose ("issue is claimed by ...") to tell a
held claim from a stale ETag. MCP turned every refusal into a sentence of text,
and a 202 approval ask arrived as an ordinary success with nothing saying that
the write was not applied.

The contract pinned here:

* every 4xx and 5xx JSON body from a REST route carries a top-level ``code``
  from ``core/refusal_codes.py``, and nothing else in it changes: the status,
  ``detail`` and extra fields are what they were before the code existed;
* the registry is well formed: each code has statuses and a meaning, and every
  ``next`` names a tool a ``seat`` session carries;
* the static check (``scripts/check_refusal_codes.py``) passes on the source and
  rejects each kind of violation it exists to catch;
* over MCP a refused call is one JSON object with ``isError`` true, and a 202
  ``approval_required`` comes back as a non-error with ``applied: false``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from athena.aegis import issues as aegis_issues
from athena.core import approvals, refusal_codes
from athena.core.refusal_codes import DEFAULT_CODE_BY_STATUS, REFUSAL_CODES
from athena.main import create_app
from athena.mcp import client as mcp_client
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

REPO = Path(__file__).resolve().parents[1]
H1 = {"X-Athena-Actor": "1"}


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_refusal_codes", REPO / "scripts" / "check_refusal_codes.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["check_refusal_codes"] = module
    spec.loader.exec_module(module)
    return module


def _assert_coded(response, status: int, code: str | None = None) -> dict:
    """The response is a JSON refusal with this status and a registered code that
    the registry says this status may carry. Returns the body."""
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body, dict), body
    assert body.get("code") in REFUSAL_CODES, body
    assert status in REFUSAL_CODES[body["code"]].statuses, (status, body)
    if code is not None:
        assert body["code"] == code, body
    return body


# --- the registry -------------------------------------------------------------


def test_every_registered_code_has_statuses_and_a_one_line_meaning():
    assert len(REFUSAL_CODES) >= 20
    for code, entry in REFUSAL_CODES.items():
        assert entry.code == code
        assert entry.statuses, code
        assert all(100 <= status <= 599 for status in entry.statuses), code
        assert entry.meaning.strip() and "\n" not in entry.meaning, code
    # Only the approval ask is not an error status, and it is the one 2xx.
    non_errors = sorted(
        code
        for code, entry in REFUSAL_CODES.items()
        if any(status < 400 for status in entry.statuses)
    )
    assert non_errors == [approvals.APPROVAL_REQUIRED_CODE]


def test_every_next_names_a_tool_a_seat_session_carries():
    # Each `next` is reachable from a seat profile: a refusal must never send a
    # seat to a tool its session does not have (the lesson of A8).
    named = {
        code: entry.next.tool
        for code, entry in REFUSAL_CODES.items()
        if entry.next is not None
    }
    assert named, "no code names a next call; the registry lost its hints"
    for code, tool in named.items():
        assert tool in mcp_server.TOOL_SCOPES, (code, tool)
        assert tool in mcp_server.SEAT_PROFILE, (code, tool)
        assert REFUSAL_CODES[code].next.hint.strip(), code


def test_the_fallback_codes_are_registered_for_their_status():
    for status, code in DEFAULT_CODE_BY_STATUS.items():
        assert status in REFUSAL_CODES[code].statuses, (status, code)
        assert refusal_codes.code_for_status(status) == code


def test_codes_already_on_the_wire_are_kept_and_registered():
    # These existed before the registry: an agent may already branch on them.
    from athena.aegis import desk_commands, issue_commands
    from athena.core import budgets, undo

    existing = {
        approvals.APPROVAL_REQUIRED_CODE,
        approvals.APPROVAL_REJECTED_CODE,
        budgets.BUDGET_EXHAUSTED_CODE,
        desk_commands.CURSOR_AHEAD_CODE,
        issue_commands.BLOCKED_CLOSE_POLICY_ERROR_CODE,
        undo.NOT_FOUND_CODE,
        undo.NOT_REVERSIBLE_CODE,
        undo.ALREADY_REVERSED_CODE,
        undo.IMPORTED_CODE,
        undo.NO_EFFECT_CODE,
        undo.COMMAND_REFUSED_CODE,
        "idempotency_mismatch",
        "idempotency_authorization_changed",
        "idempotency_indeterminate",
        "idempotency_in_progress",
        "precondition_required",
        "invalid_if_match",
        "if_match_too_large",
        "precondition_failed",
        "lease_generation_required",
        "invalid_lease_generation",
        "lease_generation_mismatch",
        "invalid_query",
    }
    assert existing <= set(REFUSAL_CODES)


def test_the_mcp_client_names_the_approval_code_the_server_answers_with():
    assert mcp_client._APPROVAL_REQUIRED == approvals.APPROVAL_REQUIRED_CODE


# --- the static check -----------------------------------------------------------


def test_the_static_check_passes_on_the_source():
    checker = _load_checker()
    assert checker.check() == []


def test_the_static_check_reads_the_whole_registry():
    checker = _load_checker()
    parsed = checker.registered_codes(checker.REGISTRY_PATH.read_text())
    assert parsed == set(REFUSAL_CODES)


_NEGATIVE_FIXTURES = {
    "bare HTTPException": (
        "from fastapi import HTTPException\n"
        "def route():\n"
        "    raise HTTPException(status_code=404, detail='gone')\n",
        "HTTPException(...) carries no refusal code",
    ),
    "Refusal without code": (
        "def route():\n    raise Refusal(status_code=404, detail='gone')\n",
        "Refusal(...) without code=",
    ),
    "unregistered literal code": (
        "def route():\n"
        "    raise Refusal(status_code=404, detail='gone', code='vanished')\n",
        "names 'vanished'",
    ),
    "unregistered fallback inside a code expression": (
        "def route(exc):\n"
        "    raise Refusal(400, exc.detail, code=CODE_BY_KIND.get(exc.kind, 'oops'))\n",
        "names 'oops'",
    ),
    "status table with no code table": (
        "STATUS_BY_KIND = {'not_found': 404, 'invalid': 422}\n",
        "has no code table CODE_BY_KIND",
    ),
    "kind with no code": (
        "STATUS_BY_KIND = {'not_found': 404, 'invalid': 422}\n"
        "CODE_BY_KIND = {'not_found': 'not_found'}\n",
        "kind 'invalid' in STATUS_BY_KIND has no code",
    ),
    "code table with an unregistered code": (
        "STATUS_BY_KIND = {'not_found': 404}\nCODE_BY_KIND = {'not_found': 'nope'}\n",
        "names 'nope'",
    ),
    "inline kind mapping": (
        "def route(exc):\n    return {'forbidden': 403, 'conflict': 409}[exc.kind]\n",
        "must be a named *STATUS* table",
    ),
    "unregistered (status, code) pair": (
        "SPEC = {'precondition_failed': (412, 'stale_thing')}\n",
        "names 'stale_thing'",
    ),
    "JSON body without a code": (
        "def handler():\n"
        "    return JSONResponse(status_code=403, content={'detail': 'no'})\n",
        'body has "detail" but no "code"',
    ),
    "middleware body without a code": (
        "async def deny(send):\n"
        "    await _send_json_response(send, {'detail': 'no'}, status_code=400)\n",
        'body has "detail" but no "code"',
    ),
    "unregistered code constant": (
        "SOMETHING_CODE = 'made_up'\n",
        "names 'made_up'",
    ),
    "unregistered code on a command error": (
        "def command():\n    raise IssueCommandError('conflict', 'x', code='mine')\n",
        "names 'mine'",
    ),
}


@pytest.mark.parametrize("name", sorted(_NEGATIVE_FIXTURES))
def test_the_static_check_rejects(name):
    checker = _load_checker()
    codes = checker.registered_codes(checker.REGISTRY_PATH.read_text())
    source, expected = _NEGATIVE_FIXTURES[name]
    found = checker.check_source(source, "fixture.py", codes)
    assert any(expected in str(v) for v in found), [str(v) for v in found]


def test_the_static_check_accepts_the_coded_forms():
    checker = _load_checker()
    codes = checker.registered_codes(checker.REGISTRY_PATH.read_text())
    source = (
        "STATUS_BY_KIND = {'not_found': 404, 'invalid': 422}\n"
        "CODE_BY_KIND = {'not_found': 'not_found', 'invalid': 'validation_error'}\n"
        "SPEC = {'precondition_failed': (412, 'precondition_failed')}\n"
        "def route(exc):\n"
        "    raise Refusal(\n"
        "        STATUS_BY_KIND[exc.kind], exc.detail, code=CODE_BY_KIND[exc.kind]\n"
        "    )\n"
        "def handler():\n"
        "    return JSONResponse(\n"
        "        status_code=403, content={'detail': 'no', 'code': 'forbidden'}\n"
        "    )\n"
    )
    assert checker.check_source(source, "fixture.py", codes) == []


def test_browser_and_mcp_modules_are_held_only_to_the_refusal_rules():
    checker = _load_checker()
    codes = checker.registered_codes(checker.REGISTRY_PATH.read_text())
    html_table = "STATUS = {'invalid': 400}\n"
    assert checker.check_source(html_table, "web.py", codes, rest=False) == []
    bare = "def f():\n    raise HTTPException(403, 'no')\n"
    assert checker.check_source(bare, "web.py", codes, rest=False)


def test_the_checker_runs_alone():
    import subprocess

    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_refusal_codes.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- the sweep: real refusals over the real app -------------------------------


@pytest.fixture()
def world(tmp_path):
    """An admin (user 1), an issue:write agent, a read-only agent, a viewer, one
    project with one issue assigned to the agent that the admin holds."""
    with TestClient(create_app(tmp_path / "refusals.db")) as client:
        client.post(
            "/users", json={"email": "a@e.com", "name": "Ann", "password": "secret"}
        )

        def onboard(email, name, scopes):
            made = client.post(
                "/users/onboard_agent",
                json={"email": email, "name": name, "scopes": scopes},
                headers=H1,
            )
            assert made.status_code == 201, made.text
            body = made.json()
            return body["user"]["id"], {
                "Authorization": f"Bearer {body['token']['token']}"
            }

        writer_id, writer = onboard("sol@e.com", "Sol", ["read", "issue:write"])
        reader_id, reader = onboard("ro@e.com", "Ro", ["read"])
        viewer = client.post(
            "/users",
            json={
                "email": "v@e.com",
                "name": "Vee",
                "password": "secret",
                "role": "viewer",
            },
            headers=H1,
        ).json()
        project = client.post(
            "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
        ).json()
        # Assigned to the writer, so the writer is an eligible claimant and the
        # admin's lease is the only thing standing between them.
        issue = client.post(
            "/issues", json={"title": "held", "project_id": project["id"]}, headers=H1
        ).json()
        assigned = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": writer_id},
            headers=H1,
        )
        assert assigned.status_code == 200, assigned.text
        etag = assigned.headers["ETag"]
        claimed = client.post(
            f"/issues/{issue['id']}/claim",
            json={},
            headers={**H1, "If-Match": etag},
        )
        assert claimed.status_code == 201, claimed.text
        yield {
            "client": client,
            "issue": issue,
            "project": project,
            "writer": writer,
            "writer_id": writer_id,
            "reader": reader,
            "reader_id": reader_id,
            "viewer": {"X-Athena-Actor": str(viewer["id"])},
        }


def test_framework_refusals_carry_codes(world):
    client = world["client"]
    missing = _assert_coded(client.get("/no/such/route"), 404, "not_found")
    assert missing == {"detail": "Not Found", "code": "not_found"}
    wrong = _assert_coded(client.delete("/healthz"), 405, "method_not_allowed")
    assert wrong == {"detail": "Method Not Allowed", "code": "method_not_allowed"}

    invalid = _assert_coded(
        client.post("/issues", json={}, headers=H1), 422, "validation_error"
    )
    # FastAPI's own validation list, unchanged, beside the code.
    assert set(invalid) == {"detail", "code"}
    assert [error["loc"] for error in invalid["detail"]] == [["body", "title"]]


def test_auth_refusals_carry_codes(world):
    client = world["client"]
    no_credential = _assert_coded(
        client.post("/issues", json={"title": "x"}), 401, "unauthenticated"
    )
    assert no_credential["detail"] == "authentication required"
    bad_token = _assert_coded(
        client.post(
            "/issues",
            json={"title": "x"},
            headers={"Authorization": "Bearer ath_nope"},
        ),
        401,
        "unauthenticated",
    )
    assert bad_token["detail"] == "invalid or revoked token"

    scope = _assert_coded(
        client.post("/issues", json={"title": "x"}, headers=world["reader"]),
        403,
        "scope_denied",
    )
    assert scope == {
        "detail": "token scope required: issue:write",
        "code": "scope_denied",
    }
    viewer = _assert_coded(
        client.post("/issues", json={"title": "x"}, headers=world["viewer"]),
        403,
        "forbidden",
    )
    assert viewer == {"detail": "viewer role is read-only", "code": "forbidden"}
    admin_only = _assert_coded(
        client.get("/approvals", headers=world["writer"]), 403, "forbidden"
    )
    assert admin_only == {"detail": "admin role required", "code": "forbidden"}

    paused = client.put(
        f"/users/{world['writer_id']}/paused", json={"paused": True}, headers=H1
    )
    assert paused.status_code == 200, paused.text
    frozen = _assert_coded(
        client.get("/issues", headers=world["writer"]), 403, "account_paused"
    )
    assert frozen == {"detail": "account is paused", "code": "account_paused"}


def test_lease_and_etag_refusals_carry_codes(world):
    client, issue = world["client"], world["issue"]
    path = f"/issues/{issue['id']}"
    etag = client.get(path, headers=H1).headers["ETag"]

    held = _assert_coded(
        client.post(
            f"{path}/claim", json={}, headers={**world["writer"], "If-Match": etag}
        ),
        409,
        "claim_held",
    )
    assert held["detail"].startswith("issue is claimed by Ann until ")
    # The holder and the expiry the detail names are fields too (F11).
    # A refusal keeps the packet conventions (A13): the expiry in the one
    # shape, its seconds beside it.
    assert set(held) == {
        "detail",
        "code",
        "holder",
        "expires_at",
        "expires_in_seconds",
        "events_after",
    }
    assert held["holder"] == {"id": 1, "name": "Ann"}
    assert held["detail"].endswith(held["expires_at"])
    assert held["expires_at"].endswith("Z") and "T" in held["expires_at"]
    assert 0 < held["expires_in_seconds"] <= 86400

    not_yours = _assert_coded(
        client.post(f"{path}/complete", json={}, headers=world["writer"]),
        409,
        "claim_held",
    )
    assert not_yours["detail"] == "issue is claimed by Ann, not you"

    free = client.post("/issues", json={"title": "free"}, headers=H1).json()
    required = _assert_coded(
        client.post(f"/issues/{free['id']}/claim", json={}, headers=H1),
        428,
        "precondition_required",
    )
    assert set(required) == {"detail", "code"}

    stale = _assert_coded(
        client.patch(path, json={"title": "y"}, headers={**H1, "If-Match": '"stale"'}),
        412,
        "precondition_failed",
    )
    assert set(stale) == {"detail", "code"}

    renewal = _assert_coded(
        client.post(f"{path}/claim", json={}, headers={**H1, "If-Match": etag}),
        428,
        "lease_generation_required",
    )
    assert renewal["detail"].startswith("you already hold this issue's lease")


def test_command_kind_refusals_from_each_module_carry_codes(world):
    client, issue = world["client"], world["issue"]

    # aegis: the issue command's `invalid` kind
    bad_status = _assert_coded(
        client.patch(f"/issues/{issue['id']}", json={"status": "bogus"}, headers=H1),
        422,
        "validation_error",
    )
    assert set(bad_status) == {"detail", "code", "valid_statuses"}
    assert bad_status["valid_statuses"] == ["open", "in_progress", "done"]
    # aegis: a sprint command on a missing sprint
    _assert_coded(client.post("/sprints/9999/start", headers=H1), 404, "not_found")
    # mentor: the space command's `not_found` kind
    space = _assert_coded(
        client.patch("/spaces/9999", json={"name": "n"}, headers=H1), 404, "not_found"
    )
    assert set(space) == {"detail", "code"}
    # core: the worker command's `not_found` kind
    worker = _assert_coded(
        client.post("/workers/9999/kill", headers=H1), 404, "not_found"
    )
    assert worker == {"detail": "no such worker", "code": "not_found"}
    # core: the agent-run command's `forbidden` kind (header auth is not a token)
    heartbeat = _assert_coded(
        client.put("/agent-runs/heartbeat", json={"run_id": "r"}, headers=H1),
        403,
        "forbidden",
    )
    assert heartbeat == {"detail": "bearer token required", "code": "forbidden"}
    # core: the approval command on a missing request
    _assert_coded(
        client.post(
            "/approvals/9999/decision", json={"decision": "approve"}, headers=H1
        ),
        404,
        "not_found",
    )
    # core: undo's own code survives unchanged
    undo = _assert_coded(
        client.post("/activity/999999/undo", headers=H1), 404, "undo_event_not_found"
    )
    assert undo == {"detail": "no such activity event", "code": "undo_event_not_found"}
    # aegis: the desk cursor keeps its code and extra field
    ahead = _assert_coded(
        client.post("/desk/cursor", json={"after_id": 999_999}, headers=H1),
        422,
        "cursor_ahead",
    )
    assert isinstance(ahead["latest_visible_event_id"], int)
    # aegis + workflows: a query the grammar refuses keeps its detail object, and
    # the top-level code repeats the one it always carried inside.
    for url in ("/issues?q=labl:infra", "/search/workspace?q=labl:infra"):
        query = _assert_coded(client.get(url, headers=H1), 422, "invalid_query")
        assert query["detail"]["code"] == "invalid_query"
        assert query["detail"]["atom"] == "labl"
    # resource 404 from a plain route
    gone = _assert_coded(client.get("/issues/99999", headers=H1), 404, "not_found")
    assert gone == {"detail": "no such issue", "code": "not_found"}


def test_approval_answers_carry_codes(world):
    client = world["client"]
    gated = client.put(
        f"/approvals/policies/{world['writer_id']}",
        json={"action_kind": "issue.close"},
        headers=H1,
    )
    assert gated.status_code == 200, gated.text
    mine = client.post("/issues", json={"title": "gated"}, headers=world["writer"])
    close = client.patch(
        f"/issues/{mine.json()['id']}", json={"status": "done"}, headers=world["writer"]
    )
    assert close.status_code == 202
    asked = close.json()
    assert asked["code"] == "approval_required"
    # The REST body is unchanged by A5: no MCP-only fields leak into it.
    assert "applied" not in asked and "next" not in asked

    decided = client.post(
        f"/approvals/{asked['approval']['id']}/decision",
        json={"decision": "reject", "note": "not yet"},
        headers=H1,
    )
    assert decided.status_code == 200, decided.text
    rejected = _assert_coded(
        client.patch(
            f"/issues/{mine.json()['id']}",
            json={"status": "done"},
            headers=world["writer"],
        ),
        409,
        "approval_rejected",
    )
    assert rejected["approval"]["state"] == "rejected"


def test_idempotency_refusals_carry_codes(world):
    client = world["client"]
    first = client.post(
        "/issues", json={"title": "one"}, headers={**H1, "Idempotency-Key": "k-1"}
    )
    assert first.status_code == 201, first.text
    reused = _assert_coded(
        client.post(
            "/issues", json={"title": "two"}, headers={**H1, "Idempotency-Key": "k-1"}
        ),
        409,
        "idempotency_mismatch",
    )
    assert reused == {
        "detail": "Idempotency-Key reused for a different request",
        "code": "idempotency_mismatch",
    }
    malformed = _assert_coded(
        client.post(
            "/issues", json={"title": "x"}, headers={**H1, "Idempotency-Key": "a b"}
        ),
        400,
        "idempotency_key_invalid",
    )
    assert malformed["detail"] == (
        "Idempotency-Key must be 1-255 visible ASCII characters"
    )


def test_run_binding_refusal_carries_a_code(world):
    client = world["client"]
    first = client.post(
        "/issues", json={"title": "r"}, headers={**H1, "X-Athena-Run": "run-a5"}
    )
    assert first.status_code == 201, first.text
    other = _assert_coded(
        client.post(
            "/issues",
            json={"title": "r2"},
            headers={**world["writer"], "X-Athena-Run": "run-a5"},
        ),
        403,
        "run_binding_conflict",
    )
    assert other == {
        "detail": "run 'run-a5' is bound to another actor",
        "code": "run_binding_conflict",
    }


def test_middleware_refusals_carry_codes(tmp_path):
    app = create_app(
        tmp_path / "limits.db",
        max_request_body_bytes=64,
        token_rate_limit_per_minute=1,
    )
    with TestClient(app) as client:
        client.post(
            "/users", json={"email": "a@e.com", "name": "Ann", "password": "secret"}
        )
        large = _assert_coded(
            client.post("/issues", json={"title": "x" * 200}, headers=H1),
            413,
            "payload_too_large",
        )
        assert large == {
            "detail": "request body too large",
            "code": "payload_too_large",
            "limit_bytes": 64,
        }

        made = client.post(
            "/tokens", json={"name": "t", "scopes": ["read"]}, headers=H1
        ).json()
        bearer = {"Authorization": f"Bearer {made['token']}"}
        assert client.get("/issues", headers=bearer).status_code == 200
        limited = _assert_coded(
            client.get("/issues", headers=bearer), 429, "rate_limited"
        )
        assert limited == {
            "detail": "token rate limit exceeded",
            "code": "rate_limited",
            "rate_limit": {
                "limit": 1,
                "remaining": 0,
                "retry_after_seconds": limited["rate_limit"]["retry_after_seconds"],
            },
        }
        assert limited["rate_limit"]["retry_after_seconds"] >= 1

    with TestClient(app, base_url="http://elsewhere.example") as client:
        boundary = _assert_coded(client.get("/healthz"), 503, "deployment_boundary")
        assert boundary["detail"] == "request rejected by deployment boundary"


def test_an_unhandled_error_is_a_coded_json_500(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    with TestClient(
        create_app(tmp_path / "boom.db"), raise_server_exceptions=False
    ) as client:
        client.post(
            "/users", json={"email": "a@e.com", "name": "Ann", "password": "secret"}
        )
        monkeypatch.setattr(aegis_issues, "get_issue", broken)
        failed = _assert_coded(client.get("/issues/1", headers=H1), 500)
        assert failed == {"detail": "Internal Server Error", "code": "internal_error"}


# --- MCP ------------------------------------------------------------------------


def _wire_call(server, name: str, arguments: dict):
    """Call a tool the way a client does: over the protocol, not in-process."""

    async def call():
        async with create_connected_server_and_client_session(
            server._mcp_server  # noqa: SLF001 — the protocol server under test
        ) as session:
            return await session.call_tool(name, arguments)

    return asyncio.run(call())


def _one_object(result) -> dict:
    assert len(result.content) == 1, result.content
    return json.loads(result.content[0].text)


def test_a_refused_mcp_call_is_one_json_object_with_its_code(world):
    server = mcp_server.build_server(AthenaClient(client=world["client"]))
    world["client"].headers.update(H1)
    result = _wire_call(server, "get_issue", {"ref": "99999"})
    assert result.isError is True
    refusal = _one_object(result)
    assert refusal == {
        "detail": "no such issue",
        "code": "not_found",
        "status_code": 404,
    }


def test_a_refusal_with_a_resolving_call_names_it(world):
    client = world["client"]
    client.headers.update(world["writer"])
    server = mcp_server.build_server(
        AthenaClient(client=client),
        scopes=frozenset({"read", "issue:write"}),
        profile=mcp_server.PROFILES["seat"],
    )
    issue = world["issue"]
    etag = client.get(f"/issues/{issue['id']}").headers["ETag"]
    result = _wire_call(
        server, "claim_issue", {"issue_id": issue["id"], "if_match": etag}
    )
    assert result.isError is True
    refusal = _one_object(result)
    assert refusal["code"] == "claim_held"
    assert refusal["status_code"] == 409
    assert refusal["detail"].startswith("issue is claimed by Ann until ")
    assert refusal["next"] == refusal_codes.next_call("claim_held")
    assert refusal["next"]["tool"] == "wait_for_events"
    assert isinstance(refusal["events_after"], int)
    assert refusal["holder"]["name"] == "Ann" and refusal["expires_at"]


def test_a_refusal_keeps_the_rest_body_extra_fields(world):
    client = world["client"]
    client.headers.update(H1)
    server = mcp_server.build_server(AthenaClient(client=client))
    result = _wire_call(server, "advance_desk_cursor", {"after_id": 999_999})
    assert result.isError is True
    refusal = _one_object(result)
    rest = client.post("/desk/cursor", json={"after_id": 999_999}).json()
    # The REST body, unchanged, plus the status and the resolving call.
    assert {k: refusal[k] for k in rest} == rest
    assert refusal["status_code"] == 422
    assert refusal["next"]["tool"] == "advance_desk_cursor"


def test_a_gated_write_over_mcp_says_it_was_not_applied(world):
    client = world["client"]
    gated = client.put(
        f"/approvals/policies/{world['writer_id']}",
        json={"action_kind": "issue.close"},
        headers=H1,
    )
    assert gated.status_code == 200, gated.text
    client.headers.update(world["writer"])
    mine = client.post("/issues", json={"title": "gated"}).json()
    server = mcp_server.build_server(
        AthenaClient(client=client),
        scopes=frozenset({"read", "issue:write"}),
        profile=mcp_server.PROFILES["seat"],
    )
    result = _wire_call(
        server,
        "update_issue",
        {"issue_id": mine["id"], "status": "done", "idempotency_key": "close-1"},
    )
    assert result.isError is False
    answer = _one_object(result)
    assert answer["code"] == "approval_required"
    assert answer["applied"] is False
    assert answer["next"] == refusal_codes.next_call("approval_required")
    assert answer["next"]["tool"] in mcp_server.SEAT_PROFILE
    assert answer["approval"]["state"] == "pending"
    assert client.get(f"/issues/{mine['id']}").json()["status"] != "done"


def test_a_failure_before_any_request_is_one_object_with_an_mcp_side_code(world):
    # Bad arguments never reach Athena, so there is no REST body to hand over:
    # the failure is still one object, with a code that has no HTTP status (A4).
    server = mcp_server.build_server(AthenaClient(client=world["client"]))
    result = _wire_call(server, "get_issue", {"ref": 5})
    assert result.isError is True
    failure = _one_object(result)
    assert failure == {
        "detail": "invalid arguments: ref: Input should be a valid string",
        "code": "invalid_arguments",
        "status_code": None,
    }
    assert failure["code"] in refusal_codes.MCP_REFUSAL_CODES
    assert failure["code"] not in REFUSAL_CODES


def test_the_fallbacks_for_a_status_or_code_outside_the_registry():
    assert refusal_codes.code_for_status(418) == "bad_request"
    assert refusal_codes.code_for_status(599) == "internal_error"
    assert refusal_codes.next_call(None) is None
    assert refusal_codes.next_call("not_a_code") is None
    assert refusal_codes.next_call("not_found") is None  # registered, no next


def test_the_handlers_keep_fastapis_shape_at_the_edges():
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError

    from athena.core import refusals

    # A status that may not carry a body still gets none, headers intact.
    bare = refusals._http_exception(
        None, HTTPException(status_code=304, headers={"ETag": '"x"'})
    )
    assert bare.status_code == 304 and bare.body == b""
    assert bare.headers["ETag"] == '"x"'
    # Each handler refuses an exception it was not registered for.
    with pytest.raises(ValueError):
        refusals._http_exception(None, ValueError("other"))
    with pytest.raises(ValueError):
        refusals._request_validation(None, ValueError("other"))
    coded = refusals._request_validation(None, RequestValidationError([]))
    assert json.loads(coded.body) == {"detail": [], "code": "validation_error"}


# --- MCP-side codes (A4) --------------------------------------------------------


def test_mcp_side_codes_have_no_http_status_and_never_collide():
    # A failure that never reached Athena has no HTTP answer: its entry has no
    # status field at all, and no REST code shares its name.
    assert set(refusal_codes.MCP_REFUSAL_CODES) == {
        "invalid_arguments",
        "unknown_tool",
        "coordination_root_unconfirmed",
        "athena_no_response",
        "tool_failed",
    }
    assert not set(refusal_codes.MCP_REFUSAL_CODES) & set(REFUSAL_CODES)
    for code, entry in refusal_codes.MCP_REFUSAL_CODES.items():
        assert entry.code == code
        assert not hasattr(entry, "statuses"), code
        assert entry.meaning.strip() and "\n" not in entry.meaning, code
        if entry.next is not None:
            assert entry.next.tool in mcp_server.SEAT_PROFILE, code
    assert refusal_codes.next_call("coordination_root_unconfirmed") == {
        "tool": "get_issue_lease",
        "hint": "issue_id; do not retry the claim without its root",
    }


def test_the_static_check_reads_the_mcp_side_codes_apart():
    checker = _load_checker()
    registry = checker.REGISTRY_PATH.read_text()
    assert checker.mcp_side_codes(registry) == set(refusal_codes.MCP_REFUSAL_CODES)
    assert checker.registered_codes(registry) == set(REFUSAL_CODES)


def test_the_static_check_keeps_mcp_side_codes_out_of_rest_answers():
    checker = _load_checker()
    registry = checker.REGISTRY_PATH.read_text()
    codes = checker.registered_codes(registry)
    mcp_codes = checker.mcp_side_codes(registry)
    rest = "def route():\n    raise Refusal(422, 'bad', code='invalid_arguments')\n"
    found = checker.check_source(rest, "route.py", codes, mcp_codes=mcp_codes)
    assert [v.message for v in found] == [
        "code= names 'invalid_arguments', an MCP-side code with no HTTP status; "
        "a REST answer may not carry it"
    ]
    # Under mcp/, a code= literal may name either kind, and nothing else.
    client = (
        "def f():\n"
        "    raise AthenaError('x', code='coordination_root_unconfirmed')\n"
        "def g():\n"
        "    raise AthenaError('x', code='not_found')\n"
    )
    assert (
        checker.check_source(
            client, "mcp/client.py", codes, rest=False, mcp_codes=mcp_codes
        )
        == []
    )
    typo = "def f():\n    raise AthenaError('x', code='root_unconfirmed')\n"
    assert checker.check_source(
        typo, "mcp/client.py", codes, rest=False, mcp_codes=mcp_codes
    )


def test_the_reference_lists_mcp_side_codes_with_no_status():
    text = (REPO / "docs" / "AGENT_API.md").read_text()
    section = text.split("### MCP-side codes\n", 1)[1]
    for code in refusal_codes.MCP_REFUSAL_CODES:
        assert f"| `{code}` | none |" in section, code
