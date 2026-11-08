"""Work handed off into silence: delegations nobody picked up.

Delegating adds the delegate as a contributor (``issue_contributors.added_at``).
Nothing said when that delegate never claimed the issue and never did any work on
it. These pin the read that does, on both sides of the hand-off:

- the fleet-attention card counts delegations older than the stated threshold with
  no claim and no recorded work BY THE DELEGATE since the delegation;
- the delegator's desk lists them as "waiting on" lines;
- a claimed or worked delegation is not counted, a fresh one is not counted, and
  work by someone other than the delegate does not count as pickup;
- a hidden project's delegations are in neither the count nor the rows for a
  viewer who cannot see that project.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from athena.aegis import fleet_attention, work_progress
from athena.core import db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
ADMIN = {"id": 1, "role": "admin"}
_TS = "%Y-%m-%d %H:%M:%S"


def _app(tmp_path, name="unclaimed.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email="sol@e.com", name="Sol"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _ago(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).strftime(_TS)


def _sql(db_file, statement, params=()):
    conn = db.connect(db_file)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _delegated(client, db_file, agent, title, *, hours_ago=48, **fields):
    issue = client.post("/issues", json={"title": title, **fields}, headers=H1).json()
    sent = client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    assert sent.status_code in (200, 201), sent.text
    _sql(
        db_file,
        "UPDATE issue_contributors SET added_at = ? WHERE issue_id = ? AND user_id = ?",
        (_ago(hours=hours_ago), issue["id"], agent["user"]["id"]),
    )
    return issue


def _claim(client, agent, issue_id):
    read = client.get(f"/issues/{issue_id}", headers=_bearer(agent))
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        headers={
            **_bearer(agent),
            "If-Match": read.headers["ETag"],
            "X-Athena-Run": f"run-{issue_id}",
        },
    )
    assert claimed.status_code in (200, 201), claimed.text


def _ids(lane):
    return {row["issue"]["id"] for row in lane["items"]}


def _signal(projection):
    return next(s for s in projection["signals"] if s["key"] == "delegation_unclaimed")


def test_a_delegation_nobody_picked_up_is_counted_and_a_claimed_or_worked_one_is_not(
    tmp_path,
):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        silent = _delegated(c, db_file, agent, "handed off, silence")
        claimed = _delegated(c, db_file, agent, "handed off, claimed")
        _claim(c, agent, claimed["id"])
        worked = _delegated(c, db_file, agent, "handed off, commented")
        posted = c.post(
            f"/issues/{worked['id']}/comments",
            json={"body": "on it"},
            headers=_bearer(agent),
        )
        assert posted.status_code == 201, posted.text
        # Work by someone else is not the delegate picking it up.
        nudged = _delegated(c, db_file, agent, "handed off, nudged by admin")
        c.post(f"/issues/{nudged['id']}/comments", json={"body": "ping"}, headers=H1)
        fresh = _delegated(c, db_file, agent, "handed off just now", hours_ago=1)

        conn = db.connect(db_file)
        lane = work_progress.unclaimed_delegations(conn, actor=ADMIN)
        card = fleet_attention.build_attention(conn)
        conn.close()

        assert _ids(lane) == {silent["id"], nudged["id"]}
        assert lane["visible_total"] == 2
        assert lane["older_than_hours"] == work_progress.DELEGATION_UNCLAIMED_HOURS
        # Positive controls: claimed, worked and fresh delegations are each absent.
        assert claimed["id"] not in _ids(lane)
        assert worked["id"] not in _ids(lane)
        assert fresh["id"] not in _ids(lane)
        (row,) = [r for r in lane["items"] if r["issue"]["id"] == silent["id"]]
        assert row["delegate"]["name"] == "Sol"
        assert row["delegated_by"]["id"] == 1
        assert row["waiting_seconds"] >= 47 * 3600

        signal = _signal(card)
        assert signal["count"] == 2
        assert signal["href"] == "/admin/agents"
        assert str(work_progress.DELEGATION_UNCLAIMED_HOURS) in signal["scope"]


def test_a_done_or_archived_delegation_is_not_waiting(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        done = _delegated(c, db_file, agent, "finished elsewhere")
        _sql(db_file, "UPDATE issues SET status = 'done' WHERE id = ?", (done["id"],))
        archived = _delegated(c, db_file, agent, "shelved")
        _sql(
            db_file,
            "UPDATE issues SET archived_at = datetime('now') WHERE id = ?",
            (archived["id"],),
        )
        open_one = _delegated(c, db_file, agent, "still open")
        conn = db.connect(db_file)
        lane = work_progress.unclaimed_delegations(conn, actor=ADMIN)
        conn.close()
        assert _ids(lane) == {open_one["id"]}  # positive control: open one counted


def test_the_delegators_desk_has_a_waiting_on_line_and_the_delegates_does_not(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        silent = _delegated(c, db_file, agent, "handed off, silence")
        claimed = _delegated(c, db_file, agent, "handed off, claimed")
        _claim(c, agent, claimed["id"])

        mine = c.get("/desk", headers=H1).json()["work"]["waiting_on"]
        assert [row["issue"]["id"] for row in mine["items"]] == [silent["id"]]
        assert mine["total"] == 1
        assert mine["items"][0]["delegate"]["name"] == "Sol"
        assert "waiting" in mine["meaning"]
        # The delegate is not waiting on itself; its desk shows the delegation in
        # its own inbox, not as a "waiting on" line.
        theirs = c.get("/desk", headers=_bearer(agent)).json()["work"]["waiting_on"]
        assert theirs["items"] == [] and theirs["total"] == 0


def test_a_hidden_projects_delegations_are_in_neither_counts_nor_rows(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        outsider = c.post(
            "/users",
            json={"email": "out@e.com", "name": "Out", "password": "pw"},
            headers=H1,
        ).json()
        project = c.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=H1
        ).json()
        hidden = _delegated(
            c, db_file, agent, "hidden hand-off", project_id=project["id"]
        )
        public = _delegated(c, db_file, agent, "backlog hand-off")
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )

        conn = db.connect(db_file)
        theirs = work_progress.unclaimed_delegations(
            conn, actor=users.get_user(conn, outsider["id"])
        )
        mine = work_progress.unclaimed_delegations(conn, actor=ADMIN)
        card = fleet_attention.build_attention(conn, actor=ADMIN)
        conn.close()

        assert _ids(theirs) == {public["id"]}
        assert theirs["visible_total"] == 1
        # Positive controls: the admin sees both, and so does the admin-only card.
        assert _ids(mine) == {hidden["id"], public["id"]}
        assert mine["visible_total"] == 2
        assert _signal(card)["count"] == 2


def test_reading_writes_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        _delegated(c, db_file, agent, "handed off, silence")
        conn = db.connect(db_file)
        before = conn.total_changes
        work_progress.unclaimed_delegations(conn, actor=ADMIN)
        fleet_attention.build_attention(conn, actor=ADMIN)
        assert conn.total_changes == before
        conn.close()
