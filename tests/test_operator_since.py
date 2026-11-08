"""The operator's "since you last looked" card.

It reads the operator's own desk cursor (``agent_cursors``, name ``desk``): the
table accepts any user, so an admin or human operator needs no migration. The
card counts what changed after that cursor, by kind, with links:

- **new asks:** claims yielded with a blocking question;
- **claims completed:** each with the counts its report carries, when it has one
  (counts, never an overall verdict);
- **new blockers:** ``blocks`` / ``blocked_by`` links;
- **approvals raised;**
- **stalled:** in-progress issues whose last recorded work crossed the stale
  threshold since the cursor was last moved.

Reading never moves the cursor. Only "mark seen" does, and it moves to exactly
the last event the card showed, so anything that arrived after the render is
still new next time. Visibility applies to every count and every row.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from athena.aegis import operator_since, work_progress
from athena.core import db, desk_cursors, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
ADMIN = {"id": 1, "role": "admin"}
_TS = "%Y-%m-%d %H:%M:%S"


def _app(tmp_path, name="since.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _login(client, email="a@e.com"):
    client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )
    return client.cookies.get("athena_csrf")


def _agent(client, email="sol@e.com", name="Sol"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(agent, run="run-1"):
    return {"Authorization": f"Bearer {agent['token']['token']}", "X-Athena-Run": run}


def _ago(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).strftime(_TS)


def _sql(db_file, statement, params=()):
    conn = db.connect(db_file)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _since(db_file, actor=ADMIN):
    conn = db.connect(db_file)
    try:
        return operator_since.build_since(conn, actor=actor)
    finally:
        conn.close()


def _cursor(db_file, user_id=1):
    conn = db.connect(db_file)
    try:
        return desk_cursors.get_cursor(
            conn, user_id=user_id, name=desk_cursors.DESK_CURSOR
        )
    finally:
        conn.close()


def _counts(projection):
    return {kind["key"]: kind["count"] for kind in projection["kinds"]}


def _claimed(client, agent, title, run="run-1", **fields):
    issue = client.post("/issues", json={"title": title, **fields}, headers=H1).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    tag = client.get(f"/issues/{issue['id']}", headers=H1).headers["etag"]
    claimed = client.post(
        f"/issues/{issue['id']}/claim", headers={**_bearer(agent, run), "If-Match": tag}
    )
    assert claimed.status_code in (200, 201), claimed.text
    return issue, claimed.json()


def _mark_seen(client, csrf, after_id):
    return client.post(
        "/aegis/dashboard/since/seen",
        data={"after_id": str(after_id)},
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )


# ── the cursor: no migration, and only "mark seen" moves it ─────────────────


def test_the_desk_cursor_takes_a_human_operator_without_a_migration(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post("/issues", json={"title": "one event"}, headers=H1)
    conn = db.connect(db_file)
    admin = users.get_user(conn, 1)
    assert not admin.get("is_agent")
    desk_cursors.set_cursor(conn, user_id=1, name=desk_cursors.DESK_CURSOR, after_id=1)
    conn.commit()  # set_cursor leaves the transaction to its caller
    conn.close()
    assert _cursor(db_file)["after_id"] == 1


def test_nothing_advances_on_a_read(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post("/issues", json={"title": "new work"}, headers=H1)
        csrf = _login(c)
        assert _cursor(db_file) is None
        for _ in range(3):
            assert c.get("/aegis/dashboard").status_code == 200
            assert c.get("/desk/since", headers=H1).status_code == 200
        _since(db_file)
        assert _cursor(db_file) is None
        # Positive control: the explicit action does move it.
        shown = _since(db_file)["last_shown_event_id"]
        assert _mark_seen(c, csrf, shown).status_code == 303
        assert _cursor(db_file)["after_id"] == shown


def test_mark_seen_advances_exactly_to_the_last_shown_event(tmp_path, monkeypatch):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        for n in range(3):
            c.post("/issues", json={"title": f"before {n}"}, headers=H1)
        csrf = _login(c)
        page = c.get("/aegis/dashboard").text
        shown = _since(db_file)["last_shown_event_id"]
        assert f'name="after_id" value="{shown}"' in page
        # Something arrives after the card was rendered.
        c.post("/issues", json={"title": "after the render"}, headers=H1)
        assert _mark_seen(c, csrf, shown).status_code == 303
        assert _cursor(db_file)["after_id"] == shown
        # The late arrival is still new next time; nothing earlier is.
        later = _since(db_file)
        assert later["examined_events"] >= 1
        assert later["last_shown_event_id"] > shown
        # When there is more than the card examines, "seen" covers only what was
        # shown (the oldest first), never the unshown rest.
        monkeypatch.setattr(operator_since, "SCAN_LIMIT", 2)
        for n in range(4):
            c.post("/issues", json={"title": f"burst {n}"}, headers=H1)
        clipped = _since(db_file)
        assert clipped["capped"] is True and clipped["examined_events"] == 2
        assert _mark_seen(c, csrf, clipped["last_shown_event_id"]).status_code == 303
        assert _since(db_file)["examined_events"] >= 1  # the rest is still new


def test_mark_seen_refuses_to_move_backward_and_needs_csrf(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post("/issues", json={"title": "x"}, headers=H1)
        csrf = _login(c)
        shown = _since(db_file)["last_shown_event_id"]
        assert _mark_seen(c, csrf, shown).status_code == 303
        # A stale form (a lower id) does not rewind the cursor.
        stale = _mark_seen(c, csrf, max(1, shown - 1))
        assert _cursor(db_file)["after_id"] == shown
        assert stale.status_code in (303, 409)
        # Without the CSRF token the form is refused and nothing moves.
        c.post("/issues", json={"title": "y"}, headers=H1)
        newer = _since(db_file)["last_shown_event_id"]
        refused = c.post(
            "/aegis/dashboard/since/seen",
            data={"after_id": str(newer)},
            follow_redirects=False,
        )
        assert refused.status_code == 403
        assert _cursor(db_file)["after_id"] == shown


# ── what it counts ──────────────────────────────────────────────────────────


def test_counts_by_kind_with_links_and_completion_report_counts(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        c.post("/issues", json={"title": "warm-up"}, headers=H1)
        csrf = _login(c)
        assert (
            _mark_seen(c, csrf, _since(db_file)["last_shown_event_id"]).status_code
            == 303
        )
        # Since the look: a question, two completions (one with a report), a
        # blocker, an approval request, and an unrelated comment.
        asked, lease = _claimed(c, agent, "asked")
        c.post(
            f"/issues/{asked['id']}/yield",
            json={
                "generation": lease["generation"],
                "reason": "needs_input",
                "attempted_work": "Read it.",
                "evidence": ["notes"],
                "blocking_question": "Which way?",
                "resume_instructions": "Pick.",
            },
            headers=_bearer(agent),
        )
        reported, rlease = _claimed(c, agent, "reported")
        c.post(
            f"/issues/{reported['id']}/complete",
            json={
                "generation": rlease["generation"],
                "results": [
                    {"condition": "tests pass", "state": "met", "note": ""},
                    {"condition": "docs", "state": "not_assessed", "note": "later"},
                ],
            },
            headers=_bearer(agent),
        )
        plain, plain_lease = _claimed(c, agent, "plain")
        c.post(
            f"/issues/{plain['id']}/complete",
            json={"generation": plain_lease["generation"]},
            headers=_bearer(agent),
        )
        blocked = c.post("/issues", json={"title": "blocked"}, headers=H1).json()
        c.post(
            f"/issues/{blocked['id']}/links",
            json={"target_ref": str(asked["id"]), "relation": "blocked_by"},
            headers=H1,
        )
        relates = c.post("/issues", json={"title": "related"}, headers=H1).json()
        c.post(
            f"/issues/{relates['id']}/links",
            json={"target_ref": str(asked["id"]), "relation": "relates"},
            headers=H1,
        )
        _sql(
            db_file,
            "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail) "
            "VALUES (?, 'approval_requested', 'issue', ?, 'issue.close (approval #1)')",
            (agent["user"]["id"], plain["id"]),
        )
        c.post(f"/issues/{plain['id']}/comments", json={"body": "note"}, headers=H1)

        since = _since(db_file)
        counts = _counts(since)
        assert counts["asks"] == 1
        assert counts["completions"] == 2
        assert counts["blockers"] == 1  # the "relates" link is not a blocker
        assert counts["approvals"] == 1
        hrefs = {kind["key"]: kind["href"] for kind in since["kinds"]}
        assert hrefs["asks"] == "/admin/agents#questions"
        assert hrefs["approvals"] == "/admin/agents"
        by_issue = {row["issue"]["id"]: row for row in since["completions"]}
        assert by_issue[reported["id"]]["reported_counts"] == {
            "met": 1,
            "not_assessed": 1,
        }
        # Positive control: a completion without a report carries no counts,
        # rather than an invented verdict.
        assert by_issue[plain["id"]]["reported_counts"] is None
        assert by_issue[reported["id"]]["holder"]["name"] == "Sol"
        html = " ".join(c.get("/aegis/dashboard").text.split())
        card = html.split('id="since-heading"', 1)[1].split("</section>", 1)[0]
        assert "1 reported met" in card and "1 reported not_assessed" in card
        assert f'href="/aegis/issues/{reported["id"]}"' in card


def test_stalled_counts_only_issues_that_crossed_the_threshold_since_the_look(tmp_path):
    app, db_file = _app(tmp_path)
    days = work_progress.STALE_WORK_DAYS
    with TestClient(app) as c:
        _bootstrap(c)
        crossed = c.post("/issues", json={"title": "crossed"}, headers=H1).json()
        long_ago = c.post(
            "/issues", json={"title": "stale for ages"}, headers=H1
        ).json()
        fresh = c.post("/issues", json={"title": "recent work"}, headers=H1).json()
        for issue in (crossed, long_ago, fresh):
            _sql(
                db_file,
                "UPDATE issues SET status = 'in_progress' WHERE id = ?",
                (issue["id"],),
            )
            c.post(f"/issues/{issue['id']}/comments", json={"body": "w"}, headers=H1)
        csrf = _login(c)
        assert (
            _mark_seen(c, csrf, _since(db_file)["last_shown_event_id"]).status_code
            == 303
        )
    # Looked two days ago. Last work: crossed 8 days ago (crossed 1 day ago), stale
    # for ages 30 days ago (crossed long before the look), recent 1 day ago.
    _sql(db_file, "UPDATE agent_cursors SET updated_at = ?", (_ago(days=2),))
    for issue, age in ((crossed, days + 1), (long_ago, 30), (fresh, 1)):
        stamp = _ago(days=age)
        _sql(
            db_file,
            "UPDATE issues SET created_at = ? WHERE id = ?",
            (stamp, issue["id"]),
        )
        _sql(
            db_file,
            "UPDATE activity SET created_at = ? WHERE target_kind = 'issue' AND target_id = ?",
            (stamp, issue["id"]),
        )
    since = _since(db_file)
    assert _counts(since)["stalled"] == 1
    assert [row["issue"]["id"] for row in since["stalled"]] == [crossed["id"]]


# ── visibility ──────────────────────────────────────────────────────────────


def test_hidden_projects_stay_hidden(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = c.post(
            "/users",
            json={
                "email": "m@e.com",
                "name": "Mem",
                "password": "pw",
                "role": "member",
            },
            headers=H1,
        ).json()
        project = c.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=H1
        ).json()
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        conn = db.connect(db_file)
        start = conn.execute("SELECT MAX(id) AS m FROM activity").fetchone()["m"]
        desk_cursors.set_cursor(
            conn, user_id=member["id"], name=desk_cursors.DESK_CURSOR, after_id=start
        )
        desk_cursors.set_cursor(
            conn, user_id=1, name=desk_cursors.DESK_CURSOR, after_id=start
        )
        conn.commit()  # set_cursor leaves the transaction to its caller
        conn.close()
        hidden = c.post(
            "/issues",
            json={"title": "secret-zz", "project_id": project["id"]},
            headers=H1,
        ).json()
        other_hidden = c.post(
            "/issues",
            json={"title": "secret-yy", "project_id": project["id"]},
            headers=H1,
        ).json()
        public = c.post("/issues", json={"title": "public a"}, headers=H1).json()
        public_b = c.post("/issues", json={"title": "public b"}, headers=H1).json()
        # A blocker inside the private project, and one in the open backlog.
        c.post(
            f"/issues/{hidden['id']}/links",
            json={"target_ref": str(other_hidden["id"]), "relation": "blocked_by"},
            headers=H1,
        )
        c.post(
            f"/issues/{public_b['id']}/links",
            json={"target_ref": str(public["id"]), "relation": "blocked_by"},
            headers=H1,
        )
        conn = db.connect(db_file)
        mem = users.get_user(conn, member["id"])
        theirs = operator_since.build_since(conn, actor=mem)
        mine = operator_since.build_since(conn, actor=ADMIN)
        hidden_ids = {
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM activity WHERE target_kind = 'issue' AND target_id = ?",
                (hidden["id"],),
            )
        }
        conn.close()
        assert "secret-" not in str(theirs)
        assert theirs["last_shown_event_id"] not in hidden_ids
        assert _counts(theirs)["blockers"] == 1
        # Positive control: the admin's view includes the private project's events.
        assert _counts(mine)["blockers"] == 2
        assert mine["examined_events"] > theirs["examined_events"]

        # Stalled rows apply project visibility too. Both issues stalled 8 days
        # ago (crossing 1 day ago); both viewers looked 2 days ago.
        for issue in (hidden, public):
            _sql(
                db_file,
                "UPDATE issues SET status = 'in_progress', created_at = ? WHERE id = ?",
                (_ago(days=work_progress.STALE_WORK_DAYS + 1), issue["id"]),
            )
            _sql(
                db_file,
                "UPDATE activity SET created_at = ? WHERE target_kind = 'issue' "
                "AND target_id = ?",
                (_ago(days=work_progress.STALE_WORK_DAYS + 1), issue["id"]),
            )
        _sql(db_file, "UPDATE agent_cursors SET updated_at = ?", (_ago(days=2),))
        conn = db.connect(db_file)
        theirs = operator_since.build_since(
            conn, actor=users.get_user(conn, member["id"])
        )
        mine = operator_since.build_since(conn, actor=ADMIN)
        conn.close()
        assert [row["issue"]["id"] for row in theirs["stalled"]] == [public["id"]]
        assert _counts(theirs)["stalled"] == 1
        # Positive control: the admin sees both stalled issues.
        assert {row["issue"]["id"] for row in mine["stalled"]} == {
            hidden["id"],
            public["id"],
        }


# ── the empty state ─────────────────────────────────────────────────────────


def test_an_empty_state_says_nothing_new_since_the_time(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post("/issues", json={"title": "x"}, headers=H1)
        csrf = _login(c)
        # Positive control: with something new, the card does not say "nothing new".
        before = " ".join(c.get("/aegis/dashboard").text.split())
        assert "Nothing new since" not in before
        assert (
            _mark_seen(c, csrf, _since(db_file)["last_shown_event_id"]).status_code
            == 303
        )
        at = _cursor(db_file)["updated_at"]
        html = " ".join(c.get("/aegis/dashboard").text.split())
        assert f"Nothing new since {at} UTC" in html
        assert _since(db_file)["empty"] is True
        # Positive control: an event of a kind the card does not name (a comment)
        # is still something new, not "nothing new".
        quiet = _since(db_file)
        c.post("/issues/1/comments", json={"body": "a note"}, headers=H1)
        later = " ".join(c.get("/aegis/dashboard").text.split())
        assert "Nothing new since" not in later
        assert _counts(_since(db_file))["other"] == 1
        assert quiet["empty"] is True


def test_reading_writes_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post("/issues", json={"title": "x"}, headers=H1)
    conn = db.connect(db_file)
    before = conn.total_changes
    operator_since.build_since(conn, actor=ADMIN)
    assert conn.total_changes == before
    conn.close()
