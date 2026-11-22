"""Work the caller just settled is not offered back (A2, decision D3).

The loop defect (LOOP-1): with two ready issues, claim-next returned the first;
the agent completed it with a report (completing releases the lease and does not
close the issue); the next claim-next returned the SAME issue, ahead of the
untouched one, every time. The same followed a ``needs_input`` yield, and the
asker could then resume its own handoff, clearing its question from the
operator's view without anyone answering it.

The ready rule now reads the issue's newest native settlement:

- after a ``needs_input`` yield the issue is ready for NO account until an
  answer: a comment, status change, assignment change or claim by an account
  other than the one that yielded, that is not Automation;
- after a completion (report or not) it is not ready for the account that
  completed it until the same kind of answer; other accounts are unaffected;
- ``blocked`` and ``capacity`` yields hold nobody back;
- direct ``claim_issue`` is unchanged for everyone, and is itself an answer.

Imported history never settles or answers. The desk's ready lane and total, the
``is:ready`` atom and claim-next agree in every state, and the desk's
``waiting_on_operator.not_offered`` names each held-back issue with its reason.
"""

import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from athena.aegis import automation, comment_commands, issue_commands
from athena.core import activity, db
from athena.main import create_app
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

OPERATOR = {"X-Athena-Actor": "1"}
REPORT = {
    "verdict": "PASS",
    "results": [{"condition": "Parser written", "state": "met", "note": ""}],
    "unfinished": ["Close the issue."],
}


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "awaiting.db"
    app = create_app(path)
    with TestClient(app) as client:
        checked(
            client.post(
                "/users",
                json={"email": "op@test.example", "name": "Operator", "password": "pw"},
            ),
            201,
        )
        sol = _onboard(client, "sol@test.example", "Sol")
        terra = _onboard(client, "terra@test.example", "Terra")
        yield client, path, app, sol, terra


def _onboard(client, email, name):
    return checked(
        client.post(
            "/users/onboard_agent",
            json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
            headers=OPERATOR,
        ),
        201,
    )


def bearer(agent):
    return {"Authorization": f"Bearer {agent['token']['token']}"}


def uid(agent):
    return agent["user"]["id"]


def issue(client, title, *agents, **fields):
    row = checked(
        client.post("/issues", json={"title": title, **fields}, headers=OPERATOR),
        201,
    )
    for agent in agents:
        checked(
            client.post(
                f"/issues/{row['id']}/delegate",
                json={"user_id": uid(agent)},
                headers=OPERATOR,
            ),
            201,
        )
    return row


def pickup(client, agent):
    return checked(client.post("/issues/claim-next", json={}, headers=bearer(agent)))


def claim(client, agent, issue_id):
    tag = client.get(f"/issues/{issue_id}", headers=bearer(agent)).headers["etag"]
    return checked(
        client.post(
            f"/issues/{issue_id}/claim", headers={**bearer(agent), "If-Match": tag}
        ),
        201,
    )


def complete(client, agent, issue_id, generation, report=None):
    return checked(
        client.post(
            f"/issues/{issue_id}/complete",
            json={"generation": generation, **(report or {})},
            headers=bearer(agent),
        )
    )


def yield_(client, agent, issue_id, generation, reason="needs_input"):
    return checked(
        client.post(
            f"/issues/{issue_id}/yield",
            json={
                "generation": generation,
                "reason": reason,
                "attempted_work": "Read the specification.",
                "evidence": ["notes"],
                "blocking_question": "Which branch?",
                "resume_instructions": "Read the operator's answer.",
            },
            headers=bearer(agent),
        ),
        201,
    )


def comment(client, issue_id, headers=OPERATOR, body="Use main."):
    checked(
        client.post(
            f"/issues/{issue_id}/comments", json={"body": body}, headers=headers
        ),
        201,
    )


def views(client, agent):
    """The ready rule as each surface reports it, asserting they agree: the
    desk's ``work.ready`` lane and total, ``GET /desk/ready``, and ``is:ready``.
    Returns (ready ids in claim order, the desk's not_offered rows)."""
    desk = checked(client.get("/desk", headers=bearer(agent)))
    lane = desk["work"]["ready"]
    alone = checked(client.get("/desk/ready", headers=bearer(agent)))
    query = checked(
        client.get("/issues", params={"q": "is:ready"}, headers=bearer(agent))
    )
    count = checked(
        client.get(
            "/issues/query/count", params={"q": "is:ready"}, headers=bearer(agent)
        )
    )
    ids = [row["issue"]["id"] for row in lane["items"]]
    assert [row["issue"]["id"] for row in alone["items"]] == ids
    assert lane["total"] == alone["total"] == len(ids) == count["matched"]
    assert sorted(row["id"] for row in query) == sorted(ids)
    not_offered = desk["work"]["waiting_on_operator"]["not_offered"]
    assert not_offered["total"] == len(not_offered["items"])
    # Held back and ready are disjoint: one rule, read two ways.
    assert not {row["issue"]["id"] for row in not_offered["items"]} & set(ids)
    return ids, not_offered["items"]


def held(rows):
    return {row["issue"]["id"]: row["reason"] for row in rows}


# ── completion ──────────────────────────────────────────────────────────────


def test_a_completed_issue_is_not_offered_back_and_the_untouched_one_is(setup):
    client, _path, _app, sol, _terra = setup
    first = issue(client, "first", sol, priority="urgent")
    second = issue(client, "second", sol, priority="low")
    assert views(client, sol)[0] == [first["id"], second["id"]]

    taken = pickup(client, sol)
    assert taken["issue"]["id"] == first["id"]
    complete(client, sol, first["id"], taken["lease"]["generation"], REPORT)

    ready, waiting = views(client, sol)
    assert ready == [second["id"]]
    (row,) = waiting
    assert row["issue"]["id"] == first["id"]
    assert row["reason"] == "completed_by_you"
    assert row["settled_by_you"] is True and row["settled_at"] is not None

    # The defect: this returned ``first`` again, ahead of the untouched issue.
    taken = pickup(client, sol)
    assert taken["claimed"] is True and taken["issue"]["id"] == second["id"]
    complete(client, sol, second["id"], taken["lease"]["generation"])

    ready, waiting = views(client, sol)
    assert ready == []
    assert held(waiting) == {
        first["id"]: "completed_by_you",
        second["id"]: "completed_by_you",
    }
    nothing = pickup(client, sol)
    assert nothing["claimed"] is False and nothing["reason"] == "nothing_ready"
    assert nothing["ready_total"] == 0


@pytest.mark.parametrize("report", [REPORT, None], ids=["report", "bare"])
def test_a_completion_holds_with_or_without_a_report(setup, report):
    client, _path, _app, sol, _terra = setup
    work = issue(client, "work", sol)
    taken = pickup(client, sol)
    complete(client, sol, work["id"], taken["lease"]["generation"], report)

    ready, waiting = views(client, sol)
    assert ready == [] and held(waiting) == {work["id"]: "completed_by_you"}
    assert pickup(client, sol)["reason"] == "nothing_ready"


def test_another_eligible_account_is_still_offered_the_completed_issue(setup):
    client, _path, _app, sol, terra = setup
    work = issue(client, "work", sol, terra)
    taken = pickup(client, sol)
    complete(client, sol, work["id"], taken["lease"]["generation"], REPORT)

    assert views(client, sol)[0] == []
    ready, waiting = views(client, terra)
    assert ready == [work["id"]] and waiting == []
    theirs = pickup(client, terra)
    assert theirs["claimed"] is True and theirs["issue"]["id"] == work["id"]


def test_the_completer_can_still_claim_directly(setup):
    client, _path, _app, sol, _terra = setup
    work = issue(client, "work", sol)
    taken = pickup(client, sol)
    complete(client, sol, work["id"], taken["lease"]["generation"])
    assert views(client, sol)[0] == []

    again = claim(client, sol, work["id"])
    assert again["holder_id"] == uid(sol)


def _answer(client, kind, issue_id, sol):
    if kind == "comment":
        comment(client, issue_id)
    elif kind == "status":
        checked(
            client.patch(
                f"/issues/{issue_id}", json={"status": "in_progress"}, headers=OPERATOR
            )
        )
    else:
        checked(
            client.put(
                f"/issues/{issue_id}/assignee",
                json={"assignee_id": uid(sol)},
                headers=OPERATOR,
            )
        )


@pytest.mark.parametrize("kind", ["comment", "status", "assignment"])
def test_an_operator_answer_makes_a_completed_issue_ready_again(setup, kind):
    client, _path, _app, sol, _terra = setup
    work = issue(client, "work", sol)
    taken = pickup(client, sol)
    complete(client, sol, work["id"], taken["lease"]["generation"], REPORT)
    assert views(client, sol)[0] == []

    _answer(client, kind, work["id"], sol)

    ready, waiting = views(client, sol)
    assert ready == [work["id"]] and waiting == []
    again = pickup(client, sol)
    assert again["claimed"] is True and again["issue"]["id"] == work["id"]


def test_own_automation_and_imported_events_do_not_answer(setup):
    client, path, _app, sol, _terra = setup
    work = issue(client, "work", sol)
    taken = pickup(client, sol)
    complete(client, sol, work["id"], taken["lease"]["generation"], REPORT)

    # The completer's own later comment is not an answer.
    comment(client, work["id"], headers=bearer(sol), body="Done; please close.")
    assert views(client, sol)[0] == []

    conn = db.connect(path)
    try:
        robot = automation.system_actor_id(conn)
        comment_commands.create_comment_as_automation(
            conn, actor_id=robot, issue_id=work["id"], body="Rule fired."
        )
        issue_commands.update_issue_as_automation(
            conn, actor_id=robot, issue_id=work["id"], status="in_progress"
        )
        # Foreign history: the operator's comment as an import, not an act here.
        activity.record(
            conn,
            actor_id=1,
            verb="commented",
            target_kind="issue",
            target_id=work["id"],
            imported_at="2035-01-01 00:00:00",
            commit=True,
        )
        verbs = [
            row["verb"]
            for row in conn.execute(
                "SELECT verb FROM activity WHERE target_kind = 'issue' "
                "AND target_id = ? AND id > (SELECT MAX(id) FROM activity "
                "WHERE verb = 'claim_completed')",
                (work["id"],),
            )
        ]
    finally:
        conn.close()
    assert verbs == ["commented", "commented", "changed_status", "commented"]

    ready, waiting = views(client, sol)
    assert ready == [] and held(waiting) == {work["id"]: "completed_by_you"}
    assert pickup(client, sol)["reason"] == "nothing_ready"

    # Positive control: a native operator comment does answer.
    comment(client, work["id"])
    assert views(client, sol)[0] == [work["id"]]


def test_an_imported_completion_holds_nothing_back(setup):
    client, path, _app, sol, _terra = setup
    work = issue(client, "work", sol)
    conn = db.connect(path)
    try:
        activity.record(
            conn,
            actor_id=uid(sol),
            verb="claim_completed",
            target_kind="issue",
            target_id=work["id"],
            detail="generation " + "a" * 32,
            imported_at="2035-01-01 00:00:00",
            commit=True,
        )
    finally:
        conn.close()
    ready, waiting = views(client, sol)
    assert ready == [work["id"]] and waiting == []


# ── yields ──────────────────────────────────────────────────────────────────


def test_a_needs_input_yield_is_offered_to_nobody_until_a_direct_claim(setup):
    client, _path, _app, sol, terra = setup
    work = issue(client, "work", sol, terra)
    taken = pickup(client, sol)
    handoff = yield_(client, sol, work["id"], taken["lease"]["generation"])

    for agent, mine in ((sol, True), (terra, False)):
        ready, waiting = views(client, agent)
        assert ready == []
        (row,) = waiting
        assert row["issue"]["id"] == work["id"]
        assert row["reason"] == "awaiting_answer"
        assert row["settled_by_you"] is mine
        # Another account's settlement time is not disclosed.
        assert (row["settled_at"] is not None) is mine
        assert pickup(client, agent)["reason"] == "nothing_ready"
    # The asker still sees its own question beside the hold.
    sol_desk = checked(client.get("/desk", headers=bearer(sol)))
    questions = sol_desk["work"]["waiting_on_operator"]["items"]
    assert [q["handoff_token"] for q in questions] == [handoff["handoff_token"]]

    # The explicit takeover path still works and resumes the handoff.
    lease = claim(client, terra, work["id"])
    assert lease["open_claim_handoff"]["handoff_token"] == handoff["handoff_token"]
    resumed = checked(
        client.post(
            f"/issues/{work['id']}/claim-handoffs/{handoff['handoff_token']}/resume",
            json={"generation": lease["generation"]},
            headers=bearer(terra),
        )
    )
    assert resumed["state"] == "resumed"
    complete(client, terra, work["id"], lease["generation"])

    # Terra's claim answered Sol's yield; Terra's own completion now holds the
    # issue back from Terra alone.
    assert views(client, sol)[0] == [work["id"]]
    ready, waiting = views(client, terra)
    assert ready == [] and held(waiting) == {work["id"]: "completed_by_you"}


def test_a_needs_input_yield_is_ready_again_after_an_operator_answer(setup):
    client, _path, _app, sol, terra = setup
    work = issue(client, "work", sol, terra)
    taken = pickup(client, sol)
    handoff = yield_(client, sol, work["id"], taken["lease"]["generation"])

    # The asker's own follow-up is not an answer, for anyone.
    comment(client, work["id"], headers=bearer(sol), body="Still need a branch.")
    assert views(client, sol)[0] == [] and views(client, terra)[0] == []

    comment(client, work["id"])
    for agent in (sol, terra):
        ready, waiting = views(client, agent)
        assert ready == [work["id"]] and waiting == []
    again = pickup(client, sol)
    assert again["issue"]["id"] == work["id"]
    assert again["open_handoff"]["handoff_token"] == handoff["handoff_token"]


@pytest.mark.parametrize("reason", ["blocked", "capacity"])
def test_other_yields_hold_nobody_back(setup, reason):
    client, _path, _app, sol, terra = setup
    work = issue(client, "work", sol, terra)
    taken = pickup(client, sol)
    yield_(client, sol, work["id"], taken["lease"]["generation"], reason=reason)
    for agent in (sol, terra):
        ready, waiting = views(client, agent)
        assert ready == [work["id"]] and waiting == []


# ── visibility ──────────────────────────────────────────────────────────────


def test_a_hidden_issue_is_neither_ready_nor_listed_as_held(setup):
    client, _path, _app, sol, _terra = setup
    project = checked(
        client.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=OPERATOR
        ),
        201,
    )
    checked(
        client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": uid(sol)},
            headers=OPERATOR,
        ),
        201,
    )
    checked(
        client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=OPERATOR,
        )
    )
    work = issue(client, "private work", sol, project_id=project["id"])
    taken = pickup(client, sol)
    complete(client, sol, work["id"], taken["lease"]["generation"])
    assert held(views(client, sol)[1]) == {work["id"]: "completed_by_you"}

    checked(
        client.delete(f"/projects/{project['id']}/members/{uid(sol)}", headers=OPERATOR)
    )
    ready, waiting = views(client, sol)
    assert ready == [] and waiting == []


# ── the MCP tool ────────────────────────────────────────────────────────────


def test_the_mcp_tool_serves_the_untouched_issue_after_a_completion(setup):
    client, _path, app, sol, _terra = setup
    first = issue(client, "first", sol, priority="urgent")
    second = issue(client, "second", sol, priority="low")
    with TestClient(app) as transport:
        transport.headers.update(bearer(sol))
        server = mcp_server.build_server(
            AthenaClient(client=transport),
            scopes=frozenset({"read", "issue:write"}),
            profile=mcp_server.PROFILES["seat"],
        )

        def call(name, arguments):
            content, _ = asyncio.run(server.call_tool(name, arguments))
            return json.loads(content[0].text)

        taken = call("claim_next_ready", {})
        assert taken["issue"]["id"] == first["id"]
        call(
            "complete_claim",
            {
                "issue_id": first["id"],
                "generation": taken["lease"]["generation"],
                "results": [{"condition": "Done", "state": "met", "note": ""}],
            },
        )
        taken = call("claim_next_ready", {})
        assert taken["issue"]["id"] == second["id"]
