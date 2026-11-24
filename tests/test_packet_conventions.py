"""Agent packets speak one dialect of time, lanes and paging (A13).

The lead's desk read on 2026-11-21 gave ``identity.budget.window_started_at`` as
``2026-11-21T18:19:13Z`` and ``office.chair.expires_at`` as
``2026-11-21 18:49:13``: two shapes in one answer, and a third
(``2026-11-21T18:19:13.123456+00:00``) sat in the conductor's project brief
and review queue. ``asks.review_requests`` was absent when empty while every
other lane was present and empty, and an expiry handed an agent a wall-clock
instant to subtract from a clock it does not share.

The contract held here (``docs/AGENT_GUIDE.md``, "Conventions"):

- Every timestamp in an agent packet is RFC 3339 UTC to the second,
  ``2026-11-21T18:19:13Z``. These tests find timestamps by walking VALUES, so a
  field named anything at all cannot bring a second shape back unnoticed.
- Every expiry ``<x>expires_at`` has ``<x>expires_in_seconds`` beside it,
  counted from the same clock read as the packet's other clock verdicts.
- Every desk lane ``docs/DESK.md`` documents is present when empty.
- Over ``/openapi-agent.json``: every timestamp property of an agent packet
  declares the one format (``date-time``) and no timestamp declares another;
  the paging styles are a frozen list naming the operations that use each, and
  a new operation pages only in D2's vocabulary (``cursor`` in, ``next_cursor``
  and ``has_more`` out).

"Agent packet" is every read the narrow MCP profiles (``seat``,
``seat+knowledge``, ``conductor``) carry, except a resource's own
representation (an issue, a comment, a page, a label, a project, a search hit):
the issue's ETag covers exactly its representation, so those keep their shape.
``PACKET_READS`` and ``RESOURCE_READS`` split the profiles' reads, and a test
holds the split against the declared tools.
"""

from __future__ import annotations

from collections.abc import Iterator
import copy
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, bearer, bootstrap, make_app, onboard_agent

from athena import agent_openapi, config
from athena.core import db, packet_time
from athena.main import AGENT_TRANSPORT
from athena.mcp import server as mcp_server

REPO = Path(__file__).resolve().parents[1]
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
#: A timestamp in any shape this codebase has produced: a date and a time of
#: day, joined by a space or a ``T``. A date alone (a metrics day) is a date.
TIMESTAMP_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
#: Property names the OpenAPI checks treat as timestamps: the rule
#: ``core/packet_time.py`` converts by.
TIMESTAMP_NAMES = frozenset({"at", "since", "freshness"})
DATE_TIME = "date-time"
EXPIRY = "expires_at"
SECONDS = "expires_in_seconds"

#: The narrow profiles' reads that are agent packets: tool -> its REST read.
PACKET_READS: dict[str, tuple[str, str]] = {
    "my_desk": ("GET", "/desk"),
    "get_issue_work_context": ("GET", "/issues/{ref}/work-context"),
    "get_issue_lease": ("GET", "/issues/{issue_id}/lease"),
    "wait_for_events": ("GET", "/events"),
    "list_notifications": ("GET", "/notifications"),
    "list_run_controls": ("GET", "/run-controls"),
    "list_my_delegated_work": ("GET", "/delegations/me"),
    "get_project_brief": ("GET", "/projects/{project_id}/brief"),
    "get_project_floor": ("GET", "/projects/{project_id}/floor"),
    "get_project_review_queue": ("GET", "/projects/{project_id}/review-queue"),
    "get_issue_history": ("GET", "/issues/{issue_id}/history"),
    "get_issue_prior_text": ("GET", "/issues/{issue_id}/prior-texts/{event_id}"),
    "get_attention_ranking": ("GET", "/attention/ranking"),
    # No timestamp in it; the first read to page in D2's vocabulary.
    "list_eligible_accounts": ("GET", "/issues/{issue_id}/eligible-accounts"),
}
#: The narrow profiles' reads that answer a resource's own representation.
RESOURCE_READS: dict[str, str] = {
    "get_idempotency_key": "one key's state; it carries no time and no expiry",
    "get_issue": "the issue; its ETag covers exactly this representation",
    "search_work": "issues, each its own representation",
    "list_subtasks": "issues, each its own representation",
    "list_issue_comments": "comments",
    "get_issue_runbook": "a runbook page reference",
    "get_page": "a page; its ETag covers its version",
    "list_pages": "pages",
    "list_labels": "labels",
    "list_projects": "projects",
    "search": "search hits",
}
#: Read tools with no REST call.
CLIENT_SIDE = frozenset({"current_run"})

# --- paging: the styles that exist, by name, and who uses each -----------------
#
# A style is what a caller sends to get the next page (the continuation
# parameters) and what the answer says about it (``has_more`` and ``next_*``).
# A ``limit`` alone is a bound, not a page: nothing continues it. ``since`` on
# the security reads is a lower bound on a newest-first list, not a
# continuation, and is not counted.

#: Query parameters that continue a list, in any vocabulary seen in APIs.
CONTINUATION_PARAMS = frozenset(
    {
        "offset",
        "skip",
        "start",
        "page",
        "page_size",
        "page_token",
        "cursor",
        "after",
        "after_id",
        "before",
        "before_id",
        "starting_after",
        "ending_before",
        "marker",
        "continuation",
        "next",
        "from_id",
    }
)
#: Answer fields that say how to continue: ``has_more`` and ``next_<param>``.
CONTINUATION_FIELDS = frozenset(
    {"has_more", "next_token", *(f"next_{name}" for name in CONTINUATION_PARAMS)}
)
D2 = "cursor, next_cursor and has_more (D2)"
PAGING_STYLES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    D2: (frozenset({"cursor"}), frozenset({"next_cursor", "has_more"})),
    "offset, bare list": (frozenset({"offset"}), frozenset()),
    "offset, next_offset and has_more": (
        frozenset({"offset"}),
        frozenset({"next_offset", "has_more"}),
    ),
    "after, next_after and has_more": (
        frozenset({"after"}),
        frozenset({"next_after", "has_more"}),
    ),
    "after_id, next_after and has_more": (
        frozenset({"after_id"}),
        frozenset({"next_after", "has_more"}),
    ),
    "before_id, bare list": (frozenset({"before_id"}), frozenset()),
    "cursor and next_cursor, no has_more": (
        frozenset({"cursor"}),
        frozenset({"next_cursor"}),
    ),
}
#: Frozen: the operations that page in each style today. D2 is the only style a
#: new operation may use; the others keep their callers and gain none.
PAGING_OPERATIONS: dict[str, frozenset[str]] = {
    D2: frozenset(),
    "offset, bare list": frozenset(
        {"GET /issues", "GET /issues/search", "GET /search"}
    ),
    "offset, next_offset and has_more": frozenset({"GET /delegations/me"}),
    "after, next_after and has_more": frozenset({"GET /events"}),
    "after_id, next_after and has_more": frozenset({"GET /activity/chain/verify"}),
    "before_id, bare list": frozenset({"GET /activity", "GET /activity/export.csv"}),
    "cursor and next_cursor, no has_more": frozenset(
        {"GET /projects/{project_id}/review-queue"}
    ),
}


# --- one workspace where every desk lane has a row and every expiry exists ------


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text[:600]
    return response.json()


def _claim(client: TestClient, issue_id: int, **body: Any) -> dict[str, Any]:
    tag = client.get(f"/issues/{issue_id}").headers["ETag"]
    return _ok(
        client.post(f"/issues/{issue_id}/claim", json=body, headers={"If-Match": tag}),
        201,
    )


def _yield_for_input(client: TestClient, issue_id: int) -> None:
    held = _claim(client, issue_id)
    _ok(
        client.post(
            f"/issues/{issue_id}/yield",
            json={
                "generation": held["generation"],
                "reason": "needs_input",
                "note": "stuck",
                "attempted_work": "tried the obvious",
                "evidence": ["log line"],
                "blocking_question": "which way?",
                "resume_instructions": "pick a way and go",
            },
        ),
        201,
    )


def seed(tmp_path: Path) -> dict[str, Any]:
    """A seat (``sol``) whose desk has a row in every lane and every expiry, a
    review issue it holds with a handoff and a review request, and the
    conductor packets of their project. Returns every packet, read at the end."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as admin:
        admin.headers.update(ADMIN)
        bootstrap(admin)
        sol = onboard_agent(admin, scopes=("read", "issue:write", "docs:write"))
        terra = onboard_agent(admin, email="terra@e.com", name="Terra")
        sol_id, terra_id = sol["user"]["id"], terra["user"]["id"]
        as_sol = TestClient(app, headers=bearer(sol))
        as_terra = TestClient(app, headers=bearer(terra))
        project = _ok(admin.post("/projects", json={"name": "Ops", "key": "OPS"}), 201)

        def issue(title: str, *delegates: int) -> int:
            made = _ok(
                admin.post(
                    "/issues", json={"title": title, "project_id": project["id"]}
                ),
                201,
            )["id"]
            for user_id in delegates:
                _ok(
                    admin.post(f"/issues/{made}/delegate", json={"user_id": user_id}),
                    201,
                )
            return made

        # identity.budget
        _ok(
            admin.put(
                f"/users/{sol_id}/budget", json={"window": "hour", "action_limit": 500}
            )
        )
        # work.ready and work.delegations
        ready = issue("ready for sol", sol_id)
        # asks.review_requests: a candidate the operator built, a request naming
        # sol, the review handed from terra to sol (asks.claim_handoffs,
        # work.leases, the office chair).
        build = issue("build")
        held = _claim(admin, build)
        candidate = _ok(
            admin.post(
                f"/issues/{build}/complete",
                json={
                    "generation": held["generation"],
                    "candidate_submission": {
                        "identity": {
                            "kind": "git",
                            "source": "o/r",
                            "revision": "a" * 40,
                        },
                        "expected_previous_candidate_id": None,
                    },
                },
            )
        )["candidate_submission"]["candidate_id"]
        review = issue("review the build", terra_id, sol_id)
        _ok(
            admin.post(
                f"/issues/{review}/review-requests",
                json={"candidate_id": candidate, "reviewer_id": sol_id},
            )
        )
        _yield_for_input(as_terra, review)
        _claim(as_sol, review, paths=["src/review.py"])
        # asks.worker_kill_requests
        worker = _ok(as_sol.put("/workers/heartbeat", json={"worker_key": "w-1"}))
        _ok(admin.post(f"/workers/{worker['id']}/kill"))
        # asks.run_controls
        _ok(as_sol.put("/agent-runs/heartbeat", json={"run_id": "sol-run-1"}))
        _ok(
            admin.post(
                "/run-controls",
                json={"run_id": "sol-run-1", "kind": "steer", "payload": "go left"},
            ),
            201,
        )
        # work.leases.lapsed
        lapsed = issue("lapsed for sol", sol_id)
        _claim(as_sol, lapsed)
        # work.waiting_on_operator: sol's own open question
        _yield_for_input(as_sol, issue("asked by sol", sol_id))
        # work.waiting_on: sol delegated, terra has not picked it up
        waiting = issue("waiting on terra")
        _ok(admin.put(f"/issues/{waiting}/assignee", json={"assignee_id": sol_id}))
        _ok(as_sol.post(f"/issues/{waiting}/delegate", json={"user_id": terra_id}), 201)
        # identity.notebook
        _ok(as_sol.post("/desk/notebook"), 201)
        conn = db.connect(db_file)
        try:
            conn.execute(
                "UPDATE issue_leases SET expires_at = '2000-01-01 00:00:00' "
                "WHERE issue_id = ?",
                (lapsed,),
            )
            conn.execute(
                "UPDATE issue_contributors SET added_at = '2000-01-01 00:00:00' "
                "WHERE issue_id = ?",
                (waiting,),
            )
            conn.commit()
        finally:
            conn.close()
        # cursor: one event behind the newest, so signals count something
        newest = _ok(as_sol.get("/desk"))["signals"]["latest_visible_event_id"]
        _ok(as_sol.post("/desk/cursor", json={"after_id": newest - 1}))

        pid = project["id"]
        reads = {
            "desk": as_sol.get("/desk"),
            "office": as_sol.get("/office"),
            "desk_ready": as_sol.get("/desk/ready"),
            "desk_since": as_sol.get("/desk/since"),
            "work_context": as_sol.get(f"/issues/{review}/work-context"),
            "work_context_brief": as_sol.get(
                f"/issues/{review}/work-context?brief=true"
            ),
            "build_work_context": as_sol.get(f"/issues/{build}/work-context"),
            "lease": as_sol.get(f"/issues/{review}/lease"),
            "lapsed_lease": as_sol.get(f"/issues/{lapsed}/lease"),
            "events": as_sol.get("/events?after=0&limit=200"),
            "notifications": as_sol.get("/notifications"),
            "run_controls": as_sol.get("/run-controls"),
            "delegations": as_sol.get("/delegations/me"),
            "brief": admin.get(f"/projects/{pid}/brief"),
            "floor": admin.get(f"/projects/{pid}/floor"),
            "review_queue": admin.get(f"/projects/{pid}/review-queue"),
            "history": admin.get(f"/issues/{review}/history"),
            "attention": admin.get("/attention/ranking"),
        }
        answers = {name: _ok(response) for name, response in reads.items()}
        # A claim answers the lease packet too.
        answers["claim"] = _claim(as_sol, ready)
        return answers


@pytest.fixture
def packets(tmp_path, monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(config, "TOKEN_RATE_LIMIT_PER_MINUTE", 0)
    return seed(tmp_path)


def timestamps(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every ``(path, value)`` whose value looks like a timestamp, at any key."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from timestamps(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            yield from timestamps(child, f"{path}[]")
    elif isinstance(value, str) and TIMESTAMP_LIKE.match(value):
        yield path, value


def expiries(value: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any], str]]:
    """Every object holding an expiry: ``(path, the object, the expiry key)``."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith(EXPIRY):
                yield f"{path}.{key}", value, key
            yield from expiries(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            yield from expiries(child, f"{path}[]")


def _instant(value: str) -> datetime:
    assert RFC3339_UTC.match(value), value
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _seconds_beside(holder: dict[str, Any], key: str) -> Any:
    seconds_key = key[: -len(EXPIRY)] + SECONDS
    assert seconds_key in holder, f"{key} has no {seconds_key} beside it: {holder}"
    return holder[seconds_key]


# --- the shape ------------------------------------------------------------------


def test_every_timestamp_in_an_agent_packet_has_the_one_shape(packets):
    wrong = [
        f"{name}{path} = {value!r}"
        for name, packet in packets.items()
        for path, value in timestamps(packet)
        if not RFC3339_UTC.match(value)
    ]
    assert wrong == [], "timestamps outside RFC 3339 UTC:\n" + "\n".join(wrong)
    # Not vacuous: the walk reaches the fields the baseline found in three
    # shapes, and every packet carries at least one timestamp.
    found = {
        f"{name}{path}"
        for name, packet in packets.items()
        for path, _ in timestamps(packet)
    }
    for reached in (
        "desk.identity.budget.window_started_at",
        "desk.office.chair.expires_at",
        "desk.asks.run_controls.items[].expires_at",
        "desk.work.leases.lapsed[].expires_at",
        "desk.work.ready.observed_at",
        "desk.cursor.updated_at",
        "desk_since.since",
        "work_context.claim_handoffs.open.yielded.created_at",
        "build_work_context.review_tracking.current_candidate.candidate.reported_at",
        "lease.open_claim_handoff.yielded.created_at",
        "events.events[].created_at",
        "brief.observed_at",
        "review_queue.observed_at",
        "history.items[].at",
        "attention.items[].freshness",
    ):
        assert reached in found, reached
    assert all(any(True for _ in timestamps(packet)) for packet in packets.values())


def test_every_expiry_has_its_seconds_beside_it(packets):
    seen = []
    for name, packet in packets.items():
        for path, holder, key in expiries(packet):
            seen.append(f"{name}{path}")
            seconds = _seconds_beside(holder, key)
            if holder[key] is None:
                assert seconds is None, (name, path)
                continue
            assert isinstance(seconds, int) and seconds >= 0, (name, path, seconds)
    for expected in (
        "desk.asks.run_controls.items[].expires_at",
        "desk.work.leases.items[].expires_at",
        "desk.work.leases.lapsed[].expires_at",
        "desk.work.leases.lapsed_oldest_expires_at",
        "desk.office.chair.expires_at",
        "office.chair.expires_at",
        "lease.expires_at",
        "lapsed_lease.expires_at",
        "run_controls[].expires_at",
        "brief.active_claims.items[].expires_at",
        "floor.chairs[].occupant.expires_at",
        "claim.expires_at",
    ):
        assert expected in seen, expected


def test_the_seconds_count_from_the_packets_own_clock_read(packets):
    """The desk's ready lane and the brief state their clock read
    (``observed_at``); every expiry beside them counts from exactly that
    second. A lease says ``active`` from its clock: positive seconds and
    ``active`` are one verdict. A claim's clock read is its ``claimed_at``."""
    for name in ("desk", "brief"):
        packet = packets[name]
        observed = _instant(
            packet["work"]["ready"]["observed_at"]
            if name == "desk"
            else packet["observed_at"]
        )
        for path, holder, key in expiries(packet):
            if holder[key] is None:
                continue
            expected = max(0, int((_instant(holder[key]) - observed).total_seconds()))
            assert _seconds_beside(holder, key) == expected, (name, path)
    for name in ("lease", "lapsed_lease"):
        lease = packets[name]
        assert lease["active"] is (lease["expires_in_seconds"] > 0), lease
    assert packets["lapsed_lease"]["expires_in_seconds"] == 0
    claim = packets["claim"]
    assert claim["expires_in_seconds"] == int(
        (_instant(claim["expires_at"]) - _instant(claim["claimed_at"])).total_seconds()
    )


def test_one_injected_clock_reaches_the_verdict_and_the_seconds(tmp_path, monkeypatch):
    """With the packet clock pinned just short of a second boundary, the desk
    and the lease read count every expiry from that one read, truncated to the
    second as the stored expiry is, and ``active`` agrees with the count."""
    from athena.core import packet_time

    pinned = datetime.now(UTC).replace(microsecond=999_999)
    monkeypatch.setattr(packet_time, "clock", lambda: pinned)
    whole_second = pinned.replace(microsecond=0)
    app, _ = make_app(tmp_path)
    with TestClient(app) as admin:
        admin.headers.update(ADMIN)
        bootstrap(admin)
        sol = onboard_agent(admin)
        as_sol = TestClient(app, headers=bearer(sol))
        made = _ok(admin.post("/issues", json={"title": "held"}), 201)["id"]
        _ok(
            admin.post(f"/issues/{made}/delegate", json={"user_id": sol["user"]["id"]}),
            201,
        )
        _claim(as_sol, made, lease_seconds=600)
        board = _ok(as_sol.get("/desk"))
        lease = _ok(as_sol.get(f"/issues/{made}/lease"))
    held = board["work"]["leases"]["items"][0]
    for holder in (held, board["office"]["chair"], lease):
        expected = int((_instant(holder["expires_at"]) - whole_second).total_seconds())
        assert holder["expires_in_seconds"] == expected, holder
    assert lease["active"] is True and held["active"] is True


# --- the lanes ------------------------------------------------------------------


def documented_desk_lanes() -> list[tuple[str, ...]]:
    """The lane column of DESK.md's "The lanes" table, as key paths."""
    text = (REPO / "docs" / "DESK.md").read_text()
    table = text.split("## The lanes", 1)[1].split("\n## ", 1)[0]
    lanes = re.findall(r"^\| `([a-z_.]+)` \|", table, flags=re.MULTILINE)
    assert len(lanes) >= 10, lanes
    return [tuple(lane.split(".")) for lane in lanes]


def test_every_documented_desk_lane_is_present_when_empty(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as admin:
        admin.headers.update(ADMIN)
        bootstrap(admin)
        sol = onboard_agent(admin)
        board = _ok(TestClient(app, headers=bearer(sol)).get("/desk"))
    missing = []
    for lane in documented_desk_lanes():
        here: Any = board
        for step in lane:
            if not isinstance(here, dict) or step not in here:
                missing.append(".".join(lane))
                break
            here = here[step]
    assert missing == [], f"documented lanes absent from an empty desk: {missing}"
    # Present and empty, in the shape the lane has when it has rows.
    assert board["asks"]["review_requests"] == {
        "items": [],
        "visible_total": 0,
        "returned": 0,
        "limit": 20,
        "clipped": False,
    }


# --- /openapi-agent.json ----------------------------------------------------------


@pytest.fixture(scope="module")
def agent_document(tmp_path_factory) -> dict[str, Any]:
    """The document ``/openapi-agent.json`` serves: ``main.py`` answers that
    route with exactly ``build_agent_openapi(app, AGENT_TRANSPORT)``."""
    app, _ = make_app(tmp_path_factory.mktemp("agent-document"))
    return agent_openapi.build_agent_openapi(app, AGENT_TRANSPORT)


def _is_timestamp_name(name: str) -> bool:
    return name.endswith("_at") or name in TIMESTAMP_NAMES


def _ok_schema(operation: dict[str, Any]) -> dict[str, Any]:
    for code, answer in operation.get("responses", {}).items():
        if code.startswith("2"):
            return (answer.get("content", {}).get("application/json") or {}).get(
                "schema", {}
            )
    return {}


def _reachable(document: dict[str, Any], node: Any, found: set[str]) -> set[str]:
    """Every component schema ``node`` reaches through ``$ref``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[1]
            if name not in found:
                found.add(name)
                _reachable(document, document["components"]["schemas"][name], found)
        for child in node.values():
            _reachable(document, child, found)
    elif isinstance(node, list):
        for child in node:
            _reachable(document, child, found)
    return found


def _formats(prop: dict[str, Any]) -> set[str]:
    """The formats a property's string branches declare ("" when one declares none)."""
    branches = prop.get("anyOf") or prop.get("oneOf") or [prop]
    return {
        branch.get("format", "")
        for branch in branches
        if branch.get("type") == "string"
    }


def _properties(
    document: dict[str, Any], names: set[str]
) -> Iterator[tuple[str, dict]]:
    schemas = document["components"]["schemas"]
    for name in sorted(names):
        for prop_name, prop in (schemas[name].get("properties") or {}).items():
            yield f"{name}.{prop_name}", prop


def undeclared_packet_timestamps(document: dict[str, Any]) -> tuple[list[str], int]:
    """Timestamp properties an agent packet's answer reaches that do not declare
    exactly the one format, and how many were checked."""
    names: set[str] = set()
    for method, path in PACKET_READS.values():
        _reachable(document, _ok_schema(document["paths"][path][method.lower()]), names)
    wrong, checked = [], 0
    for where, prop in _properties(document, names):
        if _is_timestamp_name(where.rsplit(".", 1)[1]):
            checked += 1
            if _formats(prop) != {DATE_TIME}:
                wrong.append(f"{where}: {sorted(_formats(prop))}")
    return wrong, checked


def test_every_packet_timestamp_declares_the_one_format(agent_document):
    wrong, checked = undeclared_packet_timestamps(agent_document)
    assert wrong == [], (
        f"agent packet timestamps must declare format {DATE_TIME!r} "
        "(type them athena.core.packet_time.Timestamp):\n" + "\n".join(wrong)
    )
    assert checked >= 30, checked  # the typed packets are reached, not skipped


def test_no_timestamp_declares_a_second_format(agent_document):
    schemas = agent_document["components"]["schemas"]
    declared = {
        fmt
        for where, prop in _properties(agent_document, set(schemas))
        if _is_timestamp_name(where.rsplit(".", 1)[1])
        for fmt in _formats(prop)
        if fmt
    }
    assert declared == {DATE_TIME}, declared


def test_the_format_check_fails_on_an_untyped_packet_timestamp(agent_document):
    document = copy.deepcopy(agent_document)
    document["components"]["schemas"]["LeaseOut"]["properties"]["expires_at"] = {
        "type": "string"
    }
    wrong, _ = undeclared_packet_timestamps(document)
    assert wrong == ["LeaseOut.expires_at: ['']"]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_agent_api", REPO / "scripts" / "generate_agent_api.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_agent_api"] = module
    spec.loader.exec_module(module)
    return module


def test_the_packet_scope_is_the_profiles_reads():
    """Every read the narrow profiles carry is named a packet or a resource, so
    a new read joins one list on purpose; each packet is the REST read its
    tool makes."""
    generator = _load_generator()
    calls, methods = generator.client_method_calls(), generator.tool_client_methods()
    declared = mcp_server.declared()
    reads = {
        name
        for name, declaration in declared.items()
        if mcp_server.is_read_only(name)
        and any(
            declaration.carried_by(profile) for profile in mcp_server.BUDGETED_PROFILES
        )
    }
    named = set(PACKET_READS) | set(RESOURCE_READS) | CLIENT_SIDE
    assert reads - named == set(), (
        "classify these reads in PACKET_READS or RESOURCE_READS"
    )
    assert named - reads == set(), "these are no longer narrow-profile reads"
    for tool, (method, path) in PACKET_READS.items():
        rest = sorted({call for m in methods[tool] for call in calls[m]})
        assert rest == [f"{method} {path}"], (tool, rest)


def paging_styles(document: dict[str, Any]) -> dict[str, tuple[frozenset, frozenset]]:
    """Each operation that pages: its continuation parameters and fields."""
    schemas = document["components"]["schemas"]

    def fields(schema: dict[str, Any]) -> set[str]:
        while "$ref" in schema:
            schema = schemas[schema["$ref"].rsplit("/", 1)[1]]
        found = set(schema.get("properties") or {})
        for branch in schema.get("anyOf") or schema.get("oneOf") or []:
            found |= fields(branch)
        return found

    styles = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            sent = frozenset(
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "query"
                and parameter["name"] in CONTINUATION_PARAMS
            )
            said = frozenset(
                name
                for name in fields(_ok_schema(operation))
                if name in CONTINUATION_FIELDS
            )
            if sent or said:
                styles[f"{method.upper()} {path}"] = (sent, said)
    return styles


def paging_violations(document: dict[str, Any]) -> list[str]:
    by_signature = {signature: name for name, signature in PAGING_STYLES.items()}
    used: dict[str, set[str]] = {name: set() for name in PAGING_STYLES}
    problems = []
    rule = (
        "a new operation pages in D2's vocabulary only: an opaque `cursor` in, "
        "`next_cursor` and `has_more` out (docs/AGENT_GUIDE.md, Conventions)"
    )
    for operation, (sent, said) in sorted(paging_styles(document).items()):
        style = by_signature.get((sent, said))
        if style is None:
            problems.append(
                f"{operation} pages with {sorted(sent)} in and {sorted(said)} out, "
                f"a style that does not exist; {rule}"
            )
            continue
        used[style].add(operation)
    for style, operations in used.items():
        if style == D2:
            continue
        for operation in sorted(operations - PAGING_OPERATIONS[style]):
            problems.append(f"{operation} pages as {style!r}; {rule}")
        for operation in sorted(PAGING_OPERATIONS[style] - operations):
            problems.append(f"{operation} no longer pages as {style!r}: drop it here")
    return problems


def test_paging_styles_are_the_frozen_list_and_new_operations_use_d2(agent_document):
    assert paging_violations(agent_document) == []
    legacy = [name for name, operations in PAGING_OPERATIONS.items() if operations]
    assert len(legacy) == 6, legacy  # the styles that exist, each still in use


def test_the_paging_check_refuses_a_new_style_and_a_new_legacy_caller(agent_document):
    document = copy.deepcopy(agent_document)
    listing = {
        "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
    }

    def operation(path: str, *names: str, answer: dict | None = None) -> None:
        document["paths"][path] = {
            "get": {
                **listing,
                "parameters": [{"name": n, "in": "query"} for n in names],
                **(
                    {
                        "responses": {
                            "200": {"content": {"application/json": {"schema": answer}}}
                        }
                    }
                    if answer
                    else {}
                ),
            }
        }

    operation("/widgets", "page", "limit")
    operation("/gadgets", "offset", "limit")
    operation(
        "/gizmos",
        "cursor",
        answer={"properties": {"items": {}, "next_cursor": {}, "has_more": {}}},
    )
    problems = paging_violations(document)
    assert len(problems) == 2, problems
    assert all("D2's vocabulary" in problem for problem in problems)
    assert any(problem.startswith("GET /widgets") for problem in problems)
    assert any(problem.startswith("GET /gadgets") for problem in problems)


def test_a_packet_timestamp_sent_back_is_compared_as_the_instant_it_names(tmp_path):
    """The one parameter that takes a timestamp, `since` on the admin security
    reads, compares it with stored text as a string. A packet gives
    `2026-11-21T18:19:13Z`; compared raw, the `T` sorts after the space of
    every stored time that day, so the read answered with nothing. The edge
    turns the packet shape into the stored one."""
    assert packet_time.stored("2026-11-21T18:19:13Z") == "2026-11-21 18:19:13"
    assert packet_time.stored("2026-11-21 18:19:13") == "2026-11-21 18:19:13"
    assert packet_time.stored("2026-11-21T18:19:13.250+00:00") == "2026-11-21 18:19:13"
    assert packet_time.stored("yesterday") == "yesterday"
    assert packet_time.stored(None) is None

    app, _db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        # A failed login for the first user: one security event.
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "wrong"},
            follow_redirects=False,
        )
        listed = client.get("/security/events", headers=ADMIN).json()
        assert listed, "the failed login is a security event"
        when = listed[0]["created_at"]
        as_a_packet_gives_it = packet_time.rfc3339(when)
        assert as_a_packet_gives_it != when
        for since in (when, as_a_packet_gives_it):
            rows = client.get(
                "/security/events", params={"since": since}, headers=ADMIN
            ).json()
            assert [row["id"] for row in rows] == [row["id"] for row in listed], since
            counts = client.get(
                "/security/counts", params={"since": since}, headers=ADMIN
            ).json()
            assert counts["login_failed"] >= 1, (since, counts)
