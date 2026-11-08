"""W3C trace context on writes: a correlation hint next to the run headers.

WHY: a seat that writes to Athena usually also writes spans somewhere else. The
run headers say which Athena run an event belongs to; nothing said which trace the
same action belongs to, so an operator could not jump from an Athena event into the
tracing stack. `traceparent` and `tracestate` on a REST write (and in MCP call
metadata, forwarded as the same headers) are now stored in the event detail under a
versioned marker, with exactly the standing docs/RUNS.md gives the lineage headers:
hints, never authorization, never used to grant anything. A malformed value is
dropped with a logged reason and the write proceeds. Old events render unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi.testclient import TestClient
import pytest

from athena.aegis import issue_history, lease_commands
from athena.core import activity, db, trace_context
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACESTATE = "congo=t61rcWkgMzE,rojo=00f067aa0ba902b7"
TRACE = {"traceparent": TRACEPARENT, "tracestate": TRACESTATE}
LOGGER = "athena"


# --- 1. the shape ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (TRACEPARENT, TRACEPARENT),
        # A higher version may carry more fields; the first four are what we keep.
        (
            "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
            "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ),
        ("  " + TRACEPARENT + "  ", TRACEPARENT),
        (None, None),
        ("", None),
        ("garbage", None),
        # version ff is reserved; version 00 has exactly four fields
        ("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra", None),
        # all-zero ids are invalid
        ("00-00000000000000000000000000000000-00f067aa0ba902b7-01", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01", None),
        # uppercase hex, wrong lengths, wrong separators
        ("00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01", None),
        ("00_4bf92f3577b34da6a3ce929d0e0e4736_00f067aa0ba902b7_01", None),
        ("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1", None),
    ],
)
def test_traceparent_shape_is_validated(raw, expected):
    assert trace_context.parse_traceparent(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (TRACESTATE, TRACESTATE),
        ("congo=t61rcWkgMzE , rojo=00f067aa0ba902b7", TRACESTATE),
        ("a@vendor=1,b=2", "a@vendor=1,b=2"),
        (",,congo=1,,", "congo=1"),
        (None, None),
        ("", None),
        ("novalue", None),
        ("Key=upper", None),
        ("k=has=equals", None),
        ("k=has,comma=x", None),  # the comma splits it into a member with no value
        ("dup=1,dup=2", None),
        (",".join(f"k{i}=v" for i in range(33)), None),
        ("k=" + "v" * 257, None),
        ("k=" + "v" * 500 + ",j=" + "w" * 100, None),  # over 512 in all
    ],
)
def test_tracestate_shape_is_validated(raw, expected):
    assert trace_context.parse_tracestate(raw) == expected


def test_attach_and_split_round_trip():
    token = trace_context.set_client_trace(TRACEPARENT, TRACESTATE)
    try:
        attached = trace_context.attach("open → done")
    finally:
        trace_context.reset_trace(token)
    assert attached.startswith("open → done" + trace_context.TRACE_CONTEXT_MARKER)
    assert trace_context.split_detail(attached) == ("open → done", TRACE)
    assert trace_context.split_detail("open → done") == ("open → done", None)
    assert trace_context.split_detail("") == ("", None)
    # Nothing in force: detail passes through untouched.
    assert trace_context.attach("open → done") == "open → done"
    # Only the parent: no tracestate key, not a null one.
    token = trace_context.set_client_trace(TRACEPARENT, None)
    try:
        only_parent = trace_context.attach("")
    finally:
        trace_context.reset_trace(token)
    assert trace_context.split_detail(only_parent) == ("", {"traceparent": TRACEPARENT})


def test_tracestate_without_a_traceparent_is_dropped(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        token = trace_context.set_client_trace(None, TRACESTATE)
        try:
            assert trace_context.get_trace() is None
        finally:
            trace_context.reset_trace(token)
    assert "trace context dropped" in caplog.text
    assert "tracestate" in caplog.text


def test_the_existing_detail_parsers_see_through_the_suffix():
    # WHY: two readers already parse detail structurally. A suffix must not turn
    # "open → done" into "open → done; trace…" or break the completion report.
    token = trace_context.set_client_trace(TRACEPARENT, TRACESTATE)
    try:
        transition = trace_context.attach("open → done")
        report = lease_commands._normalize_completion_report(
            results=[{"condition": "read the brief", "state": "met", "note": "yes"}],
            unfinished=[],
            uncertainty=[],
            evidence=[],
        )
        generation = "0123456789abcdef0123456789abcdef"
        completion = f"generation {generation}; reported_completion_v1 " + json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        completion_traced = trace_context.attach(completion)
    finally:
        trace_context.reset_trace(token)
    assert issue_history._before_after(transition) == ("open", "done")
    plain = {"verb": "claim_completed", "imported_at": None, "detail": completion}
    traced = {
        "verb": "claim_completed",
        "imported_at": None,
        "detail": completion_traced,
    }
    assert lease_commands.reported_completion(plain) == report  # the control
    assert lease_commands.reported_completion(traced) == report


# --- the board --------------------------------------------------------------------


@pytest.fixture
def board(tmp_path):
    database = tmp_path / "trace.db"
    app = create_app(database)
    with TestClient(app) as admin:
        admin.post(
            "/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"}
        )
        admin.headers.update(H1)
        reader = admin.post(
            "/users/onboard_agent",
            json={"email": "ro@e.com", "name": "Reader", "scopes": ["read"]},
        )
        assert reader.status_code == 201, reader.text
        reader_token = reader.json()["token"]["token"]
        yield {
            "app": app,
            "database": database,
            "admin": admin,
            "reader": TestClient(
                app, headers={"Authorization": f"Bearer {reader_token}"}
            ),
        }


def _events(database, **filters):
    conn = db.connect(database)
    try:
        return activity.list_activity(conn, limit=100, **filters)
    finally:
        conn.close()


# --- 2. a valid traceparent is stored and shown ---------------------------------


def test_a_valid_traceparent_is_stored_and_shown(board):
    admin = board["admin"]
    created = admin.post(
        "/issues",
        json={"title": "traced"},
        headers={
            "X-Athena-Run": "t1",
            "traceparent": TRACEPARENT,
            "tracestate": TRACESTATE,
        },
    )
    assert created.status_code == 201, created.text
    event = next(
        e for e in _events(board["database"], verb="created") if e["run_id"] == "t1"
    )
    assert trace_context.split_detail(event["detail"]) == ("", TRACE)
    assert event["detail"].endswith(
        trace_context.TRACE_CONTEXT_MARKER
        + json.dumps(TRACE, sort_keys=True, separators=(",", ":"))
    )

    replay = admin.get("/activity/runs/t1/replay")
    assert replay.status_code == 200, replay.text
    assert replay.json()["events"][0]["trace"] == TRACE
    assert (
        replay.json()["events"][0]["detail"] == event["detail"]
    )  # the record, as stored
    assert "trace" in replay.json()["determinism_contract"]["observational_fields"]

    lineage = admin.get("/activity/runs/t1/lineage")
    assert lineage.status_code == 200, lineage.text
    assert lineage.json()["run"]["events"][0]["trace"] == TRACE

    page = admin.get("/aegis/activity/runs/t1/lineage")
    assert page.status_code == 200
    assert TRACEPARENT in page.text
    assert TRACESTATE in page.text


# --- 3. a malformed one is dropped and the write still succeeds -------------------


def test_a_malformed_traceparent_is_dropped_and_the_write_succeeds(board, caplog):
    admin = board["admin"]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        created = admin.post(
            "/issues",
            json={"title": "still created"},
            headers={
                "X-Athena-Run": "t2",
                "traceparent": "not-a-trace",
                "tracestate": TRACESTATE,
            },
        )
    assert created.status_code == 201, created.text
    event = next(
        e for e in _events(board["database"], verb="created") if e["run_id"] == "t2"
    )
    assert trace_context.split_detail(event["detail"]) == ("", None)
    assert trace_context.TRACE_CONTEXT_MARKER not in event["detail"]
    dropped = [
        r.getMessage()
        for r in caplog.records
        if "trace context dropped" in r.getMessage()
    ]
    assert dropped, caplog.text
    assert any("traceparent" in line for line in dropped)
    # The reason is logged, the value never is: a header is attacker-controlled text.
    assert "not-a-trace" not in caplog.text
    replay = admin.get("/activity/runs/t2/replay").json()
    assert "trace" not in replay["events"][0]


def test_a_malformed_tracestate_is_dropped_alone(board, caplog):
    admin = board["admin"]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        created = admin.post(
            "/issues",
            json={"title": "parent kept"},
            headers={
                "X-Athena-Run": "t3",
                "traceparent": TRACEPARENT,
                "tracestate": "Bad=Key",
            },
        )
    assert created.status_code == 201, created.text
    event = next(
        e for e in _events(board["database"], verb="created") if e["run_id"] == "t3"
    )
    assert trace_context.split_detail(event["detail"]) == (
        "",
        {"traceparent": TRACEPARENT},
    )
    assert any(
        "tracestate" in r.getMessage()
        for r in caplog.records
        if "trace context dropped" in r.getMessage()
    )
    assert "Bad=Key" not in caplog.text


# --- 4. trace context can never change who may do what -----------------------------


def test_trace_context_never_changes_who_may_do_what(board):
    admin, reader = board["admin"], board["reader"]
    bait = {
        "traceparent": TRACEPARENT,
        "tracestate": "athena=admin,role=admin,scope=issue:write",
        "X-Athena-Run": "t4",
    }
    # A read-only token cannot create an issue, with or without trace context.
    refused_plain = reader.post("/issues", json={"title": "no"})
    refused_traced = reader.post("/issues", json={"title": "no"}, headers=bait)
    assert refused_plain.status_code == refused_traced.status_code == 403, (
        refused_plain.text,
        refused_traced.text,
    )
    # Nor read an admin surface.
    assert reader.get("/activity/chain", headers=bait).status_code == 403
    # Positive control: the same writes are possible for an authorized actor, with
    # trace context and without, and only then is anything recorded.
    assert admin.post("/issues", json={"title": "yes"}).status_code == 201
    assert admin.post("/issues", json={"title": "yes"}, headers=bait).status_code == 201
    assert not [
        e
        for e in _events(board["database"], verb="created")
        if e["detail"] and "role=admin" in e["detail"] and e["actor_id"] != 1
    ]
    assert not _events(board["database"], run_id="t4", actor_id=2)


# --- 5. old events render unchanged -----------------------------------------------


def test_old_events_render_unchanged(board):
    admin = board["admin"]
    assert (
        admin.post(
            "/issues", json={"title": "old"}, headers={"X-Athena-Run": "old"}
        ).status_code
        == 201
    )
    assert (
        admin.post(
            "/issues", json={"title": "new"}, headers={"X-Athena-Run": "new", **TRACE}
        ).status_code
        == 201
    )
    untraced_keys = {
        "id",
        "actor_id",
        "actor_name",
        "verb",
        "target_kind",
        "target_id",
        "detail",
        "created_at",
        "run_id",
        "parent_run_id",
        "forked_from_event_id",
        "imported_at",
        "reverses_event_id",
    }
    replay_old = admin.get("/activity/runs/old/replay").json()
    assert set(replay_old["events"][0]) == untraced_keys
    assert trace_context.TRACE_CONTEXT_MARKER not in replay_old["events"][0]["detail"]
    lineage_old = admin.get("/activity/runs/old/lineage").json()
    assert set(lineage_old["run"]["events"][0]) == untraced_keys
    assert lineage_old["run"]["children"] == []  # the node keeps every key it had
    feed_old = admin.get("/activity", params={"run_id": "old"}).json()
    assert set(feed_old[0]) == untraced_keys
    # Positive control: the traced run renders the extra field, and only there.
    replay_new = admin.get("/activity/runs/new/replay").json()
    assert set(replay_new["events"][0]) == untraced_keys | {"trace"}
    feed_new = admin.get("/activity", params={"run_id": "new"}).json()
    assert set(feed_new[0]) == untraced_keys  # the feed is not a replay view: unchanged


def test_imported_history_carries_no_native_trace(board):
    conn = db.connect(board["database"])
    try:
        token = trace_context.set_client_trace(TRACEPARENT, TRACESTATE)
        try:
            native = activity.record(
                conn, actor_id=1, verb="commented", target_kind="issue", target_id=1
            )
            foreign = activity.record(
                conn,
                actor_id=1,
                verb="commented",
                target_kind="issue",
                target_id=1,
                imported_at="2026-11-08T00:00:00Z",
            )
        finally:
            trace_context.reset_trace(token)
    finally:
        conn.close()
    assert trace_context.split_detail(native["detail"])[1] == TRACE  # the control
    assert trace_context.split_detail(foreign["detail"]) == ("", None)


# --- 6. MCP call metadata -----------------------------------------------------------


def _with_request_meta(meta_fields: dict | None):
    """Enter the SDK's request context the way the low-level server does for one
    tools/call, carrying the call's `_meta`; None means no request at all."""
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext
    from mcp.types import RequestParams

    meta = None if meta_fields is None else RequestParams.Meta(**meta_fields)
    return request_ctx.set(
        RequestContext(request_id=1, meta=meta, session=None, lifespan_context=None)
    )


def _reset_request_meta(token):
    from mcp.server.lowlevel.server import request_ctx

    request_ctx.reset(token)


def test_mcp_call_metadata_is_forwarded_as_the_same_headers(board):
    admin = board["admin"]
    server = mcp_server.build_server(AthenaClient(client=admin, run_id="mcp-t"))
    token = _with_request_meta({"traceparent": TRACEPARENT, "tracestate": TRACESTATE})
    try:
        asyncio.run(server.call_tool("create_issue", {"title": "from mcp"}))
    finally:
        _reset_request_meta(token)
    traced = [
        e for e in _events(board["database"], run_id="mcp-t") if e["verb"] == "created"
    ]
    assert trace_context.split_detail(traced[0]["detail"])[1] == TRACE
    # The headers do not outlive the call.
    assert "traceparent" not in admin.headers
    assert "tracestate" not in admin.headers
    # Without metadata nothing is attached, and the run header still is.
    asyncio.run(server.call_tool("create_issue", {"title": "untraced from mcp"}))
    rows = [
        e for e in _events(board["database"], run_id="mcp-t") if e["verb"] == "created"
    ]
    assert [
        trace_context.split_detail(e["detail"])[1]
        for e in sorted(rows, key=lambda e: e["id"])
    ] == [TRACE, None]


def test_mcp_metadata_that_is_not_a_short_string_is_not_forwarded(board):
    admin = board["admin"]
    server = mcp_server.build_server(AthenaClient(client=admin, run_id="mcp-big"))
    token = _with_request_meta(
        {"traceparent": "x" * 2000, "tracestate": ["not", "a", "string"]}
    )
    try:
        asyncio.run(server.call_tool("create_issue", {"title": "oversize meta"}))
    finally:
        _reset_request_meta(token)
    rows = [
        e
        for e in _events(board["database"], run_id="mcp-big")
        if e["verb"] == "created"
    ]
    assert trace_context.split_detail(rows[0]["detail"]) == ("", None)


class _Recording:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {}

        return record


def test_a_call_without_metadata_touches_only_the_tool_it_called():
    # WHY: harnesses inject recording and stub clients; the trace wrapper must not
    # reach for a client method unless a trace is actually present.
    client = _Recording()
    server = mcp_server.build_server(client)
    asyncio.run(server.call_tool("comment_on_issue", {"issue_id": 1, "body": "hi"}))
    assert [name for name, _, _ in client.calls] == ["comment_on_issue"]


def test_the_client_carries_the_headers_only_inside_the_block(board):
    admin = board["admin"]
    client = AthenaClient(client=admin)
    with client.trace_context(TRACEPARENT, TRACESTATE):
        assert admin.headers["traceparent"] == TRACEPARENT
        assert admin.headers["tracestate"] == TRACESTATE
    assert "traceparent" not in admin.headers
    assert "tracestate" not in admin.headers
    with client.trace_context(TRACEPARENT, None):
        assert "tracestate" not in admin.headers
    with client.trace_context(None, TRACESTATE):
        assert "traceparent" not in admin.headers
        assert (
            "tracestate" not in admin.headers
        )  # state without a parent is meaningless


# --- 7. the doc ----------------------------------------------------------------------


def test_the_runs_doc_states_the_standing():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "docs" / "RUNS.md").read_text()
    assert "traceparent" in text and "tracestate" in text
    assert "trace_context_v1" in text
    assert "never authorization" in text
