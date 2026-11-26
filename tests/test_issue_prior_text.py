"""An issue's replaced title and body are kept, readable and restorable (F10, D20).

WHY: any account allowed to act on an issue could replace its title and body,
and the old text was in no event, no history answer and no undo (FLEET-10). The
conductor recipe keeps an outcome's contract and acceptance criteria in issue
bodies, so one careless or stale agent edit erased the contract for good. A seat
whose lease had lapsed could still do it, blind, with no precondition.

The contract pinned here:

* an edit that changes the title, the body or both keeps the text it replaced,
  in the edit's transaction, keyed to its ``issue_edited`` event; an edit that
  changes nothing stores nothing;
* the issue history names each edit (who, when, which fields) and points at the
  one read that returns the prior text, byte for byte, to a caller who can see
  the issue; a hidden issue's prior text reads as missing;
* undo of an edit restores the prior text exactly and is itself undoable (a
  redo); it refuses, writing nothing, once a later edit has replaced that text;
* a bearer-token caller sending a title or body must send ``If-Match``: 428
  ``precondition_required`` without it, 412 ``precondition_failed`` with a stale
  one, each with its registered ``next``. The browser's form and every other
  field are unaffected;
* MCP ``update_issue`` is no longer marked destructive, and follows the 428;
* imported issues and imported history carry no prior text.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest
from support import (
    ADMIN,
    PASSWORD,
    McpAgent,
    Meter,
    add_user,
    bootstrap,
    client_as,
    lapse_lease,
    login,
    make_app,
    mcp_server_for,
    mcp_sessions,
    onboard_agent,
    run_agents,
)

from athena.core import db, packet_time, portability, refusal_codes
from athena.main import create_app
from athena.mcp import server as mcp_server

CONTRACT = "## Success criteria\r\nCSV with a header row; UTF-8; 13 tests pass.  \n\n"
TITLE = "Contract: widget export ✓"


def _world(tmp_path: Path, name: str = "prior.db"):
    app, db_file = make_app(tmp_path, name)
    return app, db_file


def _project(client: TestClient, key: str = "WRK", visibility: str | None = None):
    body = {"name": f"Project {key}", "key": key}
    if visibility is not None:
        body["visibility"] = visibility
    made = client.post("/projects", json=body, headers=ADMIN)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _issue(
    client: TestClient, *, project_id=None, title=TITLE, body=CONTRACT, seat=None
):
    made = client.post(
        "/issues",
        json={"title": title, "body": body, "project_id": project_id},
        headers=ADMIN,
    )
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    if seat is not None:
        handed = client.post(
            f"/issues/{issue_id}/delegate",
            json={"user_id": seat["user"]["id"]},
            headers=ADMIN,
        )
        assert handed.status_code == 201, handed.text
    return issue_id


def _etag(client: TestClient, issue_id: int) -> str:
    return client.get(f"/issues/{issue_id}", headers=ADMIN).headers["ETag"]


def _edit(seat_http: TestClient, client: TestClient, issue_id: int, **fields):
    """A bearer edit made the way the contract asks: with the current tag."""
    edited = seat_http.patch(
        f"/issues/{issue_id}",
        json=fields,
        headers={"If-Match": _etag(client, issue_id)},
    )
    assert edited.status_code == 200, edited.text
    return edited


def _edits(client: TestClient, issue_id: int) -> list[dict]:
    """The issue's ``issue_edited`` events, newest first, as the admin sees them."""
    feed = client.get(
        "/activity",
        params={"target_kind": "issue", "target_id": issue_id, "verb": "issue_edited"},
        headers=ADMIN,
    )
    assert feed.status_code == 200, feed.text
    return feed.json()


def _prior(client: TestClient, issue_id: int, event_id: int, headers=ADMIN):
    return client.get(f"/issues/{issue_id}/prior-texts/{event_id}", headers=headers)


def _rows(db_file: Path) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM issue_prior_texts ORDER BY event_id")
        ]
    finally:
        conn.close()


def _seat(client: TestClient, app, email="sol@e.com", name="Sol"):
    onboarded = onboard_agent(client, email=email, name=name)
    return onboarded, client_as(app, token=onboarded)


# --- storing and reading the prior text ------------------------------------------


def test_each_edit_keeps_the_text_it_replaced_and_nothing_for_a_no_op(tmp_path):
    app, db_file = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)

        _edit(seat_http, client, issue_id, title="Contract: v2")
        _edit(seat_http, client, issue_id, body="Nothing to do.")
        _edit(seat_http, client, issue_id, title="Contract: v3", body="Still nothing.")
        # A resubmit of identical text is no edit: no event, no prior text.
        _edit(seat_http, client, issue_id, title="Contract: v3", body="Still nothing.")

        title_only, body_only, both = reversed(_edits(client, issue_id))
        assert len(_edits(client, issue_id)) == 3

        def read(event):
            answer = _prior(client, issue_id, event["id"])
            assert answer.status_code == 200, answer.text
            return answer.json()

        first = read(title_only)
        assert first["changed"] == ["title"]
        assert first["prior_title"] == TITLE
        assert first["prior_body"] is None
        second = read(body_only)
        assert second["changed"] == ["body"]
        assert second["prior_title"] is None
        # Byte for byte: CRLF, trailing spaces and the non-ASCII mark survive.
        assert second["prior_body"].encode() == CONTRACT.encode()
        third = read(both)
        assert third["changed"] == ["title", "body"]
        assert (third["prior_title"], third["prior_body"]) == (
            "Contract: v2",
            "Nothing to do.",
        )
        # Who edited and when, as an RFC 3339 UTC stamp; who wrote the replaced
        # text (the admin wrote the original contract, the seat the rest).
        assert third["edited_by"] == {"id": seat["user"]["id"], "name": "Sol"}
        assert third["edited_at"].endswith("Z") and "T" in third["edited_at"]
        assert first["authored_by"] == {"id": 1, "is_agent": False}
        assert third["authored_by"] == {"id": seat["user"]["id"], "is_agent": True}

    # One row per edit that changed something; an unchanged field stores nothing.
    rows = _rows(db_file)
    assert [(r["prior_title"] is None, r["prior_body"] is None) for r in rows] == [
        (False, True),
        (True, False),
        (False, False),
    ]


def test_the_history_names_each_edit_and_points_at_its_prior_text(tmp_path):
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        _edit(seat_http, client, issue_id, body="Nothing to do.")
        event = _edits(client, issue_id)[0]

        history = client.get(f"/issues/{issue_id}/history", headers=ADMIN)
        assert history.status_code == 200, history.text
        edits = [i for i in history.json()["items"] if i["signal"] == "edit"]
        assert len(edits) == 1
        item = edits[0]
        assert item["actor"] == {"id": seat["user"]["id"], "name": "Sol"}
        # The history is a packet (A13); the activity row keeps the stored shape.
        assert item["at"] == packet_time.rfc3339(event["created_at"])
        assert item["changed"] == ["body"]
        assert item["source"] == {"kind": "activity", "id": event["id"]}
        assert item["prior_text"] == {
            "event_id": event["id"],
            "via": f"GET /issues/{issue_id}/prior-texts/{event['id']}",
            "next": {
                "tool": "get_issue_prior_text",
                "arguments": {"issue_id": issue_id, "event_id": event["id"]},
            },
        }
        # The history names the edit; it never carries the replaced text.
        assert "13 tests pass" not in history.text


def test_no_other_answer_carries_the_replaced_text(tmp_path):
    """The feed, notifications, the desk and the work-context packet read the
    trail; none of them grows by the size of a replaced body."""
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        other, other_http = _seat(client, app, email="bo@e.com", name="Bo")
        issue_id = _issue(client, seat=seat)
        client.post(
            f"/issues/{issue_id}/delegate",
            json={"user_id": other["user"]["id"]},
            headers=ADMIN,
        )
        _edit(seat_http, client, issue_id, body="Nothing to do.")

        for http, path in (
            (client, "/activity"),
            (client, f"/activity?target_kind=issue&target_id={issue_id}"),
            (client, "/events"),
            (other_http, "/notifications"),
            (other_http, "/desk"),
            (seat_http, "/desk"),
            (seat_http, f"/issues/{issue_id}/work-context"),
            (client, f"/issues/{issue_id}"),
        ):
            answer = http.get(path, headers=ADMIN if http is client else None)
            assert answer.status_code == 200, (path, answer.text)
            assert "13 tests pass" not in answer.text, path


def test_a_hidden_issue_prior_text_reads_as_missing(tmp_path):
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        outsider = add_user(client, "o@e.com", name="Outsider")
        private = _project(client, "PRV", visibility="private")
        issue_id = _issue(client, project_id=private)
        edited = client.patch(
            f"/issues/{issue_id}", json={"body": "replaced"}, headers=ADMIN
        )
        assert edited.status_code == 200, edited.text
        event_id = _edits(client, issue_id)[0]["id"]
        assert _prior(client, issue_id, event_id).status_code == 200

        as_outsider = {"X-Athena-Actor": str(outsider["id"])}
        hidden = _prior(client, issue_id, event_id, headers=as_outsider)
        missing = _prior(client, issue_id, event_id + 1000, headers=as_outsider)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == {"detail": "no such issue", "code": "not_found"}
        assert "13 tests pass" not in hidden.text

        # An event of another issue, or one with no prior text, is missing too.
        other = _issue(client, title="Other")
        assert _prior(client, other, event_id).status_code == 404
        created = client.get(
            "/activity",
            params={"target_kind": "issue", "target_id": other, "verb": "created"},
            headers=ADMIN,
        ).json()[0]
        answer = _prior(client, other, created["id"])
        assert answer.status_code == 404
        assert answer.json()["code"] == "not_found"


def test_prior_text_rows_are_append_only_and_native_only(tmp_path):
    app, db_file = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue_id = _issue(client)
        client.patch(f"/issues/{issue_id}", json={"title": "New"}, headers=ADMIN)
        event_id = _edits(client, issue_id)[0]["id"]
    conn = db.connect(db_file)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE issue_prior_texts SET prior_title = 'forged'")
        # A delete needs its removal event on the trail first (migration 0096;
        # tests/test_prior_text_removal.py).
        with pytest.raises(sqlite3.IntegrityError, match="removal event"):
            conn.execute("DELETE FROM issue_prior_texts")
        # A row for an event that is not this issue's native edit is refused.
        created = conn.execute(
            "SELECT id FROM activity WHERE verb = 'created' AND target_id = ?",
            (issue_id,),
        ).fetchone()["id"]
        with pytest.raises(sqlite3.IntegrityError, match="issue_edited"):
            conn.execute(
                "INSERT INTO issue_prior_texts (event_id, issue_id, prior_title, "
                "after_title_sha256) VALUES (?, ?, 'x', ?)",
                (created, issue_id, "0" * 64),
            )
        assert conn.execute("SELECT COUNT(*) FROM issue_prior_texts").fetchone()[0] == 1
        assert event_id
    finally:
        conn.close()


# --- undo ---------------------------------------------------------------------------


def test_undo_restores_the_prior_text_exactly_and_redo_restores_the_edit(tmp_path):
    app, db_file = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        _edit(seat_http, client, issue_id, title="Contract: whatever", body="Nothing.")
        edit = _edits(client, issue_id)[0]

        # The operator undoes the seat's edit, over a bearer token, with no tag:
        # the still-in-force check is the precondition.
        undone = client.post(f"/activity/{edit['id']}/undo", headers=ADMIN)
        assert undone.status_code == 201, undone.text
        reversal = undone.json()["reversal"]
        assert reversal["verb"] == "issue_edited"
        assert reversal["reverses_event_id"] == edit["id"]
        current = client.get(f"/issues/{issue_id}", headers=ADMIN)
        assert current.json()["title"] == TITLE
        assert current.json()["body"].encode() == CONTRACT.encode()
        assert undone.json()["target_etag"] == current.headers["ETag"]

        # The reversal kept the text it replaced, so undoing it is a redo.
        redo = client.post(f"/activity/{reversal['id']}/undo", headers=ADMIN)
        assert redo.status_code == 201, redo.text
        again = client.get(f"/issues/{issue_id}", headers=ADMIN).json()
        assert (again["title"], again["body"]) == ("Contract: whatever", "Nothing.")

        # Single-use, as every undo.
        twice = client.post(f"/activity/{edit['id']}/undo", headers=ADMIN)
        assert twice.status_code == 409
        assert twice.json()["code"] == "undo_already_reversed"
    assert len(_rows(db_file)) == 3


def test_undo_by_a_bearer_caller_needs_no_tag(tmp_path):
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        _edit(seat_http, client, issue_id, body="Nothing.")
        edit = _edits(client, issue_id)[0]
        undone = seat_http.post(f"/activity/{edit['id']}/undo")
        assert undone.status_code == 201, undone.text
        body = client.get(f"/issues/{issue_id}", headers=ADMIN).json()["body"]
        assert body == CONTRACT


def test_undo_refuses_once_a_later_edit_replaced_the_text(tmp_path):
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        _edit(seat_http, client, issue_id, body="First rewrite.")
        first = _edits(client, issue_id)[0]
        _edit(seat_http, client, issue_id, body="Second rewrite.")

        refused = client.post(f"/activity/{first['id']}/undo", headers=ADMIN)
        assert refused.status_code == 409
        assert refused.json()["code"] == "undo_no_effect"
        assert refused.json()["code"] in refusal_codes.REFUSAL_CODES
        # Nothing was written: the newer edit stands, and no event was added.
        assert client.get(f"/issues/{issue_id}", headers=ADMIN).json()["body"] == (
            "Second rewrite."
        )
        assert len(_edits(client, issue_id)) == 2

        # A later edit of the OTHER field does not block it: a title edit
        # leaves the body this edit wrote in force.
        _edit(seat_http, client, issue_id, title="Renamed")
        second = _edits(client, issue_id)[1]
        assert second["detail"] == TITLE
        undone = client.post(f"/activity/{second['id']}/undo", headers=ADMIN)
        assert undone.status_code == 201, undone.text
        now = client.get(f"/issues/{issue_id}", headers=ADMIN).json()
        assert (now["title"], now["body"]) == ("Renamed", "First rewrite.")


def test_undo_of_an_edit_recorded_without_prior_text_refuses(tmp_path):
    """An edit from before prior texts were kept has nothing to restore."""
    app, db_file = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue_id = _issue(client)
        client.patch(f"/issues/{issue_id}", json={"body": "x"}, headers=ADMIN)
        edit = _edits(client, issue_id)[0]
    conn = db.connect(db_file)
    try:
        conn.execute("DROP TRIGGER issue_prior_texts_delete_requires_removal_event")
        conn.execute("DELETE FROM issue_prior_texts")
        conn.commit()
    finally:
        conn.close()
    with TestClient(app) as client:
        refused = client.post(f"/activity/{edit['id']}/undo", headers=ADMIN)
        assert refused.status_code == 422
        assert refused.json()["code"] == "undo_not_reversible"
        history = client.get(f"/issues/{issue_id}/history", headers=ADMIN).json()
        item = next(i for i in history["items"] if i["signal"] == "edit")
        assert item["changed"] is None and item["prior_text"] is None


# --- If-Match for bearer-token callers ----------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [{"title": "Blind"}, {"body": "Blind"}, {"title": "Blind", "body": "Blind"}],
)
def test_a_bearer_edit_of_title_or_body_needs_the_current_tag(tmp_path, fields):
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        stale = _etag(client, issue_id)
        client.patch(f"/issues/{issue_id}", json={"priority": "high"}, headers=ADMIN)

        bare = seat_http.patch(f"/issues/{issue_id}", json=fields)
        assert bare.status_code == 428, bare.text
        assert bare.json()["code"] == "precondition_required"
        assert "If-Match" in bare.json()["detail"]
        assert 428 in refusal_codes.REFUSAL_CODES["precondition_required"].statuses

        moved = seat_http.patch(
            f"/issues/{issue_id}", json=fields, headers={"If-Match": stale}
        )
        assert moved.status_code == 412, moved.text
        assert moved.json()["code"] == "precondition_failed"
        assert moved.headers["ETag"] == _etag(client, issue_id)

        # Nothing was written by either refusal.
        current = client.get(f"/issues/{issue_id}", headers=ADMIN).json()
        assert (current["title"], current["body"]) == (TITLE, CONTRACT)
        assert _edits(client, issue_id) == []

        fresh = seat_http.patch(
            f"/issues/{issue_id}",
            json=fields,
            headers={"If-Match": moved.headers["ETag"]},
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.headers["ETag"] == _etag(client, issue_id)


def test_a_wildcard_or_a_list_is_not_a_read(tmp_path):
    """The tag must name the revision read: one strong ETag, as a claim takes."""
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        current = _etag(client, issue_id)
        for value in ("*", f'{current}, "sha256-other"'):
            refused = seat_http.patch(
                f"/issues/{issue_id}", json={"body": "x"}, headers={"If-Match": value}
            )
            assert refused.status_code == 400, refused.text
            assert refused.json()["code"] == "invalid_if_match"
        # A status change alone keeps the wildcard it always accepted.
        moved = seat_http.patch(
            f"/issues/{issue_id}", json={"priority": "low"}, headers={"If-Match": "*"}
        )
        assert moved.status_code == 200, moved.text


def test_the_tag_is_required_whenever_title_or_body_is_sent(tmp_path):
    """Sending the text the issue already has still needs the tag: the rule
    reads the request, not a comparison the caller cannot see."""
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        same = seat_http.patch(f"/issues/{issue_id}", json={"title": TITLE})
        assert same.status_code == 428
        assert same.json()["code"] == "precondition_required"


def test_other_fields_and_other_callers_are_unaffected(tmp_path):
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        admin = bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        label = client.post("/labels", json={"name": "triage"}, headers=ADMIN).json()

        # A bearer caller changing status, priority, assignee or labels: no tag.
        for response in (
            seat_http.patch(f"/issues/{issue_id}", json={"status": "in_progress"}),
            seat_http.patch(f"/issues/{issue_id}", json={"priority": "urgent"}),
            seat_http.put(
                f"/issues/{issue_id}/assignee",
                json={"assignee_id": seat["user"]["id"]},
            ),
            seat_http.post(
                f"/issues/{issue_id}/labels", json={"label_id": label["id"]}
            ),
        ):
            assert response.status_code in (200, 201), response.text

        # The trusted actor header (no bearer token) is unaffected.
        edited = client.patch(
            f"/issues/{issue_id}", json={"title": "By header"}, headers=ADMIN
        )
        assert edited.status_code == 200, edited.text

        # The browser's form posts no tag and still saves, keeping the prior text.
        browser = TestClient(app)
        login(browser, admin["email"], PASSWORD)
        saved = browser.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "By form", "body": "Form body"},
            follow_redirects=False,
        )
        assert saved.status_code == 303, saved.text
        now = client.get(f"/issues/{issue_id}", headers=ADMIN).json()
        assert (now["title"], now["body"]) == ("By form", "Form body")
        latest = _edits(client, issue_id)[0]
        prior = _prior(client, issue_id, latest["id"]).json()
        assert (prior["prior_title"], prior["prior_body"]) == ("By header", CONTRACT)


def test_a_bearer_link_mention_on_an_issue_needs_the_tag(tmp_path):
    """link-mention rewrites the issue's body, so it is a body edit."""
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        project = _project(client)
        target = _issue(client, project_id=project, title="Target", body="")
        source = _issue(
            client, project_id=project, title="Source", body="see WRK-1 here", seat=seat
        )
        mention = {"target_kind": "issue", "target_id": target}
        bare = seat_http.post(f"/issues/{source}/link-mention", json=mention)
        assert bare.status_code == 428, bare.text
        assert bare.json()["code"] == "precondition_required"
        tagged = seat_http.post(
            f"/issues/{source}/link-mention",
            json=mention,
            headers={"If-Match": _etag(client, source)},
        )
        assert tagged.status_code == 200, tagged.text
        prior = _prior(client, source, _edits(client, source)[0]["id"]).json()
        assert prior["prior_body"] == "see WRK-1 here"


def test_a_seat_whose_lease_lapsed_still_edits_with_a_fresh_tag(clock, tmp_path):
    """FLEET-10's third sentence is narrowed, not closed: the lapsed seat can no
    longer edit blind, but a fresh read gives it the tag. F10 adds no lease
    requirement; the edit is kept and can be undone."""
    app, db_file = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat, seat_http = _seat(client, app)
        issue_id = _issue(client, seat=seat)
        claimed = seat_http.post(
            f"/issues/{issue_id}/claim",
            json={"lease_seconds": 60},
            headers={"If-Match": _etag(client, issue_id)},
        )
        assert claimed.status_code == 201, claimed.text
        lapse_lease(clock, db_file, issue_id)
        assert (
            client.get(f"/issues/{issue_id}/lease", headers=ADMIN).json()["active"]
            is False
        )

        blind = seat_http.patch(f"/issues/{issue_id}", json={"body": "lapsed edit"})
        assert blind.status_code == 428
        tagged = _edit(seat_http, client, issue_id, body="lapsed edit")
        assert tagged.json()["body"] == "lapsed edit"
        prior = _prior(client, issue_id, _edits(client, issue_id)[0]["id"]).json()
        assert prior["prior_body"] == CONTRACT


# --- MCP ----------------------------------------------------------------------------


def test_update_issue_is_not_destructive_and_says_what_to_do_on_428():
    hints = mcp_server.WRITE_HINTS["update_issue"]
    assert hints.destructive is False
    declared = mcp_server.declared()["update_issue"]
    assert declared.hints == mcp_server.SETS
    assert mcp_server.TOOL_SCOPES["get_issue_prior_text"] == mcp_server.READ_ONLY
    assert "get_issue_prior_text" in mcp_server.PROFILES["conductor"]


def test_mcp_update_issue_follows_the_428_to_a_tagged_edit(tmp_path, monkeypatch):
    from athena import config

    monkeypatch.setattr(config, "TOKEN_RATE_LIMIT_PER_MINUTE", 0)
    app, _ = _world(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seat = onboard_agent(client, profile="conductor")
        issue_id = _issue(client, seat=seat)
        server = mcp_server_for(app, seat, run_id="f10-run")

        async def play():
            async with mcp_sessions(server) as (session,):
                listed = await session.list_tools()
                tool = next(t for t in listed.tools if t.name == "update_issue")
                assert tool.annotations.destructiveHint is False
                assert "428" in tool.description and "if_match" in tool.description
                agent = McpAgent(
                    "sol",
                    seat["user"]["id"],
                    session,
                    Meter(),
                    frozenset(mcp_server.PROFILES["conductor"]),
                )
                refused = await agent.refused(
                    "precondition_required",
                    "update_issue",
                    issue_id=issue_id,
                    body="Rewritten",
                )
                assert refused["next"]["tool"] == "get_issue"
                read = await agent.follow(refused, issue_id=issue_id)
                edited = await agent.call(
                    "update_issue",
                    issue_id=issue_id,
                    body="Rewritten",
                    if_match=read["_etag"],
                )
                assert edited["body"] == "Rewritten"
                history = await agent.call("get_issue_history", issue_id=issue_id)
                item = next(i for i in history["items"] if i["signal"] == "edit")
                follow = item["prior_text"]["next"]
                prior = await agent.call(follow["tool"], **follow["arguments"])
                assert prior["prior_body"] == CONTRACT
                assert prior["authored_by"] == {"id": 1, "is_agent": False}

        run_agents(play())


# --- import -------------------------------------------------------------------------


def test_imported_history_carries_no_prior_text(tmp_path):
    """Export carries the current text only; an imported edit event stores no
    prior text, has none to read, and cannot be undone."""
    source_app = create_app(tmp_path / "source.db")
    with TestClient(source_app) as client:
        bootstrap(client)
        project = _project(client, "EXP")
        issue_id = _issue(client, project_id=project)
        client.patch(f"/issues/{issue_id}", json={"body": "Rewritten"}, headers=ADMIN)
        assert len(_rows(tmp_path / "source.db")) == 1
    source = db.connect(tmp_path / "source.db")
    try:
        bundle = portability.export_bundle(source, "project", project)
    finally:
        source.close()
    assert "13 tests pass" not in json.dumps(bundle)
    assert any(row["verb"] == "issue_edited" for row in bundle["activity"])

    target_app = create_app(tmp_path / "target.db")
    with TestClient(target_app) as client:
        bootstrap(client)
    target = db.connect(tmp_path / "target.db")
    try:
        manifest = portability.build_import_manifest(target, bundle)
        assert manifest["ok"], manifest["conflicts"]
        result = portability.replay_import_manifest(target, bundle, manifest)
    finally:
        target.close()
    imported_issue = result["id_map"]["issues"][0]["target_id"]
    assert _rows(tmp_path / "target.db") == []

    with TestClient(target_app) as client:
        edit = _edits(client, imported_issue)[0]
        assert edit["imported_at"] is not None
        assert _prior(client, imported_issue, edit["id"]).status_code == 404
        refused = client.post(f"/activity/{edit['id']}/undo", headers=ADMIN)
        assert refused.json()["code"] == "undo_imported_event"
        history = client.get(f"/issues/{imported_issue}/history", headers=ADMIN)
        item = next(i for i in history.json()["items"] if i["signal"] == "edit")
        assert item["prior_text"] is None
