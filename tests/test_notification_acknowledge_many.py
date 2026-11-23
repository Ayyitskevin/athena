"""One call acknowledges many notifications, and a renewal is not one (F7, D18).

WHY: in the fleet trial 39 of 142 calls acknowledged one notification each,
and a conductor ended one outcome with 23 unread; each lease renewal also
notified the assignee, so a seat that kept its lease alive filled its owner's
inbox with nothing to act on. Under D18 the existing acknowledgement takes a
set (``POST /notifications/read_all`` with ``ids`` or ``through_id``; the
``mark_notification_read`` tool with the same), and a renewal stays an event
in the trail and the feed but notifies nobody.

What the set form must keep:

- an account acknowledges only its own notifications: another account's id and
  an id that does not exist answer alike, and change nothing;
- acknowledging is idempotent: a repeat acknowledges nothing more and answers
  the same;
- ``through_id`` acknowledges the caller's unread notifications up to and
  including that id and nothing newer, so a notification that arrived after
  the caller's read is not silently acknowledged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    add_user,
    as_user,
    bearer,
    bootstrap,
    events,
    make_app,
    mcp_server_for,
    mcp_sessions,
    onboard_agent,
    run_agents,
)

from athena.core import db

MEMBER = as_user(2)
OTHER = as_user(3)


def _unread(db_file: Path, user_id: int) -> list[int]:
    conn = db.connect(db_file)
    try:
        return [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM notifications WHERE user_id = ? AND read_at IS NULL "
                "ORDER BY id",
                (user_id,),
            )
        ]
    finally:
        conn.close()


@pytest.fixture
def inbox(tmp_path) -> Any:
    """The member (2) and another account (3) each watch an issue the admin
    comments on three times: three unread notifications each, interleaved."""
    app, db_file = make_app(tmp_path, "inbox.db")
    with TestClient(app) as client:
        bootstrap(client)
        add_user(client, "m@e.com", name="Member")
        add_user(client, "o@e.com", name="Other")
        issue = client.post("/issues", json={"title": "watched"}, headers=ADMIN).json()
        for headers in (MEMBER, OTHER):
            watched = client.post(
                "/watches",
                json={"target_kind": "issue", "target_id": issue["id"]},
                headers=headers,
            )
            assert watched.status_code == 204, watched.text
        for n in range(3):
            client.post(
                f"/issues/{issue['id']}/comments",
                json={"body": f"note {n}"},
                headers=ADMIN,
            )
        assert len(_unread(db_file, 2)) == len(_unread(db_file, 3)) == 3
        yield {"app": app, "client": client, "db_file": db_file, "issue": issue}


def _ack(client: TestClient, body: dict, headers=MEMBER):
    return client.post("/notifications/read_all", json=body, headers=headers)


def test_ids_acknowledge_the_callers_own_in_one_call(inbox):
    client, db_file = inbox["client"], inbox["db_file"]
    mine, theirs = _unread(db_file, 2), _unread(db_file, 3)

    answer = _ack(client, {"ids": mine[:2]})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"count": 2, "not_found": []}
    assert _unread(db_file, 2) == mine[2:]
    assert _unread(db_file, 3) == theirs


def test_another_accounts_id_and_a_missing_id_answer_alike(inbox):
    client, db_file = inbox["client"], inbox["db_file"]
    mine, theirs = _unread(db_file, 2), _unread(db_file, 3)
    missing = max(mine + theirs) + 1000

    foreign = _ack(client, {"ids": [theirs[0]]})
    absent = _ack(client, {"ids": [missing]})

    assert foreign.status_code == absent.status_code == 200
    assert foreign.json() == {"count": 0, "not_found": [theirs[0]]}
    assert absent.json() == {"count": 0, "not_found": [missing]}
    assert _unread(db_file, 3) == theirs  # nothing of theirs was touched
    assert _unread(db_file, 2) == mine


def test_acknowledging_is_idempotent(inbox):
    client, db_file = inbox["client"], inbox["db_file"]
    mine = _unread(db_file, 2)
    first = _ack(client, {"ids": mine})
    again = _ack(client, {"ids": mine})
    assert first.json() == {"count": 3, "not_found": []}
    assert again.status_code == 200, again.text
    assert again.json() == {"count": 0, "not_found": []}
    bounded = _ack(client, {"through_id": mine[-1]})
    assert bounded.json() == {"count": 0}


def test_through_id_acknowledges_nothing_newer(inbox):
    """The caller read its inbox through ``read_through``; one more notification
    arrives before it acknowledges. That one stays unread."""
    client, db_file, issue = inbox["client"], inbox["db_file"], inbox["issue"]
    read_through = _unread(db_file, 2)[-1]
    client.post(f"/issues/{issue['id']}/comments", json={"body": "late"}, headers=ADMIN)
    (late,) = [n for n in _unread(db_file, 2) if n > read_through]

    answer = _ack(client, {"through_id": read_through})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"count": 3}
    assert _unread(db_file, 2) == [late]
    assert len(_unread(db_file, 3)) == 4  # the other inbox is untouched


def test_a_notification_the_caller_can_no_longer_see_is_left_alone(inbox):
    """Visibility gates the set form as it gates the single one: a notification
    on an issue in a project made private reads as not found."""
    client, db_file = inbox["client"], inbox["db_file"]
    project = client.post(
        "/projects", json={"name": "Moved", "key": "MOV"}, headers=ADMIN
    ).json()["id"]
    hidden = client.post(
        "/issues", json={"title": "soon hidden", "project_id": project}, headers=ADMIN
    ).json()["id"]
    client.post(
        "/watches", json={"target_kind": "issue", "target_id": hidden}, headers=MEMBER
    )
    client.post(f"/issues/{hidden}/comments", json={"body": "x"}, headers=ADMIN)
    newest = _unread(db_file, 2)[-1]
    flipped = client.put(
        f"/projects/{project}/visibility", json={"visibility": "private"}, headers=ADMIN
    )
    assert flipped.status_code == 200, flipped.text

    answer = _ack(client, {"ids": [newest]})
    assert answer.json() == {"count": 0, "not_found": [newest]}
    bounded = _ack(client, {"through_id": newest})
    assert bounded.json() == {"count": 3}
    assert _unread(db_file, 2) == [newest]


@pytest.mark.parametrize(
    "body",
    [
        {"ids": [1], "through_id": 1},
        {"ids": []},
        {"through_id": 0},
        {"id": [1]},
    ],
)
def test_the_set_form_is_one_of_ids_or_through_id(inbox, body):
    client, db_file = inbox["client"], inbox["db_file"]
    before = _unread(db_file, 2)
    answer = _ack(client, body)
    assert answer.status_code == 422, answer.text
    assert _unread(db_file, 2) == before


def test_no_body_still_acknowledges_everything(inbox):
    client, db_file = inbox["client"], inbox["db_file"]
    answer = client.post("/notifications/read_all", headers=MEMBER)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"count": 3}
    assert _unread(db_file, 2) == []


def test_the_tool_acknowledges_a_set_in_one_call(inbox):
    app, client, db_file = inbox["app"], inbox["client"], inbox["db_file"]
    agent = onboard_agent(client, email="s@e.com", name="Seat")
    seat_id = agent["user"]["id"]
    client.post(
        "/watches",
        json={"target_kind": "issue", "target_id": inbox["issue"]["id"]},
        headers=bearer(agent),
    )
    for n in range(3):
        client.post(
            f"/issues/{inbox['issue']['id']}/comments",
            json={"body": f"for the seat {n}"},
            headers=ADMIN,
        )
    first, second, third = _unread(db_file, seat_id)

    async def scene() -> list[dict]:
        server = mcp_server_for(app, agent, run_id="ack-run", profile="seat")
        async with mcp_sessions(server) as (session,):
            answers = []
            for arguments in (
                {"ids": [first, 10_000]},
                {"through_id": second},
                {},
                {"notification_id": third, "through_id": third},
            ):
                result = await session.call_tool("mark_notification_read", arguments)
                text = result.content[0].text if result.content else "{}"
                answers.append({"error": result.isError, "text": text})
            return answers

    by_ids, by_bound, neither, both = run_agents(scene())
    assert not by_ids["error"] and json.loads(by_ids["text"]) == {
        "count": 1,
        "not_found": [10_000],
    }
    assert not by_bound["error"] and json.loads(by_bound["text"]) == {"count": 1}
    assert neither["error"] and both["error"]  # exactly one form, never "all"
    assert _unread(db_file, seat_id) == [third]


# --- D18: a renewal is an event, not a notification --------------------------------


def test_a_lease_renewal_stays_an_event_and_notifies_nobody(tmp_path):
    app, db_file = make_app(tmp_path, "renew.db")
    with TestClient(app) as client:
        bootstrap(client)
        add_user(client, "m@e.com", name="Owner")  # 2, the accountable assignee
        agent = onboard_agent(client, email="s@e.com", name="Seat")
        seat = bearer(agent)
        issue_id = client.post(
            "/issues", json={"title": "kept alive"}, headers=ADMIN
        ).json()["id"]
        client.put(
            f"/issues/{issue_id}/assignee", json={"assignee_id": 2}, headers=ADMIN
        )
        client.post(
            f"/issues/{issue_id}/delegate",
            json={"user_id": agent["user"]["id"]},
            headers=ADMIN,
        )
        etag = client.get(f"/issues/{issue_id}", headers=seat).headers["ETag"]
        claimed = client.post(
            f"/issues/{issue_id}/claim", headers={**seat, "If-Match": etag}
        )
        assert claimed.status_code == 201, claimed.text
        cursor = client.get("/events", params={"limit": 1}, headers=MEMBER).json()
        renewed = client.post(
            f"/issues/{issue_id}/claim",
            json={"generation": claimed.json()["generation"]},
            headers={**seat, "If-Match": etag},
        )
        assert renewed.status_code == 201, renewed.text

        (renewal,) = events(db_file, "lease_renewed")
        # In the trail and in the assignee's feed...
        feed = client.get(
            "/events", params={"after": cursor["next_after"] - 1}, headers=MEMBER
        ).json()
        assert renewal["id"] in [event["id"] for event in feed["events"]]
        # ...and in nobody's inbox, while the claim itself still notifies.
        conn = db.connect(db_file)
        try:
            notified = {
                verb: [
                    row["user_id"]
                    for row in conn.execute(
                        "SELECT n.user_id FROM notifications n JOIN activity a "
                        "ON a.id = n.event_id WHERE a.verb = ?",
                        (verb,),
                    )
                ]
                for verb in ("claimed", "lease_renewed")
            }
        finally:
            conn.close()
        assert 2 in notified["claimed"]
        assert notified["lease_renewed"] == []
