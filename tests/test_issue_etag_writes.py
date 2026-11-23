"""Every write that changes an issue's ETag returns the new one (C9, FLEET-6).

WHY: ``PUT /issues/{id}/parent`` changed the issue's ETag and answered none, so
the conductor recipe read a 5.9 KB work-context packet after each parent write
only to obtain the tag its next guarded write needed. Archive, unarchive and the
label writes did the same. Now each answers the new tag, and this test derives
the list of such writes from the code, so a new one cannot be added without it.

How the list is derived:

1. **What an ETag covers.** An issue's ETag (``aegis/issue_etags``) is a hash
   of its public representation: the issue row's columns and its labels. So the
   writers that can change it are the functions whose SQL inserts, updates or
   deletes rows of ``issues`` or ``issue_labels``. They are found by reading
   every module under ``src/athena``; an ``UPDATE issues`` whose static column
   list names no represented column (``room_id``) is not one.
2. **Which routes reach them.** Every agent-facing write route
   (``agent_openapi.classified_routes``) whose endpoint or dependencies call one
   of those writers, following resolved calls as the agent OpenAPI document's
   own code reader does. The undo engine dispatches through a registry, so the
   undo route's callees include every registered compensator.
3. **How each returns the tag.** A route that answers one issue must set the
   ``ETag`` header (``agent_openapi.sets_etag``, which also declares it in the
   agent document). A route that writes several issues names each one's tag in
   its body; those are listed in ``BODY_ETAGS`` and each is exercised below.
4. **The MCP tools.** A write tool whose client calls one of those routes must
   return the client's answer unwrapped: the client puts the ``ETag`` header in
   ``_etag`` and a wrapper such as ``listing()`` would drop it.

Not in the list, by construction: a write to another resource that changes the
representation of many issues at once — renaming a project, a label or an
account, recolouring or deleting a label. It answers that resource, not an
issue, and the issues' tags move as a side effect; a guarded write on one of
them refuses 412 with the current tag, as it always has.
"""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
import re
import sys
import textwrap
from typing import Any, Callable

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    bearer,
    bootstrap,
    make_app,
    mcp_server_for,
    mcp_sessions,
    onboard_agent,
    run_agents,
)

from athena import agent_openapi
from athena.core import undo
from athena.main import AGENT_TRANSPORT, create_app

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "athena"
sys.path.insert(0, str(REPO / "scripts"))
import generate_agent_api  # noqa: E402

# The columns an issue's representation is built from (issue_etags._ISSUE_FIELDS,
# with key, project_name and assignee_name read through their source columns).
REPRESENTED = {
    "title",
    "body",
    "status",
    "priority",
    "created_by",
    "created_at",
    "assignee_id",
    "project_id",
    "project_seq",
    "parent_id",
    "sprint_id",
    "archived_at",
}
_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(issues|issue_labels)\b",
    re.IGNORECASE,
)
_SET = re.compile(r"UPDATE\s+issues\s+SET\s+(.*?)\s+WHERE", re.IGNORECASE | re.DOTALL)
_DEPTH = 8

# The writes that change several issues, and where each answers their tags.
BODY_ETAGS = {
    ("POST", "/issues/bulk"): "results[].etag on each applied row",
    ("POST", "/pages/{page_id}/start-playbook"): "parent.etag and children[].etag",
    ("POST", "/activity/{event_id}/undo"): "target_etag",
}
_TEMPLATE = re.compile(r"\{[^}]+\}")


def _sql_text(function: ast.FunctionDef) -> str:
    parts: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.append(node.value)
        elif isinstance(node, ast.FormattedValue):
            parts.append("{}")  # a column list built at run time counts
    return " ".join(parts)


def _changes_representation(sql: str) -> bool:
    if any(table == "issue_labels" for table in _WRITE.findall(sql)):
        return True
    if re.search(r"\b(INSERT|DELETE)\b", sql, re.IGNORECASE):
        return True
    for assignments in _SET.findall(sql):
        if "{}" in assignments:
            return True
        columns = {part.split("=")[0].strip() for part in assignments.split(",")}
        if columns & REPRESENTED:
            return True
    return False


def etag_writers() -> set[Callable[..., Any]]:
    """Every function whose own SQL changes an issue's representation."""
    found: set[Callable[..., Any]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            sql = _sql_text(node)
            if _WRITE.search(sql) and _changes_representation(sql):
                found.add(getattr(importlib.import_module(module), node.name))
    return found


def _dispatch() -> dict[Callable[..., Any], list[Callable[..., Any]]]:
    """Calls the code reader cannot resolve by name: registry dispatch."""
    compensators = [
        entry.compensator
        for entry in undo._REGISTRY.values()  # noqa: SLF001 — the registry is the dispatch
        if entry.compensator is not None
    ]
    return {undo.undo: compensators}


def _reaches(
    reader: agent_openapi.CodeReader,
    root: Callable[..., Any],
    writers: set[Callable[..., Any]],
) -> bool:
    dispatch = _dispatch()
    frontier, seen = [root], set()
    for _ in range(_DEPTH + 1):
        following: list[Callable[..., Any]] = []
        for function in frontier:
            if function in seen:
                continue
            seen.add(function)
            if function in writers:
                return True
            following += reader.callees(function)
            following += dispatch.get(function, [])
        frontier = following
    return False


def etag_changing_routes(app) -> dict[tuple[str, str], Any]:
    """``{(verb, path): route}`` for every agent write that reaches a writer."""
    writers = etag_writers()
    reader = agent_openapi.CodeReader()
    found: dict[tuple[str, str], Any] = {}
    for route, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT):
        if kind != agent_openapi.AGENT or not route.include_in_schema:
            continue
        verbs = set(route.methods or ()) - {"GET", "HEAD", "OPTIONS"}
        if not verbs:
            continue
        roots = agent_openapi._code_roots(route)  # noqa: SLF001 — the document's own roots
        if any(_reaches(reader, root, writers) for root in roots):
            for verb in verbs:
                found[(verb, route.path_format)] = route
    return found


@pytest.fixture(scope="module")
def derived(tmp_path_factory):
    app = create_app(tmp_path_factory.mktemp("etag") / "walk.db")
    return app, etag_changing_routes(app)


def test_the_writers_are_found_from_the_sql(derived):
    """The derivation is real: it finds the data-layer writers the issue commands
    use, and leaves out the room write, whose column the ETag does not cover."""
    names = {f"{f.__module__}.{f.__name__}" for f in etag_writers()}
    assert {
        "athena.aegis.issues.create_issue",
        "athena.aegis.issues.update_issue",
        "athena.aegis.issues.set_assignee",
        "athena.aegis.issues.set_parent",
        "athena.aegis.issues.set_sprint",
        "athena.aegis.issues.set_project",
        "athena.aegis.issues.set_archived",
        "athena.core.labels.add_label_to_issue",
        "athena.core.labels.remove_label_from_issue",
    } <= names
    assert "athena.aegis.rooms.set_issue_room" not in names


def test_every_write_that_changes_an_issue_etag_returns_it(derived):
    app, routes = derived
    reader = agent_openapi.CodeReader()
    # The walk is real: these are the writes the reviewer and this change named.
    assert {
        ("POST", "/issues"),
        ("PATCH", "/issues/{issue_id}"),
        ("PUT", "/issues/{issue_id}/assignee"),
        ("PUT", "/issues/{issue_id}/parent"),
        ("POST", "/issues/{issue_id}/archive"),
        ("POST", "/issues/{issue_id}/labels"),
        ("DELETE", "/issues/{issue_id}/labels/{label_id}"),
        *BODY_ETAGS,
    } <= set(routes)
    silent = sorted(
        f"{verb} {path}"
        for (verb, path), route in routes.items()
        if (verb, path) not in BODY_ETAGS and not agent_openapi.sets_etag(route, reader)
    )
    assert silent == [], (
        "these writes change an issue's ETag and do not answer it: set the ETag "
        "header (aegis/api._tagged_issue), or, for a write of several issues, "
        "name each one's tag in the body and list it in BODY_ETAGS"
    )
    assert set(BODY_ETAGS) <= set(routes), "a BODY_ETAGS entry no longer writes"


def _shape(path: str) -> str:
    return _TEMPLATE.sub("{}", path.split("?")[0])


def _returns_client_answer(tool: str) -> bool:
    """The tool body returns ``client.<method>(...)`` itself, unwrapped."""
    for module in generate_agent_api.TOOL_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == tool:
                returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
                return bool(returns) and all(
                    isinstance(r.value, ast.Call)
                    and isinstance(r.value.func, ast.Attribute)
                    and isinstance(r.value.func.value, ast.Name)
                    and r.value.func.value.id == "client"
                    for r in returns
                )
    raise AssertionError(f"no tool {tool}")


def test_every_mcp_tool_over_such_a_write_passes_the_etag_on(derived):
    _app, routes = derived
    shapes = {(verb, _shape(path)) for verb, path in routes}
    calls = generate_agent_api.client_method_calls()
    tools = {
        tool
        for tool, methods in generate_agent_api.tool_client_methods().items()
        for method in methods
        for call in calls.get(method, [])
        # link_mention picks its base from the source kind at run time.
        for base in ("issues", "pages")
        if (
            call.split(" ", 1)[0],
            _shape(call.split(" ", 1)[1].replace("{base}", base)),
        )
        in shapes
    }
    assert {
        "create_issue",
        "set_issue_parent",
        "assign_issue",
        "archive_issue",
    } <= tools
    wrapped = sorted(tool for tool in tools if not _returns_client_answer(tool))
    assert wrapped == [], "these tools drop the client's _etag"


# --- the tags are the issue's current ones ----------------------------------------


def _tag(client: TestClient, issue_id: int) -> str:
    return client.get(f"/issues/{issue_id}", headers=ADMIN).headers["ETag"]


@pytest.fixture
def board(tmp_path):
    app, _db = make_app(tmp_path, "tags.db")
    with TestClient(app) as client:
        bootstrap(client)
        project = client.post(
            "/projects", json={"name": "P", "key": "PP"}, headers=ADMIN
        ).json()["id"]

        def issue(title: str) -> int:
            made = client.post(
                "/issues", json={"title": title, "project_id": project}, headers=ADMIN
            )
            assert made.status_code == 201, made.text
            return made.json()["id"]

        yield client, issue, project


def test_each_single_issue_write_answers_the_issue_current_etag(board):
    client, issue, _project = board
    parent, child = issue("parent"), issue("child")
    label = client.post("/labels", json={"name": "ready"}, headers=ADMIN).json()["id"]
    for method, path, body in (
        ("put", f"/issues/{child}/parent", {"parent_id": parent}),
        ("post", f"/issues/{child}/labels", {"label_id": label}),
        ("delete", f"/issues/{child}/labels/{label}", None),
        ("post", f"/issues/{child}/archive", None),
        ("post", f"/issues/{child}/unarchive", None),
        ("put", f"/issues/{child}/parent", {"parent_id": None}),
    ):
        before = _tag(client, child)
        kwargs = {"json": body} if body is not None else {}
        answer = getattr(client, method)(path, headers=ADMIN, **kwargs)
        assert answer.status_code in (200, 201), (path, answer.text)
        assert answer.headers.get("ETag") == _tag(client, child) != before, path


def test_bulk_names_each_applied_issue_tag(board):
    client, issue, _project = board
    first, second = issue("a"), issue("b")
    answer = client.post(
        "/issues/bulk",
        json={"ids": [first, second, 999], "priority": "high"},
        headers=ADMIN,
    )
    assert answer.status_code == 200, answer.text
    tags = {row["id"]: row.get("etag") for row in answer.json()["results"]}
    assert tags == {
        first: _tag(client, first),
        second: _tag(client, second),
        999: None,
    }


def test_starting_a_playbook_names_each_created_issue_tag(board):
    client, _issue, project = board
    space = client.post(
        "/spaces", json={"name": "Runbooks", "key": "RUN"}, headers=ADMIN
    ).json()["id"]
    page = client.post(
        f"/spaces/{space}/pages",
        json={"title": "Release", "body": "- [ ] build\n  - [ ] test\n- [ ] ship"},
        headers=ADMIN,
    ).json()["id"]
    label = client.post("/labels", json={"name": "playbook"}, headers=ADMIN).json()[
        "id"
    ]
    attached = client.post(
        f"/pages/{page}/labels", json={"label_id": label}, headers=ADMIN
    )
    assert attached.status_code in (200, 201), attached.text
    started = client.post(
        f"/pages/{page}/start-playbook", json={"project_id": project}, headers=ADMIN
    )
    assert started.status_code == 201, started.text
    rows = [started.json()["parent"], *started.json()["children"]]
    assert len(rows) == 4
    assert {row["id"]: row["etag"] for row in rows} == {
        row["id"]: _tag(client, row["id"]) for row in rows
    }


def test_undo_names_the_reversed_issue_tag(board):
    client, issue, _project = board
    target = issue("x")
    client.put(f"/issues/{target}/assignee", json={"assignee_id": 1}, headers=ADMIN)
    trail = client.get(f"/activity?target_kind=issue&target_id={target}", headers=ADMIN)
    assert trail.status_code == 200, trail.text
    (assigned,) = [row for row in trail.json() if row["verb"] == "assigned"]
    undone = client.post(f"/activity/{assigned['id']}/undo", headers=ADMIN)
    assert undone.status_code == 201, undone.text
    assert undone.json()["target_etag"] == _tag(client, target)


def test_the_set_issue_parent_tool_answers_the_tag_a_guarded_write_takes(tmp_path):
    """Over MCP the parent write's answer carries ``_etag``, and that tag is the
    one ``assign_issue`` needs: no work-context read in between."""
    app, _db = make_app(tmp_path, "mcp.db")
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, email="c@e.com", name="Cora")
        parent, child = (
            client.post("/issues", json={"title": t}, headers=bearer(agent)).json()[
                "id"
            ]
            for t in ("parent", "child")
        )

        async def scene() -> tuple[dict, dict]:
            server = mcp_server_for(app, agent, run_id="etag-run", profile="conductor")
            async with mcp_sessions(server) as (session,):

                async def call(tool: str, **arguments: Any) -> dict:
                    answer = await session.call_tool(tool, arguments)
                    assert not answer.isError, answer.content
                    return json.loads(answer.content[0].text)

                nested = await call(
                    "set_issue_parent", issue_id=child, parent_id=parent
                )
                assigned = await call(
                    "assign_issue",
                    issue_id=child,
                    assignee_id=agent["user"]["id"],
                    if_match=nested["_etag"],
                )
                return nested, assigned

        nested, assigned = run_agents(scene())
        assert nested["parent_id"] == parent
        assert assigned["assignee_id"] == agent["user"]["id"]
        assert assigned["_etag"] == _tag(client, child)


def test_the_derivation_reads_function_bodies():
    """Guard the reader itself: a function that writes ``issues`` through a
    dynamic column list is a writer, one that sets only ``room_id`` is not."""
    dynamic = ast.parse(
        textwrap.dedent(
            """
            def f(conn, cols):
                conn.execute(f"UPDATE issues SET {cols} WHERE id = ?")
            """
        )
    ).body[0]
    room = ast.parse(
        textwrap.dedent(
            """
            def g(conn):
                conn.execute("UPDATE issues SET room_id = ? WHERE id = ?")
            """
        )
    ).body[0]
    assert isinstance(dynamic, ast.FunctionDef) and isinstance(room, ast.FunctionDef)
    assert _changes_representation(_sql_text(dynamic))
    assert not _changes_representation(_sql_text(room))
