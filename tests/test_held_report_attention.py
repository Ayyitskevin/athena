"""A completion that reports HOLD or BLOCKED asks whoever answers for the
work (F15, FLEET-11).

Before: the attention vocabulary had no signal for a verdict. A seat that
completed with HOLD left an issue with no lease and nothing asking for anyone;
the conductor that delegated it and the operator learned of it only if they
were reading the feed. Now the report is a ranked row for the issue's assignee,
the account that delegated it and the operator, until one of them acts.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, bearer, bootstrap, login, make_app, onboard_agent

from athena.aegis import fleet_attention, held_reports
from athena.core import db

SIGNAL = "held_report"


def _rows(client: TestClient, who) -> list[dict]:
    headers = who if who is ADMIN else bearer(who)
    ranking = client.get(
        "/attention/ranking", params={"signals": SIGNAL}, headers=headers
    )
    assert ranking.status_code == 200, ranking.text
    return ranking.json()["items"]


def _count(db_file) -> int:
    conn = db.connect(db_file)
    try:
        counts = fleet_attention.needs_you(conn)
    finally:
        conn.close()
    (signal,) = [s for s in counts["signals"] if s["key"] == "held_reports"]
    return signal["count"]


def _claim(client: TestClient, agent: dict, issue_id: int) -> str:
    tag = client.get(f"/issues/{issue_id}", headers=bearer(agent)).headers["ETag"]
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        json={},
        headers={**bearer(agent), "If-Match": tag},
    )
    assert claimed.status_code in (200, 201), claimed.text
    return claimed.json()["generation"]


def _report(client: TestClient, agent: dict, issue_id: int, verdict: str) -> None:
    done = client.post(
        f"/issues/{issue_id}/complete",
        json={"generation": _claim(client, agent, issue_id), "verdict": verdict},
        headers=bearer(agent),
    )
    assert done.status_code == 200, done.text


def _world(client: TestClient) -> dict:
    """A conductor delegates one issue to a seat; a second agent is its
    assignee and a third has nothing to do with it."""
    bootstrap(client)
    cora = onboard_agent(client, email="cora@e.com", name="Cora Conductor")
    sol = onboard_agent(client, email="sol@e.com", name="Sol Seat")
    ada = onboard_agent(client, email="ada@e.com", name="Ada Assignee")
    bo = onboard_agent(client, email="bo@e.com", name="Bo Bystander")
    made = client.post("/issues", json={"title": "slice"}, headers=bearer(cora))
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    assigned = client.put(
        f"/issues/{issue_id}/assignee",
        json={"assignee_id": ada["user"]["id"]},
        headers=ADMIN,
    )
    assert assigned.status_code == 200, assigned.text
    handed = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": sol["user"]["id"]},
        headers=bearer(cora),
    )
    assert handed.status_code == 201, handed.text
    return {"cora": cora, "sol": sol, "ada": ada, "bo": bo, "issue_id": issue_id}


@pytest.mark.parametrize("verdict", ["HOLD", "BLOCKED"])
def test_the_report_is_a_row_for_who_answers_for_the_work(tmp_path, verdict):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        before = {
            name: _rows(client, world[name]) for name in ("cora", "sol", "ada", "bo")
        }
        _report(client, world["sol"], world["issue_id"], verdict)
        seen = {
            name: _rows(client, world[name]) for name in ("cora", "sol", "ada", "bo")
        }
        operator = _rows(client, ADMIN)
        count = _count(db_file)

    assert before == {"cora": [], "sol": [], "ada": [], "bo": []}
    # The delegating account, the assignee and the operator; not the reporter,
    # and not an account that merely can see the issue.
    assert seen["sol"] == [] and seen["bo"] == []
    assert seen["cora"] == seen["ada"]
    (row,) = seen["cora"]
    assert {
        key: row[key]
        for key in ("signal", "severity", "source_kind", "source_id", "owner_id")
    } == {
        "signal": SIGNAL,
        "severity": "high",
        "source_kind": "issue",
        "source_id": world["issue_id"],
        "owner_id": world["sol"]["user"]["id"],
    }
    assert row["reason"].startswith(f"reported {verdict}")
    assert row["link"] == f"/aegis/issues/{world['issue_id']}"
    assert [r["source_id"] for r in operator] == [world["issue_id"]]
    assert count == 1


@pytest.mark.parametrize("verdict", ["PASS", "PASS-WITH-NOTES", None])
def test_a_report_that_holds_nothing_is_no_row(tmp_path, verdict):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        done = client.post(
            f"/issues/{world['issue_id']}/complete",
            json={
                "generation": _claim(client, world["sol"], world["issue_id"]),
                **({} if verdict is None else {"verdict": verdict}),
            },
            headers=bearer(world["sol"]),
        )
        assert done.status_code == 200, done.text
        rows = _rows(client, ADMIN)

    assert rows == [] and _count(db_file) == 0


def _cora_comments(client, world):
    return client.post(
        f"/issues/{world['issue_id']}/comments",
        json={"body": "rework the parser, keep the tests"},
        headers=bearer(world["cora"]),
    )


def _admin_moves_it(client, world):
    return client.patch(
        f"/issues/{world['issue_id']}", json={"status": "in_progress"}, headers=ADMIN
    )


def _admin_closes_it(client, world):
    return client.patch(
        f"/issues/{world['issue_id']}", json={"status": "done"}, headers=ADMIN
    )


def _sol_closes_it(client, world):
    # By the reporter, so it is no answer: the row leaves because the issue
    # is done.
    return client.patch(
        f"/issues/{world['issue_id']}",
        json={"status": "done"},
        headers=bearer(world["sol"]),
    )


def _sol_takes_it_up_again(client, world):
    _claim(client, world["sol"], world["issue_id"])


def _sol_reports_pass(client, world):
    _report(client, world["sol"], world["issue_id"], "PASS")


def _admin_archives_it(client, world):
    return client.post(f"/issues/{world['issue_id']}/archive", headers=ADMIN)


@pytest.mark.parametrize(
    "act",
    [
        _cora_comments,
        _admin_moves_it,
        _admin_closes_it,
        _sol_closes_it,
        _sol_takes_it_up_again,
        _sol_reports_pass,
        _admin_archives_it,
    ],
)
def test_the_row_leaves_when_someone_acts_on_the_work(tmp_path, act):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _report(client, world["sol"], world["issue_id"], "HOLD")
        assert len(_rows(client, world["cora"])) == 1
        answer = act(client, world)
        assert answer is None or answer.status_code in (200, 201), answer.text
        after = (_rows(client, world["cora"]), _rows(client, ADMIN))

    assert after == ([], [])
    assert _count(db_file) == 0


def test_the_reporter_saying_more_is_not_an_answer(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _report(client, world["sol"], world["issue_id"], "BLOCKED")
        said = client.post(
            f"/issues/{world['issue_id']}/comments",
            json={"body": "still blocked"},
            headers=bearer(world["sol"]),
        )
        assert said.status_code == 201, said.text
        rows = _rows(client, world["cora"])

    assert len(rows) == 1 and _count(db_file) == 1


def test_the_reporter_is_not_asked_about_its_own_report(tmp_path):
    """Also when the reporter is the issue's assignee, one of the two accounts
    a report is raised for."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        assigned = client.put(
            f"/issues/{world['issue_id']}/assignee",
            json={"assignee_id": world["sol"]["user"]["id"]},
            headers=ADMIN,
        )
        assert assigned.status_code == 200, assigned.text
        _report(client, world["sol"], world["issue_id"], "HOLD")
        seen = (_rows(client, world["sol"]), _rows(client, world["cora"]))

    assert seen[0] == [] and len(seen[1]) == 1


def test_a_newer_hold_replaces_the_older_one(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _report(client, world["sol"], world["issue_id"], "HOLD")
        _report(client, world["sol"], world["issue_id"], "BLOCKED")
        rows = _rows(client, world["cora"])

    assert [row["reason"].split(";")[0] for row in rows] == ["reported BLOCKED"]
    assert _count(db_file) == 1


def test_an_account_that_can_no_longer_see_the_issue_has_no_row(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        project = client.post(
            "/projects", json={"name": "Hidden", "key": "HID"}, headers=ADMIN
        ).json()
        for name in ("sol", "ada"):
            added = client.post(
                f"/projects/{project['id']}/members",
                json={"user_id": world[name]["user"]["id"]},
                headers=ADMIN,
            )
            assert added.status_code in (200, 201), added.text
        moved = client.patch(
            f"/issues/{world['issue_id']}",
            json={"project_id": project["id"]},
            headers=ADMIN,
        )
        assert moved.status_code == 200, moved.text
        private = client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        assert private.status_code == 200, private.text
        _report(client, world["sol"], world["issue_id"], "HOLD")
        seen = {name: _rows(client, world[name]) for name in ("cora", "ada")}

    # Cora delegated it and is not a member of the project it moved to.
    assert seen["cora"] == []
    assert len(seen["ada"]) == 1


def test_the_cockpit_says_it_in_words(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _report(client, world["sol"], world["issue_id"], "HOLD")
        login(client, "a@e.com")
        page = client.get("/aegis/dashboard").text

    assert "Sol Seat reported HOLD: decide what happens to the work" in page
    assert "Reports of HOLD or BLOCKED" in page
    assert SIGNAL not in page


def test_the_read_walks_only_the_held_verdicts(tmp_path):
    """The verdict of every completion is stored; the read must not walk the
    PASS ones to find the few that hold."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _report(client, world["sol"], world["issue_id"], "HOLD")

    statements: list[tuple[str, tuple]] = []

    class Recording:
        def __init__(self, inner: sqlite3.Connection):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def execute(self, sql, params=()):
            statements.append((sql, tuple(params)))
            return self._inner.execute(sql, params)

    conn = db.connect(db_file)
    try:
        lane = held_reports.open_held_reports(Recording(conn), actor=None)
        plans = [
            [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
            for sql, params in statements
            if "activity_event_facts" in sql
        ]
    finally:
        conn.close()

    assert lane["total"] == 1 and len(plans) == 2
    for plan in plans:
        facts = [step for step in plan if " f " in f"{step} "]
        assert facts and all(
            "idx_activity_event_facts_held" in step for step in facts
        ), plan
