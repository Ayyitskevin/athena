"""One request sets up a slice: create_issue takes its parent, owner, workers and
blockers (C9, FLEET-6).

WHY: a conductor that set up one outcome and three slices made 21 requests: a
create, then a parent, an ETag read, an assignment and a delegation per slice,
and two links. ``POST /issues`` (and the ``create_issue`` tool over it) now
accepts ``parent_id``, ``assignee_id``, ``delegate_ids`` and ``blocked_by`` and
applies them in the create's one transaction, through the same commands the
separate writes use. These tests hold what that must keep:

- each act is checked exactly as its own command checks it, and records the
  same events: the issue set up in one request has the trail of the same issue
  set up in five;
- if any part is refused, nothing is created: no issue, no event, no
  notification, no contributor and no link;
- a hidden parent, blocker or project reads as a missing one;
- the body stays closed, and the answer carries the issue's ETag after all of
  it, so the next guarded write needs no read.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sqlite3
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, add_user, as_user, bootstrap, make_app, onboard_agent

from athena.core import db

MEMBER = as_user(2)


def _count(db_file: Path, table: str) -> int:
    conn = db.connect(db_file)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _footprint(db_file: Path) -> dict[str, int]:
    """Every table a composed create writes: the issue and each sub-act's rows."""
    return {
        table: _count(db_file, table)
        for table in (
            "issues",
            "activity",
            "notifications",
            "issue_contributors",
            "issue_links",
            "watches",
        )
    }


@pytest.fixture
def board(tmp_path) -> Any:
    app, db_file = make_app(tmp_path, "compose.db")
    with TestClient(app) as client:
        bootstrap(client)  # 1, the admin
        add_user(client, "m@e.com", name="Member")  # 2, the conductor here
        add_user(client, "h@e.com", name="Human")  # 3, a human (not an agent)
        agent = onboard_agent(client, email="w@e.com", name="Worker")["user"]
        project = client.post(
            "/projects", json={"name": "Open", "key": "OPN"}, headers=ADMIN
        ).json()
        private = client.post(
            "/projects",
            json={"name": "Private", "key": "PRV", "visibility": "private"},
            headers=ADMIN,
        ).json()
        hidden = client.post(
            "/issues",
            json={"title": "Hidden", "project_id": private["id"]},
            headers=ADMIN,
        ).json()
        yield {
            "client": client,
            "db_file": db_file,
            "agent": agent,
            "project": project["id"],
            "hidden_issue": hidden["id"],
        }


def _create(client: TestClient, **body: Any):
    return client.post("/issues", json=body, headers=MEMBER)


def _trail(db_file: Path, issue_id: int) -> Counter:
    """The events on an issue, by verb and actor, ignoring ids and order."""
    conn = db.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT verb, actor_id FROM activity WHERE target_kind = 'issue' "
            "AND target_id = ?",
            (issue_id,),
        ).fetchall()
    finally:
        conn.close()
    return Counter((row["verb"], row["actor_id"]) for row in rows)


def test_one_request_sets_up_a_slice_as_five_requests_do(board):
    client, db_file, agent = board["client"], board["db_file"], board["agent"]
    project = board["project"]
    outcome = _create(client, title="Outcome", project_id=project).json()
    blocker = _create(client, title="Design", project_id=project).json()

    # The long way: create, parent, assign, delegate, link.
    long_way = _create(client, title="Build A", project_id=project).json()
    issue_id = long_way["id"]
    for method, path, body in (
        ("put", "parent", {"parent_id": outcome["id"]}),
        ("put", "assignee", {"assignee_id": 2}),
        ("post", "delegate", {"user_id": agent["id"]}),
        ("post", "links", {"target_ref": str(blocker["id"]), "relation": "blocked_by"}),
    ):
        answer = getattr(client, method)(
            f"/issues/{issue_id}/{path}", json=body, headers=MEMBER
        )
        assert answer.status_code in (200, 201), answer.text

    # The one request.
    made = _create(
        client,
        title="Build B",
        project_id=project,
        parent_id=outcome["id"],
        assignee_id=2,
        delegate_ids=[agent["id"]],
        blocked_by=[blocker["id"]],
    )
    assert made.status_code == 201, made.text
    issue = made.json()
    assert issue["parent_id"] == outcome["id"] and issue["assignee_id"] == 2

    # The answer's ETag is the issue's after every part, so a guarded write
    # needs no read first.
    current = client.get(f"/issues/{issue['id']}")
    assert made.headers["ETag"] == current.headers["ETag"]

    contributors = client.get(f"/issues/{issue['id']}/contributors").json()
    assert [row["user_id"] for row in contributors] == [agent["id"]]
    links = client.get(f"/issues/{issue['id']}/links").json()
    assert [row["id"] for row in links["blocked_by"]] == [blocker["id"]]
    children = client.get(f"/issues/{outcome['id']}/children").json()
    assert {row["id"] for row in children} == {issue_id, issue["id"]}

    # Each act recorded the events its own command records, no more and no fewer.
    assert _trail(db_file, issue["id"]) == _trail(db_file, issue_id)


def _hidden_parent(board):
    return {"parent_id": board["hidden_issue"]}


REFUSALS = {
    # name: (body, status, detail)
    "missing parent": ({"parent_id": 9999}, 422, "no such parent issue"),
    "hidden parent": (_hidden_parent, 422, "no such parent issue"),
    "missing assignee": ({"assignee_id": 9999}, 422, "no such user"),
    # Delegation answers a missing id and a human alike (F9).
    "missing delegate": (
        {"delegate_ids": [9999]},
        422,
        "no agent account with that id",
    ),
    "human delegate": (
        {"delegate_ids": [3]},
        422,
        "no agent account with that id",
    ),
    "missing blocker": ({"blocked_by": [9999]}, 422, "no such target issue"),
    "hidden blocker": (
        lambda board: {"blocked_by": [board["hidden_issue"]]},
        422,
        "no such target issue",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_refused_part_leaves_nothing_created(board, case):
    """Every other part is valid and would have been applied before the refused
    one: the parent, the owner and a delegation land first, the refused act
    comes after them, and still nothing survives."""
    client, db_file, agent = board["client"], board["db_file"], board["agent"]
    project = board["project"]
    outcome = _create(client, title="Outcome", project_id=project).json()
    blocker = _create(client, title="Design", project_id=project).json()
    refused, status, detail = REFUSALS[case]
    refused = refused(board) if callable(refused) else refused
    body = {
        "title": "Slice",
        "project_id": project,
        "parent_id": outcome["id"],
        "assignee_id": 2,
        "delegate_ids": [agent["id"]],
        "blocked_by": [blocker["id"]],
    }
    if "delegate_ids" in refused:
        body["delegate_ids"] = [agent["id"], *refused["delegate_ids"]]
    elif "blocked_by" in refused:
        body["blocked_by"] = [blocker["id"], *refused["blocked_by"]]
    else:
        body.update(refused)
    before = _footprint(db_file)

    answer = client.post("/issues", json=body, headers=MEMBER)

    assert (answer.status_code, answer.json()["detail"]) == (status, detail), (
        answer.text
    )
    assert answer.json()["code"] == "validation_error"
    assert _footprint(db_file) == before


def test_a_hidden_parent_reads_as_a_missing_one(board):
    client = board["client"]
    hidden = _create(client, title="x", parent_id=board["hidden_issue"])
    missing = _create(client, title="x", parent_id=board["hidden_issue"] + 1000)
    assert hidden.status_code == missing.status_code == 422
    assert hidden.json() == missing.json()


def test_unknown_fields_are_still_refused(board):
    answer = _create(board["client"], title="x", delegates=[board["agent"]["id"]])
    assert answer.status_code == 422, answer.text
    assert _count(board["db_file"], "issues") == 1  # only the fixture's hidden issue


def test_a_create_without_the_new_fields_is_unchanged(board):
    client, db_file = board["client"], board["db_file"]
    made = _create(client, title="Plain", project_id=board["project"])
    assert made.status_code == 201, made.text
    issue = made.json()
    assert issue["parent_id"] is None and issue["assignee_id"] is None
    assert _trail(db_file, issue["id"]) == Counter({("created", 2): 1})


def test_a_viewer_still_cannot_create_with_parts(board):
    client = board["client"]
    add_user(client, "v@e.com", name="Viewer", role="viewer")  # 5
    answer = client.post(
        "/issues", json={"title": "x", "assignee_id": 2}, headers=as_user(5)
    )
    assert answer.status_code == 403, answer.text


def test_the_composition_runs_in_one_transaction(board, monkeypatch):
    """A failure after the issue row is written rolls the row back: the parts run
    inside the create's transaction, not after a commit."""
    from athena.aegis import issue_commands

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(issue_commands, "link_issues", explode)
    client, db_file = board["client"], board["db_file"]
    blocker = _create(client, title="Design", project_id=board["project"]).json()
    before = _footprint(db_file)
    with pytest.raises(sqlite3.OperationalError):
        client.post(
            "/issues",
            json={"title": "x", "assignee_id": 2, "blocked_by": [blocker["id"]]},
            headers=MEMBER,
        )
    assert _footprint(db_file) == before
