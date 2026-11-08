"""Alive is not the same as making progress.

A check-in says a run reported; a lease renewal says the clock was extended. Neither
says any work happened. On a quiet fleet both can keep a claim looking healthy for
weeks while nothing moves and a request sits waiting on the operator. These tests pin
the read-only projection that keeps the two apart:

- the last real work on claimed work is shown separately from the last check-in or
  lease renewal, and a heartbeat never resets the work age;
- expired leases are shown as expired, with holder and age, and reading them writes
  nothing (they are never cleared or swept);
- in-progress issues with no recorded work for longer than the stated threshold are
  listed, and unknown times say "unknown", never "fresh";
- the oldest request waiting on the operator is named and linked to its own record;
- visibility applies to the counts and the rows alike.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from athena.aegis import fleet_attention, work_progress
from athena.core import db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
ADMIN = {"id": 1, "role": "admin"}
_TS = "%Y-%m-%d %H:%M:%S"


def _app(tmp_path, name="progress.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _login(client):
    client.post(
        "/login", data={"email": "a@e.com", "password": "pw"}, follow_redirects=False
    )


def _agent(client, email="sol@e.com"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _heartbeat(client, agent, issue_id):
    beat = client.put(
        "/agent-runs/heartbeat",
        json={"run_id": f"run-{issue_id}"},
        headers=_bearer(agent),
    )
    assert beat.status_code == 200, beat.text


def _claimed_issue(client, agent, *, title):
    issue = client.post("/issues", json={"title": title}, headers=H1).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    read = client.get(f"/issues/{issue['id']}", headers=_bearer(agent))
    claimed = client.post(
        f"/issues/{issue['id']}/claim",
        headers={
            **_bearer(agent),
            "If-Match": read.headers["ETag"],
            "X-Athena-Run": f"run-{issue['id']}",
        },
    )
    assert claimed.status_code in (200, 201), claimed.text
    _heartbeat(client, agent, issue["id"])
    return issue


def _sql(db_file, statement, params=()):
    conn = db.connect(db_file)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _ago(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).strftime(_TS)


def _set_in_progress(db_file, issue_id):
    # Direct, so no changed_status event is written: the test controls the work trail.
    _sql(db_file, "UPDATE issues SET status = 'in_progress' WHERE id = ?", (issue_id,))


def _backdate_work(db_file, issue_id, stamp):
    _sql(
        db_file,
        "UPDATE activity SET created_at = ? WHERE target_kind = 'issue' "
        "AND target_id = ? AND verb IN ({})".format(
            ",".join("?" for _ in work_progress.SUBSTANTIVE_WORK_VERBS)
        ),
        (stamp, issue_id, *work_progress.SUBSTANTIVE_WORK_VERBS),
    )


def _panel(client):
    return " ".join(client.get("/aegis/dashboard?panel=fleet-attention").text.split())


def _active_work_item(client, issue_id):
    page = client.get("/fleet/active-work", headers=H1).json()
    return next(item for item in page["items"] if item["issue"]["id"] == issue_id)


# ── a heartbeat after old work does not make the item fresh ─────────────────


def test_a_heartbeat_after_old_work_does_not_make_claimed_work_fresh(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue = _claimed_issue(c, agent, title="quiet but reporting")
        posted = c.post(
            f"/issues/{issue['id']}/comments",
            json={"body": "started on it"},
            headers=_bearer(agent),
        )
        assert posted.status_code == 201, posted.text
        old = _ago(days=10)
        _backdate_work(db_file, issue["id"], old)
        # The run keeps reporting after the work stopped.
        _heartbeat(c, agent, issue["id"])

        item = _active_work_item(c, issue["id"])
        progress = item["progress"]
        assert progress["last_work_at"] == old
        assert progress["work_age_seconds"] >= 10 * 86400 - 60
        assert progress["work_state"] == "stale"
        # The check-in is fresh and shown separately; it did not reset the work age.
        assert (
            progress["last_checkin_at"] is not None
            and progress["last_checkin_at"] > old
        )
        assert progress["last_lease_activity_at"] is not None
        # The agent's own desk says the same about the issue it holds.
        held = c.get("/desk", headers=_bearer(agent)).json()["work"]["leases"]["items"]
        assert held[0]["progress"]["work_state"] == "stale"
        assert held[0]["progress"]["last_work_at"] == old

        # Positive control: real work now makes it recent.
        c.post(
            f"/issues/{issue['id']}/comments",
            json={"body": "next step done"},
            headers=_bearer(agent),
        )
        progress = _active_work_item(c, issue["id"])["progress"]
        assert progress["work_state"] == "recent"
        assert progress["last_work_at"] > old


def test_lease_bookkeeping_and_check_ins_are_not_work():
    # Stated explicitly: these say "still holding" or "still reporting", not "worked".
    for verb in ("claimed", "lease_renewed", "claim_yielded", "claim_handoff_resumed"):
        assert verb not in work_progress.SUBSTANTIVE_WORK_VERBS
    for verb in ("commented", "issue_edited", "changed_status", "claim_completed"):
        assert verb in work_progress.SUBSTANTIVE_WORK_VERBS


# ── expired leases: shown expired, never written ────────────────────────────


def test_an_expired_lease_renders_expired_and_nothing_is_written(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        active = _claimed_issue(c, agent, title="still held")
        lapsed = _claimed_issue(c, agent, title="lapsed hold")
        _sql(
            db_file,
            "UPDATE issue_leases SET expires_at = ? WHERE issue_id = ?",
            (_ago(hours=3), lapsed["id"]),
        )

        conn = db.connect(db_file)
        before_rows = [
            tuple(r)
            for r in conn.execute("SELECT * FROM issue_leases ORDER BY issue_id")
        ]
        before_changes = conn.total_changes
        progress = work_progress.build_progress(conn, actor=ADMIN)
        fleet_attention.build_attention(conn, actor=ADMIN)
        assert conn.total_changes == before_changes
        after_rows = [
            tuple(r)
            for r in conn.execute("SELECT * FROM issue_leases ORDER BY issue_id")
        ]
        conn.close()
        assert after_rows == before_rows  # K1: shown, never cleared or swept

        expired = progress["expired_leases"]
        assert expired["visible_total"] == 1
        (row,) = expired["items"]
        assert row["issue"]["id"] == lapsed["id"]
        assert row["state"] == "expired"
        assert row["holder"]["name"] == "Sol"
        assert 3 * 3600 - 60 <= row["expired_for_seconds"] <= 3 * 3600 + 60
        # Positive control: the active lease is not listed as expired.
        assert active["id"] not in [r["issue"]["id"] for r in expired["items"]]

        _login(c)
        html = _panel(c)
        assert "lapsed hold" in html and "expired" in html and "Sol" in html
        conn = db.connect(db_file)
        assert [
            tuple(r)
            for r in conn.execute("SELECT * FROM issue_leases ORDER BY issue_id")
        ] == before_rows
        conn.close()


# ── the oldest request waiting on the operator ──────────────────────────────


def test_a_recorded_answer_clears_only_its_own_request(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        conn = db.connect(db_file)
        conn.execute(
            "INSERT INTO approval_requests (action_kind, target_kind, target_id, "
            "requested_by, created_at) VALUES ('issue.close', 'issue', 1, 1, ?)",
            (_ago(days=3),),
        )
        conn.execute(
            "INSERT INTO approval_requests (action_kind, target_kind, target_id, "
            "requested_by, created_at) VALUES ('issue.close', 'issue', 2, 1, ?)",
            (_ago(days=1),),
        )
        conn.commit()
        older, newer = [
            r["id"]
            for r in conn.execute("SELECT id FROM approval_requests ORDER BY id")
        ]
        first = work_progress.build_progress(conn, actor=ADMIN)[
            "oldest_waiting_on_operator"
        ]
        conn.close()
        assert first["kind"] == "approval_request" and first["id"] == older
        assert first["pending_total"] == 2
        assert first["link"] == f"/admin/agents#approval-{older}"
        assert first["age_seconds"] >= 3 * 86400 - 60

        decided = c.post(
            f"/approvals/{older}/decision", json={"decision": "approve"}, headers=H1
        )
        assert decided.status_code == 200, decided.text

        conn = db.connect(db_file)
        second = work_progress.build_progress(conn, actor=ADMIN)[
            "oldest_waiting_on_operator"
        ]
        state = conn.execute(
            "SELECT state FROM approval_requests WHERE id = ?", (newer,)
        ).fetchone()["state"]
        conn.close()
        assert second["id"] == newer and second["pending_total"] == 1
        assert state == "pending"  # the other request is untouched

        # The link lands on that request's own row.
        _login(c)
        assert f'id="approval-{newer}"' in c.get("/admin/agents").text


def test_nothing_waiting_says_none_rather_than_a_fake_age(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    assert (
        work_progress.build_progress(conn, actor=ADMIN)["oldest_waiting_on_operator"]
        is None
    )
    conn.close()


# ── visibility: counts and rows alike ───────────────────────────────────────


def test_a_hidden_projects_items_appear_in_neither_counts_nor_details_for_a_non_member(
    tmp_path,
):
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
        hidden = c.post(
            "/issues",
            json={"title": "hidden work", "project_id": project["id"]},
            headers=H1,
        ).json()
        public = c.post("/issues", json={"title": "backlog work"}, headers=H1).json()
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        for issue in (hidden, public):
            _set_in_progress(db_file, issue["id"])
            _sql(
                db_file,
                "INSERT INTO issue_leases (issue_id, holder_id, claimed_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (issue["id"], agent["user"]["id"], _ago(days=20), _ago(days=19)),
            )

        conn = db.connect(db_file)
        outsider_actor = users.get_user(conn, outsider["id"])
        theirs = work_progress.build_progress(conn, actor=outsider_actor)
        mine = work_progress.build_progress(conn, actor=ADMIN)
        conn.close()

        for lane in ("stale_in_progress", "expired_leases"):
            outsider_ids = [r["issue"]["id"] for r in theirs[lane]["items"]]
            assert hidden["id"] not in outsider_ids
            assert theirs[lane]["visible_total"] == 1  # the count excludes it too
            # Positive controls: the outsider still sees the public backlog item, and
            # the admin sees both.
            assert public["id"] in outsider_ids
            assert {hidden["id"], public["id"]} <= {
                r["issue"]["id"] for r in mine[lane]["items"]
            }
            assert mine[lane]["visible_total"] == 2
        # Requests waiting on the operator are the operator's: a non-admin sees none.
        assert theirs["oldest_waiting_on_operator"] is None


# ── unknown is unknown ──────────────────────────────────────────────────────


def test_unknown_work_times_render_as_unknown_never_as_fresh(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        never = c.post("/issues", json={"title": "never worked"}, headers=H1).json()
        old = c.post("/issues", json={"title": "worked long ago"}, headers=H1).json()
        recent = c.post("/issues", json={"title": "worked today"}, headers=H1).json()
        for issue in (never, old, recent):
            _set_in_progress(db_file, issue["id"])
        for issue in (old, recent):
            c.post(f"/issues/{issue['id']}/comments", json={"body": "work"}, headers=H1)
        _backdate_work(db_file, old["id"], _ago(days=work_progress.STALE_WORK_DAYS + 2))

        conn = db.connect(db_file)
        stale = work_progress.build_progress(conn, actor=ADMIN)["stale_in_progress"]
        conn.close()
        by_id = {r["issue"]["id"]: r for r in stale["items"]}
        assert by_id[never["id"]]["last_work_at"] is None
        assert by_id[never["id"]]["work_age_seconds"] is None
        assert by_id[never["id"]]["work_state"] == "unknown"
        # Positive controls: a known old time is shown as stale with its time, and
        # recent work is not listed at all.
        assert by_id[old["id"]]["work_state"] == "stale"
        assert by_id[old["id"]]["last_work_at"] is not None
        assert recent["id"] not in by_id
        assert stale["visible_total"] == 2
        assert stale["stale_after_days"] == work_progress.STALE_WORK_DAYS

        _login(c)
        html = _panel(c)
        never_row = html.split("never worked", 1)[1].split("</li>", 1)[0]
        assert "unknown" in never_row
        old_row = html.split("worked long ago", 1)[1].split("</li>", 1)[0]
        assert "unknown" not in old_row


def test_claimed_work_with_no_recorded_work_is_unknown_not_fresh(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        issue = _claimed_issue(c, agent, title="claimed, nothing done")
        progress = _active_work_item(c, issue["id"])["progress"]
        assert progress["last_work_at"] is None
        assert progress["work_age_seconds"] is None
        assert progress["work_state"] == "unknown"
        assert progress["last_checkin_at"] is not None  # alive, but no work on record
