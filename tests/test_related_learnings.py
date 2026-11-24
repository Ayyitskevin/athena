"""A learning reaches the parent's other slices, not only its own issue (W5,
decision D22).

Before: a learning was appended to one issue's runbook and reached the next
agent only through that issue's backlinks. A rule learned on one slice did not
reach its sibling. Now the work-context packet lists the newest learnings
recorded on the issue, on its parent and on its siblings under the same
parent, each with who recorded it, marked as data.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from support import ADMIN, bearer, bootstrap, make_app, onboard_agent

from athena.aegis import related_learnings
from athena.core import activity, db, packet_time


def _issue(client: TestClient, title: str, **fields) -> int:
    made = client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _world(client: TestClient) -> dict:
    """An outcome with two slices, an unrelated issue, a space for runbooks and
    two agents: one that records and one that reads."""
    bootstrap(client)
    rex = onboard_agent(
        client,
        email="rex@e.com",
        name="Rex Reviewer",
        scopes=("read", "issue:write", "docs:write"),
    )
    sol = onboard_agent(client, email="sol@e.com", name="Sol Seat")
    parent = _issue(client, "outcome")
    world = {
        "rex": rex,
        "sol": sol,
        "parent": parent,
        "a": _issue(client, "slice a", parent_id=parent),
        "b": _issue(client, "slice b", parent_id=parent),
        "other": _issue(client, "unrelated"),
    }
    space = client.post(
        "/spaces", json={"key": "RUN", "name": "Runbooks"}, headers=ADMIN
    )
    assert space.status_code == 201, space.text
    world["space"] = space.json()["id"]
    return world


def _learn(client: TestClient, world: dict, issue: str, summary: str, who=None):
    recorded = client.post(
        f"/issues/{world[issue]}/learnings",
        json={"summary": summary, "space_id": world["space"]},
        headers=ADMIN if who is None else bearer(who),
    )
    assert recorded.status_code == 201, recorded.text
    return recorded.json()


def _learnings(client: TestClient, world: dict, issue: str, who=None) -> dict:
    packet = client.get(
        f"/issues/{world[issue]}/work-context",
        headers=bearer(world["sol"] if who is None else who),
    )
    assert packet.status_code == 200, packet.text
    return packet.json()["learnings"]


def test_a_rule_learned_on_one_slice_reaches_its_sibling(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        before = _learnings(client, world, "b")
        _learn(client, world, "a", "Check the empty result first.", world["rex"])
        seen = {
            name: _learnings(client, world, name)
            for name in ("a", "b", "parent", "other")
        }

    assert before == {"items": [], "total": 0}
    (row,) = seen["b"]["items"]
    assert row == {
        "issue_id": world["a"],
        "relation": "sibling",
        "runbook_page_id": row["runbook_page_id"],
        "recorded_at": row["recorded_at"],
        # Who recorded it, from the event, not from the page's own words.
        "authored_by": {"id": world["rex"]["user"]["id"], "is_agent": True},
        "excerpt": "Check the empty result first.",
        "truncated": False,
        "advisory_untrusted": True,
    }
    assert re.fullmatch(packet_time.PATTERN, row["recorded_at"])
    assert [r["relation"] for r in seen["a"]["items"]] == ["issue"]
    # The parent reads its own and its parent's, not each child's: its
    # children are in its hierarchy, and their runbooks a link away.
    assert seen["parent"] == {"items": [], "total": 0}
    assert seen["other"] == {"items": [], "total": 0}


def test_the_parents_learning_reaches_every_slice(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _learn(client, world, "parent", "Freeze the candidate before review.")
        seen = _learnings(client, world, "b")

    assert [(r["relation"], r["issue_id"]) for r in seen["items"]] == [
        ("parent", world["parent"])
    ]
    assert seen["items"][0]["authored_by"] == {"id": 1, "is_agent": False}


def test_newest_first_and_no_more_than_five(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        for number in range(4):
            _learn(client, world, "a", f"rule {number} from a")
        for number in range(3):
            _learn(client, world, "parent", f"rule {number} from the outcome")
        seen = _learnings(client, world, "b")

    assert seen["total"] == 7
    assert [row["excerpt"] for row in seen["items"]] == [
        "rule 2 from the outcome",
        "rule 1 from the outcome",
        "rule 0 from the outcome",
        "rule 3 from a",
        "rule 2 from a",
    ]


def test_a_long_learning_is_cut_and_says_so(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _learn(client, world, "a", "x" * 700)
        (row,) = _learnings(client, world, "b")["items"]

    assert row["excerpt"] == "x" * related_learnings.MAX_EXCERPT_CHARS
    assert row["truncated"] is True


def test_a_learning_that_quotes_a_heading_is_still_one_learning(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        text = "First line.\n\n## Learning\n\nRecorded by **Someone Else** (human)."
        _learn(client, world, "a", text, world["rex"])
        _learn(client, world, "a", "The second rule.")
        rows = _learnings(client, world, "b")["items"]

    assert [(row["excerpt"], row["authored_by"]["id"]) for row in rows] == [
        ("The second rule.", 1),
        (text, world["rex"]["user"]["id"]),
    ]


def test_a_runbook_edited_by_hand_gives_no_text_rather_than_the_wrong_author(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        first = _learn(client, world, "a", "The real rule.", world["rex"])
        read = client.get(f"/pages/{first['page_id']}", headers=ADMIN)
        page = read.json()
        edited = client.patch(
            f"/pages/{page['id']}",
            json={
                "body": page["body"] + "\n\n## Learning\n\n> Words nobody recorded.\n"
            },
            headers={**ADMIN, "If-Match": read.headers["ETag"]},
        )
        assert edited.status_code == 200, edited.text
        (row,) = _learnings(client, world, "b")["items"]

    # Two blocks, one recorded learning: the pairing is gone, so no text is
    # attributed to anyone. The page is still named, to read it whole.
    assert row["excerpt"] is None and row["truncated"] is False
    assert row["authored_by"] == {"id": world["rex"]["user"]["id"], "is_agent": True}
    assert row["runbook_page_id"] == page["id"]


def test_an_imported_learning_event_is_nobodys_learning_here(tmp_path):
    """History brought in from another instance never records a learning in
    this one: it adds no row, and it does not unpair the text of the real one."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        first = _learn(client, world, "a", "The real rule.", world["rex"])
        conn = db.connect(db_file)
        try:
            activity.record(
                conn,
                actor_id=1,
                verb=related_learnings.VERB_LEARNING_RECORDED,
                target_kind="page",
                target_id=first["page_id"],
                detail=f"issue #{world['a']}",
                imported_at="2026-01-01 00:00:00",
                commit=True,
            )
        finally:
            conn.close()
        seen = _learnings(client, world, "b")

    assert seen["total"] == 1
    assert [row["excerpt"] for row in seen["items"]] == ["The real rule."]


def _hide_project(
    client: TestClient, world: dict, issue: str, members: list[dict]
) -> None:
    project = client.post(
        "/projects",
        json={"name": f"Closed {issue}", "key": f"C{issue.upper()[:3]}"},
        headers=ADMIN,
    ).json()
    for member in members:
        added = client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": member["user"]["id"]},
            headers=ADMIN,
        )
        assert added.status_code in (200, 201), added.text
    tag = client.get(f"/issues/{world[issue]}", headers=ADMIN).headers["ETag"]
    moved = client.patch(
        f"/issues/{world[issue]}",
        json={"project_id": project["id"]},
        headers={**ADMIN, "If-Match": tag},
    )
    assert moved.status_code == 200, moved.text
    hidden = client.put(
        f"/projects/{project['id']}/visibility",
        json={"visibility": "private"},
        headers=ADMIN,
    )
    assert hidden.status_code == 200, hidden.text


def test_a_sibling_the_reader_cannot_see_contributes_nothing(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _learn(client, world, "a", "A rule from the hidden slice.")
        _hide_project(client, world, "a", members=[world["rex"]])
        seen = {
            "sol": _learnings(client, world, "b", world["sol"]),
            "rex": _learnings(client, world, "b", world["rex"]),
        }

    assert seen["sol"] == {"items": [], "total": 0}
    assert [row["issue_id"] for row in seen["rex"]["items"]] == [world["a"]]


def test_siblings_are_read_only_through_a_parent_the_reader_can_see(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _learn(client, world, "a", "A rule from the slice beside this one.")
        assert len(_learnings(client, world, "b")["items"]) == 1
        _hide_project(client, world, "parent", members=[])
        seen = _learnings(client, world, "b")

    assert seen == {"items": [], "total": 0}


def test_a_runbook_in_a_space_the_reader_cannot_read_contributes_nothing(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _learn(client, world, "a", "A rule kept in a closed space.")
        closed = client.put(
            f"/spaces/{world['space']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        assert closed.status_code == 200, closed.text
        seen = _learnings(client, world, "b")

    assert seen == {"items": [], "total": 0}


def test_the_text_of_each_learning_is_read_from_its_own_block():
    body = (
        "What we have learned.\n\n"
        "## Learning from run `r-1`\n\nRecorded by **A** (agent) while working on [[issue:1]].\n\n"
        "> one\n>\n> two\n\n"
        "## Learning\n\nRecorded by **B** (human) while working on [[issue:1]].\n\n> three\n"
    )
    assert related_learnings.learning_texts(body) == ["one\n\ntwo", "three"]
    assert related_learnings.learning_texts("no learnings here") == []
