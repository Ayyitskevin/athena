"""A report of HOLD or BLOCKED asks its author for the rule that would have
prevented it (W4).

Before: a HOLD said what was wrong with one candidate and nothing asked what
rule would have stopped it. The learning existed as a tool the reviewer had to
remember. Now the completion's answer names the call, while the finding is in
front of the account that made it. Nothing is recorded by reporting: the
learning stays the reporter's own, explicit act.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, bearer, bootstrap, make_app, onboard_agent

from athena.aegis import lease_commands


def _world(client: TestClient) -> dict:
    bootstrap(client)
    rex = onboard_agent(
        client,
        email="rex@e.com",
        name="Rex Reviewer",
        scopes=("read", "issue:write", "docs:write"),
    )
    issue = client.post("/issues", json={"title": "review slice"}, headers=ADMIN).json()
    handed = client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": rex["user"]["id"]},
        headers=ADMIN,
    )
    assert handed.status_code == 201, handed.text
    space = client.post(
        "/spaces", json={"key": "RUN", "name": "Runbooks"}, headers=ADMIN
    ).json()
    return {"rex": rex, "issue_id": issue["id"], "space_id": space["id"]}


def _complete(client: TestClient, world: dict, **report):
    rex, issue_id = world["rex"], world["issue_id"]
    tag = client.get(f"/issues/{issue_id}", headers=bearer(rex)).headers["ETag"]
    claimed = client.post(
        f"/issues/{issue_id}/claim", json={}, headers={**bearer(rex), "If-Match": tag}
    )
    assert claimed.status_code == 201, claimed.text
    done = client.post(
        f"/issues/{issue_id}/complete",
        json={"generation": claimed.json()["generation"], **report},
        headers=bearer(rex),
    )
    assert done.status_code == 200, done.text
    return done.json()


@pytest.mark.parametrize("verdict", ["HOLD", "BLOCKED"])
def test_the_answer_names_the_call_and_the_call_records_the_rule(tmp_path, verdict):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        answer = _complete(client, world, verdict=verdict)
        runbook = f"/issues/{world['issue_id']}/runbook"
        before = client.get(runbook, headers=bearer(world["rex"]))
        # The call the answer names, made by the account it was said to.
        recorded = client.post(
            f"/issues/{world['issue_id']}/learnings",
            json={
                "summary": "Check the empty result before the header row.",
                "space_id": world["space_id"],
            },
            headers=bearer(world["rex"]),
        )
        after = client.get(runbook, headers=bearer(world["rex"]))

    next_text = answer["next"]
    assert next_text.startswith(lease_commands.COMPLETION_NEXT)
    assert f"You reported {verdict}" in next_text
    assert next_text.endswith("record_run_learning(issue_id, summary)")
    # Reporting recorded nothing: the runbook is the reporter's later act.
    assert before.status_code == 200 and before.json() is None
    assert recorded.status_code == 201, recorded.text
    assert after.json() is not None


@pytest.mark.parametrize(
    "report", [{}, {"verdict": "PASS"}, {"verdict": "PASS-WITH-NOTES"}]
)
def test_a_report_that_holds_nothing_is_answered_as_before(tmp_path, report):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        answer = _complete(client, world, **report)

    assert answer["next"] == lease_commands.COMPLETION_NEXT
    assert answer["next"] == (
        "complete_claim released the lease only; issue status is unchanged"
    )
