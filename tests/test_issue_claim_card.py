"""The issue page's Claim card shows the LEASE, and the assignee as the assignee.

COCKPIT-5 (U1): the card read "unclaimed" while an agent held a live lease,
because it rendered ``issue.assignee_name`` and never the lease. The agent
protocol runs on the lease — the exclusive, expiring possession an agent takes
with ``POST /issues/{id}/claim`` — while the assignee is the accountable owner.
``DESIGN_SYSTEM.md``: "Claim, not assignment … Say which one you mean."

These pin the four states an operator meets: a live lease held by someone who
is not the assignee, no lease at all, an assignee with no lease, and an expired
lease nobody settled (lapsed). Each reads the same lease row the REST lease read
serves, so the page and ``GET /issues/{id}/lease`` cannot disagree, and a viewer
who cannot see the issue sees no holder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re

from fastapi.testclient import TestClient

from support import (
    ADMIN,
    add_user,
    bearer,
    bootstrap,
    login,
    make_app,
    onboard_agent,
)

from athena.core import db


def _cell(page: str, label: str) -> str:
    """The text of one supervision cell: from its ``k`` label to the next one."""
    start = page.index(f'<span class="k">{label}</span>')
    nxt = page.find('<span class="k">', start + 1)
    end = nxt if nxt != -1 else page.index("</div>\n\n", start)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page[start:end])).strip()


def _issue(client: TestClient, title: str, **fields) -> dict:
    response = client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def _claim(
    client: TestClient, agent: dict, issue_id: int, run: str | None = None
) -> dict:
    """Delegate the issue to the agent and have it take the lease over REST."""
    delegated = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    assert delegated.status_code in (200, 201), delegated.text
    read = client.get(f"/issues/{issue_id}", headers=bearer(agent))
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        headers={
            **bearer(agent, run=run or f"run-{issue_id}"),
            "If-Match": read.headers["ETag"],
        },
    )
    assert claimed.status_code in (200, 201), claimed.text
    return claimed.json()


def _assign(client: TestClient, issue_id: int, user_id: int) -> None:
    response = client.put(
        f"/issues/{issue_id}/assignee", json={"assignee_id": user_id}, headers=ADMIN
    )
    assert response.status_code == 200, response.text


def _expire(db_file, issue_id: int) -> str:
    """Move the lease's expiry an hour into the past, as the clock would."""
    stamp = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db.connect(db_file)
    conn.execute(
        "UPDATE issue_leases SET expires_at = ? WHERE issue_id = ?", (stamp, issue_id)
    )
    conn.commit()
    conn.close()
    return stamp


def _page(client: TestClient, issue_id: int) -> str:
    response = client.get(f"/aegis/issues/{issue_id}")
    assert response.status_code == 200, response.text
    return response.text


def test_a_live_lease_names_its_holder_and_expiry_not_the_assignee(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        bob = add_user(client, "bob@e.com", name="Bobby Assignee")
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "held by an agent, owned by a human")
        _assign(client, issue["id"], bob["id"])
        lease = _claim(client, agent, issue["id"])
        # The page reads the same lease the REST read serves.
        served = client.get(f"/issues/{issue['id']}/lease", headers=ADMIN).json()
        assert served["active"] is True
        login(client, "a@e.com")
        page = _page(client, issue["id"])

    claim = _cell(page, "Claim")
    assert "Sol Holder" in claim
    assert "live lease" in claim
    assert "agent" in claim  # the holder is a machine, so it carries the badge
    assert served["expires_at"] in claim
    assert "expires" in claim
    assert lease["claimed_at"] in claim
    # The assignee is not the claim; it is shown, labelled, as the assignee.
    assert "Bobby Assignee" not in claim
    assert "unclaimed" not in claim
    assignee = _cell(page, "Assignee")
    assert "Bobby Assignee" in assignee
    assert "Sol Holder" not in assignee


def test_no_lease_and_no_assignee_says_so_plainly(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue = _issue(client, "nobody on it")
        login(client, "a@e.com")
        page = _page(client, issue["id"])

    claim = _cell(page, "Claim")
    assert "unclaimed" in claim
    assert "no lease" in claim
    assert "expires" not in claim
    assert "unassigned" in _cell(page, "Assignee")


def test_an_assignee_without_a_lease_is_the_assignee_and_no_claim(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        bob = add_user(client, "bob@e.com", name="Bobby Assignee")
        issue = _issue(client, "owned, not held")
        _assign(client, issue["id"], bob["id"])
        login(client, "a@e.com")
        page = _page(client, issue["id"])

    claim = _cell(page, "Claim")
    assert "unclaimed" in claim
    assert "no lease" in claim
    # Before U1 this cell named the assignee as if they held the claim.
    assert "Bobby Assignee" not in claim
    assignee = _cell(page, "Assignee")
    assert "Bobby Assignee" in assignee
    assert "human assignee" in assignee


def test_an_expired_unsettled_lease_reads_lapsed_and_names_its_holder(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "the clock let go")
        _claim(client, agent, issue["id"])
        expired_at = _expire(db_file, issue["id"])
        served = client.get(f"/issues/{issue['id']}/lease", headers=ADMIN).json()
        assert served["active"] is False
        login(client, "a@e.com")
        page = _page(client, issue["id"])

    claim = _cell(page, "Claim")
    assert "lapsed lease" in claim
    assert "last held by Sol Holder" in claim
    # It holds nothing: the same verdict as the is:unclaimed query atom.
    assert "unclaimed" in claim
    assert expired_at in claim
    # Lapsed is not held: the cell must not say the lease still runs.
    assert "expires " not in claim
    # Reading the page settles nothing: the row is still there.
    conn = db.connect(db_file)
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM issue_leases").fetchone()["n"]
    finally:
        conn.close()
    assert rows == 1


def test_a_lease_taken_over_an_open_handoff_names_the_holder_not_yielded(tmp_path):
    """A handoff stays open until its new holder resumes it, so a live lease
    and an open handoff can hold at once. The card names who holds the work
    and says the handoff is still open; it must not read "yielded"."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        asker = onboard_agent(client, email="ada@e.com", name="Ada Asker")
        taker = onboard_agent(client, email="tess@e.com", name="Tess Taker")
        issue = _issue(client, "asked, then taken over")
        lease = _claim(client, asker, issue["id"])
        yielded = client.post(
            f"/issues/{issue['id']}/yield",
            json={
                "generation": lease["generation"],
                "reason": "needs_input",
                "attempted_work": "Read the specification",
                "evidence": [],
                "blocking_question": "Which branch?",
                "resume_instructions": "Read the operator's answer",
            },
            headers=bearer(asker),
        )
        assert yielded.status_code == 201, yielded.text
        login(client, "a@e.com")
        before = _cell(_page(client, issue["id"]), "Claim")
        client.cookies.clear()
        _claim(client, taker, issue["id"], run="run-taker")
        login(client, "a@e.com")
        after = _cell(_page(client, issue["id"]), "Claim")

    assert "yielded" in before and "needs_input" in before
    assert "Tess Taker" in after and "live lease" in after
    assert "yielded" not in after
    assert "handoff open" in after and "not resumed" in after


def test_a_viewer_who_cannot_see_the_issue_sees_no_holder(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        outsider = add_user(client, "out@e.com", name="Outsider")
        project = client.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=ADMIN
        )
        assert project.status_code == 201, project.text
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "private work", project_id=project.json()["id"])
        _claim(client, agent, issue["id"])
        hidden = client.put(
            f"/projects/{project.json()['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        assert hidden.status_code == 200, hidden.text
        # The REST lease read hides it from the outsider: a hidden issue is missing.
        theirs = {"X-Athena-Actor": str(outsider["id"])}
        rest = client.get(f"/issues/{issue['id']}/lease", headers=theirs)
        assert rest.status_code == 404
        # Positive control: the admin still reads the live holder there.
        assert (
            client.get(f"/issues/{issue['id']}/lease", headers=ADMIN).json()[
                "holder_name"
            ]
            == "Sol Holder"
        )
        login(client, "out@e.com")
        response = client.get(f"/aegis/issues/{issue['id']}")

    assert response.status_code == 404
    assert "Sol Holder" not in response.text
