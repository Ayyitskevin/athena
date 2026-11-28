"""``complete_claim`` closes the issue in the same call when asked (AD13).

WHY: in docs/plans/reports/agent-day-2026-11-26.md (DAY-11) every slice ended
with ``complete_claim`` and then ``update_issue(status="done")``, always
together. ``close_status`` makes the second call through the same command, so
its gates still apply; the release is never undone by a refused close, and the
answer says which of the two happened.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from support import ADMIN, bearer, bootstrap, make_app, onboard_agent


def _held(client, sol, title):
    made = client.post(
        "/issues",
        json={"title": title, "assignee_id": sol["user"]["id"]},
        headers=ADMIN,
    )
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    tag = client.get(f"/issues/{issue_id}", headers=bearer(sol)).headers["ETag"]
    held = client.post(
        f"/issues/{issue_id}/claim",
        json={},
        headers={**bearer(sol), "If-Match": tag},
    )
    assert held.status_code == 201, held.text
    return issue_id, held.json()["generation"]


def _complete(client, sol, issue_id, generation, **body):
    return client.post(
        f"/issues/{issue_id}/complete",
        json={"generation": generation, **body},
        headers=bearer(sol),
    )


def _lease(client, sol, issue_id):
    return client.get(f"/issues/{issue_id}/lease", headers=bearer(sol)).json()


def test_close_status_releases_and_closes_in_one_call(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        issue_id, generation = _held(client, sol, "Close with the release")
        done = _complete(client, sol, issue_id, generation, close_status="done")
        assert done.status_code == 200, done.text
        answer = done.json()
        assert answer["released"] is True
        assert answer["close"] == {"status": "done", "applied": True}
        assert answer["next"] == {
            "tool": "claim_next_ready",
            "arguments": {},
            "hint": "released the lease and set the issue to done",
        }
        assert answer["issue_status"] == "done"
        assert answer["issue_still_open"] is False
        # The answer carries the closed issue's tag for the next guarded write.
        assert (
            done.headers["ETag"]
            == client.get(f"/issues/{issue_id}", headers=bearer(sol)).headers["ETag"]
        )
        issue = client.get(f"/issues/{issue_id}", headers=bearer(sol)).json()
        assert issue["status"] == "done"

        # Without close_status the answer and the issue are as before.
        other, generation = _held(client, sol, "Release only")
        plain = _complete(client, sol, other, generation)
        assert plain.status_code == 200, plain.text
        assert "close" not in plain.json()
        assert plain.json()["issue_still_open"] is True


def test_a_status_the_project_does_not_offer_is_refused_before_the_release(
    tmp_path,
):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        issue_id, generation = _held(client, sol, "Typo in the status")
        refused = _complete(client, sol, issue_id, generation, close_status="dnoe")
        assert refused.status_code == 422, refused.text
        assert "done" in refused.json()["valid_statuses"]
        # Nothing happened: the lease is still held at the same generation.
        assert _lease(client, sol, issue_id)["generation"] == generation


def test_a_gated_close_asks_and_keeps_the_release(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        gated = client.put(
            f"/approvals/policies/{sol['user']['id']}",
            json={"action_kind": "issue.close"},
            headers=ADMIN,
        )
        assert gated.status_code == 200, gated.text
        issue_id, generation = _held(client, sol, "Gated close")
        answer = _complete(client, sol, issue_id, generation, close_status="done")
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["released"] is True
        close = body["close"]
        assert close["applied"] is False
        assert close["code"] == "approval_required"
        assert close["approval"]["state"] == "pending"
        assert "lease is released" in close["detail"]
        # The retry is a call, not a sentence to parse (K2.1).
        assert close["next"] == {
            "tool": "update_issue",
            "arguments": {"issue_id": issue_id, "status": "done"},
            "hint": "once the operator approves",
        }
        assert body["issue_still_open"] is True
        issue = client.get(f"/issues/{issue_id}", headers=bearer(sol)).json()
        assert issue["status"] != "done"
        assert _lease(client, sol, issue_id) is None

        # Once approved, the ordinary status change goes through.
        approved = client.post(
            f"/approvals/{close['approval']['id']}/decision",
            json={"decision": "approve"},
            headers=ADMIN,
        )
        assert approved.status_code == 200, approved.text
        closed = client.patch(
            f"/issues/{issue_id}", json={"status": "done"}, headers=bearer(sol)
        )
        assert closed.status_code == 200, closed.text
