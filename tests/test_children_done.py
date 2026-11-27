"""A conductor hears once that an outcome's slices are all done (AD12, D34).

WHY: in docs/plans/reports/agent-day-2026-11-26.md (DAY-10) one closed slice
left the conductor nine unread notifications, most of them approval
bookkeeping on that one issue, and nothing said that every slice of the
outcome was now done. Two changes answer it here:

* ``approval_consumed`` stays an event and notifies no one: the act it gated is
  its own event and notifies as before;
* when the last open child of an issue closes, the parent's creator and
  assignee each get one ``children_done`` notification, from one event on the
  parent. It closes nothing, archived children do not count, a parent already
  closed is told nothing, and whoever closed the last child is not told about
  their own act.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from support import (
    ADMIN,
    add_user,
    as_user,
    bearer,
    bootstrap,
    make_app,
    onboard_agent,
)

from athena.core import db


def _issue(client, title, *, parent_id=None, assignee_id=None, headers=ADMIN):
    body = {"title": title, "parent_id": parent_id, "assignee_id": assignee_id}
    made = client.post("/issues", json=body, headers=headers)
    assert made.status_code == 201, made.text
    return made.json()


def _close(client, issue_id, headers=ADMIN):
    done = client.patch(f"/issues/{issue_id}", json={"status": "done"}, headers=headers)
    assert done.status_code == 200, done.text


def _inbox(db_file, user_id, verb):
    conn = db.connect(db_file)
    try:
        return conn.execute(
            "SELECT a.target_id, a.actor_id, a.detail FROM notifications n "
            "JOIN activity a ON a.id = n.event_id WHERE n.user_id = ? AND a.verb = ? "
            "ORDER BY a.id",
            (user_id, verb),
        ).fetchall()
    finally:
        conn.close()


def _events(db_file, verb):
    conn = db.connect(db_file)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT target_id, actor_id FROM activity WHERE verb = ? ORDER BY id",
                (verb,),
            )
        ]
    finally:
        conn.close()


def test_the_last_child_closing_tells_the_parents_owners_once(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        owner = add_user(client, "owner@e.com", name="Owner")
        nova = onboard_agent(client, email="nova@e.com", name="Nova")
        nova_id = nova["user"]["id"]
        outcome = _issue(
            client, "Outcome", assignee_id=owner["id"], headers=bearer(nova)
        )
        first = _issue(client, "Slice one", parent_id=outcome["id"])
        second = _issue(client, "Slice two", parent_id=outcome["id"])
        archived = _issue(client, "Abandoned", parent_id=outcome["id"])
        gone = client.post(f"/issues/{archived['id']}/archive", headers=ADMIN)
        assert gone.status_code in (200, 204), gone.text

        _close(client, first["id"])
        assert _events(db_file, "children_done") == []
        _close(client, second["id"])

        assert _events(db_file, "children_done") == [
            {"target_id": outcome["id"], "actor_id": 1}
        ]
        for user_id in (nova_id, owner["id"]):
            (row,) = _inbox(db_file, user_id, "children_done")
            assert row["target_id"] == outcome["id"]
            assert row["detail"] == "all 2 sub-issues done"
        # The admin closed the last slice: not told about its own act.
        assert _inbox(db_file, 1, "children_done") == []

        # Reopening and closing a child again tells them again, once.
        reopened = client.patch(
            f"/issues/{second['id']}", json={"status": "open"}, headers=ADMIN
        )
        assert reopened.status_code == 200, reopened.text
        _close(client, second["id"])
        assert len(_inbox(db_file, nova_id, "children_done")) == 2

        # A parent already closed is told nothing.
        _close(client, outcome["id"])
        third = _issue(client, "Late slice", parent_id=outcome["id"])
        _close(client, third["id"])
        assert len(_events(db_file, "children_done")) == 2


def test_an_approval_consumed_is_in_the_trail_and_in_no_inbox(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        sol_id = sol["user"]["id"]
        issue = _issue(client, "Gated", assignee_id=sol_id)
        watch = client.post(
            "/watches",
            json={"target_kind": "issue", "target_id": issue["id"]},
            headers=ADMIN,
        )
        assert watch.status_code == 204, watch.text
        gated = client.put(
            f"/approvals/policies/{sol_id}",
            json={"action_kind": "issue.close"},
            headers=ADMIN,
        )
        assert gated.status_code == 200, gated.text
        asked = client.patch(
            f"/issues/{issue['id']}", json={"status": "done"}, headers=bearer(sol)
        )
        approval_id = asked.json()["approval"]["id"]
        decided = client.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": "approve"},
            headers=ADMIN,
        )
        assert decided.status_code == 200, decided.text
        _close(client, issue["id"], headers=bearer(sol))

    assert _events(db_file, "approval_consumed") == [
        {"target_id": issue["id"], "actor_id": sol_id}
    ]
    assert _inbox(db_file, 1, "approval_consumed") == []
    # The close it gated still notifies the watching admin.
    assert [row["target_id"] for row in _inbox(db_file, 1, "changed_status")] == [
        issue["id"]
    ]


def test_an_approval_decision_accepts_the_state_it_produces(tmp_path):
    """AD13: `approved` and `rejected`, the states a decision reads back as, are
    accepted as the decision; anything else is still refused."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        sol_id = sol["user"]["id"]
        gated = client.put(
            f"/approvals/policies/{sol_id}",
            json={"action_kind": "issue.close"},
            headers=ADMIN,
        )
        assert gated.status_code == 200, gated.text
        states = []
        for decision in ("approved", "rejected"):
            issue = _issue(client, f"Gated {decision}", assignee_id=sol_id)
            asked = client.patch(
                f"/issues/{issue['id']}", json={"status": "done"}, headers=bearer(sol)
            )
            approval_id = asked.json()["approval"]["id"]
            decided = client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": decision},
                headers=ADMIN,
            )
            assert decided.status_code == 200, decided.text
            states.append(decided.json()["state"])
        assert states == ["approved", "rejected"]
        refused = client.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": "maybe"},
            headers=ADMIN,
        )
        assert refused.status_code == 422


def test_the_grouped_inbox_is_one_row_per_target_and_acknowledges_by_group(
    tmp_path,
):
    """AD12: GET /notifications/grouped folds the rows on one target into one,
    with the newest row whole, and its unread_ids acknowledge that group only."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        owner = add_user(client, "owner@e.com", role="member")
        busy = _issue(client, "Busy", assignee_id=owner["id"])
        quiet = _issue(client, "Quiet", assignee_id=owner["id"])
        for n in range(3):
            said = client.post(
                f"/issues/{busy['id']}/comments",
                json={"body": f"note {n}"},
                headers=ADMIN,
            )
            assert said.status_code == 201, said.text
        said = client.post(
            f"/issues/{quiet['id']}/comments", json={"body": "one"}, headers=ADMIN
        )
        assert said.status_code == 201, said.text
        mine = as_user(owner["id"])
        rows = client.get("/notifications", params={"unread": True}, headers=mine)
        grouped = client.get(
            "/notifications/grouped", params={"unread": True}, headers=mine
        )
        assert grouped.status_code == 200, grouped.text
        body = grouped.json()
        assert body["complete"] is True
        assert body["rows_scanned"] == len(rows.json())
        by_target = {item["target_id"]: item for item in body["items"]}
        assert set(by_target) == {busy["id"], quiet["id"]}
        assert by_target[busy["id"]]["count"] >= 3
        assert body["items"][0]["target_id"] == quiet["id"]  # newest first
        assert by_target[busy["id"]]["newest"]["id"] == max(
            by_target[busy["id"]]["unread_ids"]
        )
        acked = client.post(
            "/notifications/read_all",
            json={"ids": by_target[busy["id"]]["unread_ids"]},
            headers=mine,
        )
        assert acked.status_code == 200, acked.text
        left = client.get(
            "/notifications/grouped", params={"unread": True}, headers=mine
        ).json()
        assert [item["target_id"] for item in left["items"]] == [quiet["id"]]


def test_the_desk_preview_groups_on_request(tmp_path):
    """AD12: GET /desk?group=issue folds signals.notifications one row per
    target; the default keeps rows."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        owner = add_user(client, "owner@e.com", role="member")
        busy = _issue(client, "Busy", assignee_id=owner["id"])
        for n in range(3):
            said = client.post(
                f"/issues/{busy['id']}/comments",
                json={"body": f"note {n}"},
                headers=ADMIN,
            )
            assert said.status_code == 201, said.text
        mine = as_user(owner["id"])
        rows = client.get("/desk", headers=mine).json()["signals"]["notifications"]
        grouped = client.get("/desk", params={"group": "issue"}, headers=mine).json()[
            "signals"
        ]["notifications"]
        assert len(rows) >= 3
        assert [group["target_id"] for group in grouped] == [busy["id"]]
        assert grouped[0]["count"] == len(rows)
        assert sorted(grouped[0]["unread_ids"]) == sorted(row["id"] for row in rows)
