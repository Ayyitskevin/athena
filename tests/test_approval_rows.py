"""An approval row says what is being approved, and whose word it rests on (U9).

COCKPIT-11: the queue read "Nyx · issue.close · issue #8": no title, no
verdict, so the operator had to open the issue to learn what they were deciding.
A row now carries the issue's key and title and, for a close, the completion the
ask rests on: who reported it, when, and its verdict. That completion is the
newest one recorded before the ask, the requester's own when it has one; a
completion recorded after the ask is shown beside it and never replaces it. Every
read goes through the viewer's own visibility gates: a viewer who could not read
the issue or its trail learns nothing more from the row.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from support import (
    ADMIN,
    add_user,
    bearer,
    bootstrap,
    events,
    login,
    make_app,
    onboard_agent,
)

from athena.core import approvals, db, users


def _row(page: str, request_id: int) -> str:
    start = page.index(f'<tr id="approval-{request_id}">')
    end = page.index("</tr>", start)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page[start:end])).strip()


def _gate(client: TestClient, user_id: int) -> None:
    gated = client.put(
        f"/approvals/policies/{user_id}",
        json={"action_kind": approvals.ACTION_ISSUE_CLOSE},
        headers=ADMIN,
    )
    assert gated.status_code == 200, gated.text


def _ask_to_close(client: TestClient, agent: dict, issue_id: int) -> int:
    refused = client.patch(
        f"/issues/{issue_id}", json={"status": "done"}, headers=bearer(agent)
    )
    assert refused.json()["code"] == "approval_required", refused.text
    pending = client.get("/approvals", headers=ADMIN).json()
    return next(
        r["id"]
        for r in pending
        if r["target_id"] == issue_id and r["state"] == "pending"
    )


def _delegate(client: TestClient, issue_id: int, *agents: dict) -> None:
    for agent in agents:
        delegated = client.post(
            f"/issues/{issue_id}/delegate",
            json={"user_id": agent["user"]["id"]},
            headers=ADMIN,
        )
        assert delegated.status_code in (200, 201), delegated.text


def _complete(client: TestClient, agent: dict, issue_id: int, **report) -> dict:
    """Claim and complete; returns the claim_completed event it recorded."""
    read = client.get(f"/issues/{issue_id}", headers=bearer(agent))
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        headers={
            **bearer(agent, run=f"run-{agent['user']['id']}"),
            "If-Match": read.headers["ETag"],
        },
    )
    assert claimed.status_code in (200, 201), claimed.text
    done = client.post(
        f"/issues/{issue_id}/complete",
        json={"generation": claimed.json()["generation"], **report},
        headers=bearer(agent, run=f"run-{agent['user']['id']}"),
    )
    assert done.status_code == 200, done.text
    return client.get(
        "/activity",
        params={
            "target_kind": "issue",
            "target_id": issue_id,
            "verb": "claim_completed",
        },
        headers=ADMIN,
    ).json()[0]


def _project(client: TestClient, name: str, key: str) -> int:
    response = client.post("/projects", json={"name": name, "key": key}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _new_issue(client: TestClient, title: str, project_id: int) -> dict:
    response = client.post(
        "/issues", json={"title": title, "project_id": project_id}, headers=ADMIN
    )
    assert response.status_code == 201, response.text
    return response.json()


def _queue(client: TestClient) -> str:
    login(client, "a@e.com")
    page = client.get("/admin/agents").text
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    return page


def test_an_approval_row_names_the_issue_and_the_verdict_with_its_author(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        nyx = onboard_agent(client, email="nyx@e.com", name="Nyx Asker")
        project_id = _project(client, "Operations", "OPS")
        reported = _new_issue(client, "Rotate the <b>backup</b> keys", project_id)
        silent = _new_issue(client, "Close without a report", project_id)
        _delegate(client, reported["id"], nyx)
        _delegate(client, silent["id"], nyx)
        verdict_event = _complete(
            client,
            nyx,
            reported["id"],
            verdict="PASS-WITH-NOTES",
            results=[{"condition": "keys rotated", "state": "met", "note": "ok"}],
        )
        silent_event = _complete(client, nyx, silent["id"])
        _gate(client, nyx["user"]["id"])
        reported_request = _ask_to_close(client, nyx, reported["id"])
        silent_request = _ask_to_close(client, nyx, silent["id"])
        page = _queue(client)

    row = _row(page, reported_request)
    assert reported["key"] in row
    # The title, as text: escaped in the markup, never live.
    assert "Rotate the &lt;b&gt;backup&lt;/b&gt; keys" in row
    assert "<b>backup</b>" not in page
    assert (
        f"reported verdict PASS-WITH-NOTES by Nyx Asker · {verdict_event['created_at']} UTC"
        in row
    )
    assert "not the requester" not in row and "newer" not in row
    assert f'href="/aegis/issues/{reported["id"]}"' in page
    other = _row(page, silent_request)
    assert silent["key"] in other and "Close without a report" in other
    assert (
        f"completion with no verdict by Nyx Asker · {silent_event['created_at']} UTC"
        in other
    )
    assert "PASS" not in other
    # The row grew wider with the issue and verdict, so each table of this page
    # scrolls inside its own region and never widens a phone-width page.
    assert page.count("<table") >= 1
    assert page.count("<table") == page.count('class="agent-checkin-table-region"')


def test_the_verdict_is_the_one_the_ask_rests_on_and_a_newer_one_is_shown(tmp_path):
    """V1/V3: completions after the ask do not change what the operator is
    approving; the newest of them is shown beside it, with its author."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        nyx = onboard_agent(client, email="nyx@e.com", name="Nyx Asker")
        rhea = onboard_agent(client, email="rhea@e.com", name="Rhea Reviewer")
        issue = _new_issue(client, "asked, then reworked", _project(client, "O", "OO"))
        _delegate(client, issue["id"], nyx, rhea)
        _complete(client, nyx, issue["id"], verdict="PASS")
        _gate(client, nyx["user"]["id"])
        request_id = _ask_to_close(client, nyx, issue["id"])
        _complete(client, nyx, issue["id"], verdict="BLOCKED")
        later = _complete(client, rhea, issue["id"], verdict="HOLD")
        row = _row(_queue(client), request_id)

    assert "reported verdict PASS by Nyx Asker" in row
    assert (
        f"newer, after this request: HOLD by Rhea Reviewer · {later['created_at']} UTC"
        in row
    )
    assert "BLOCKED" not in row  # superseded by the newest, not shown as the verdict


def test_the_requesters_own_completion_is_preferred(tmp_path):
    """Another account completed after the requester but before the ask: the
    ask rests on the requester's own completion, not the other account's."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        nyx = onboard_agent(client, email="nyx@e.com", name="Nyx Asker")
        rhea = onboard_agent(client, email="rhea@e.com", name="Rhea Reviewer")
        issue = _new_issue(client, "two reporters", _project(client, "O", "OO"))
        _delegate(client, issue["id"], nyx, rhea)
        _complete(client, nyx, issue["id"], verdict="PASS")
        _complete(client, rhea, issue["id"], verdict="BLOCKED")
        _gate(client, nyx["user"]["id"])
        request_id = _ask_to_close(client, nyx, issue["id"])
        row = _row(_queue(client), request_id)

    assert "reported verdict PASS by Nyx Asker" in row
    assert "BLOCKED" not in row and "newer" not in row


def test_another_accounts_completion_is_named_as_not_the_requesters(tmp_path):
    """V5: only another account ever completed; the row says whose it is."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        nyx = onboard_agent(client, email="nyx@e.com", name="Nyx Asker")
        rhea = onboard_agent(client, email="rhea@e.com", name="Rhea Reviewer")
        issue = _new_issue(client, "other only", _project(client, "O", "OO"))
        _delegate(client, issue["id"], nyx, rhea)
        _complete(client, rhea, issue["id"], verdict="PASS")
        _gate(client, nyx["user"]["id"])
        request_id = _ask_to_close(client, nyx, issue["id"])
        row = _row(_queue(client), request_id)

    assert "reported verdict PASS by Rhea Reviewer (not the requester)" in row


def test_the_row_reads_only_what_the_viewer_could_already_read(tmp_path):
    """The read model applies the viewer's own visibility, to the issue and to
    the trail separately. A member who can see the issue now but could not see
    the completion when it was recorded (the issue was in a private project
    then) gets the issue and no completion; for a hidden issue, nothing."""
    from athena.aegis import approval_rows

    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com", name="Mo Member")
        nyx = onboard_agent(client, email="nyx@e.com", name="Nyx Asker")
        secret = _project(client, "Secret", "SEC")
        public = _project(client, "Public", "PUB")
        hidden_issue = _new_issue(client, "Private plans", secret)
        moved_issue = _new_issue(client, "Moved into the open", secret)
        for project_id in (secret,):
            made_private = client.put(
                f"/projects/{project_id}/visibility",
                json={"visibility": "private"},
                headers=ADMIN,
            )
            assert made_private.status_code == 200, made_private.text
        # Delegation refuses an account that cannot see the project, so Nyx
        # joins it first.
        joined = client.post(
            f"/projects/{secret}/members",
            json={"user_id": nyx["user"]["id"]},
            headers=ADMIN,
        )
        assert joined.status_code in (200, 201, 204), joined.text
        _delegate(client, hidden_issue["id"], nyx)
        _delegate(client, moved_issue["id"], nyx)
        _complete(client, nyx, hidden_issue["id"], verdict="HOLD")
        _complete(client, nyx, moved_issue["id"], verdict="PASS")
        moved = client.patch(
            f"/issues/{moved_issue['id']}", json={"project_id": public}, headers=ADMIN
        )
        assert moved.status_code == 200, moved.text
        _gate(client, nyx["user"]["id"])
        hidden_request = _ask_to_close(client, nyx, hidden_issue["id"])
        moved_request = _ask_to_close(client, nyx, moved_issue["id"])

    conn = db.connect(db_file)
    try:
        pending = [r.public() for r in approvals.list_requests(conn, state="pending")]
        admin = users.get_user(conn, 1)
        outsider = users.get_user(conn, member["id"])
        as_admin = approval_rows.describe(conn, viewer=admin, requests=pending)
        as_member = approval_rows.describe(conn, viewer=outsider, requests=pending)
    finally:
        conn.close()

    assert as_admin[hidden_request]["issue"]["title"] == "Private plans"
    assert as_admin[hidden_request]["completion"]["verdict"] == "HOLD"
    assert as_member[hidden_request] == {
        "issue": None,
        "completion": None,
        "newer": None,
    }
    # The member sees the issue now, but not the completion it could not see.
    assert as_admin[moved_request]["completion"]["verdict"] == "PASS"
    assert as_member[moved_request]["issue"]["title"] == "Moved into the open"
    assert as_member[moved_request]["completion"] is None
    assert as_member[moved_request]["newer"] is None
    # Sanity: the completion exists, so None above is the gate, not its absence.
    assert any(
        e["target_id"] == moved_issue["id"] for e in events(db_file, "claim_completed")
    )
