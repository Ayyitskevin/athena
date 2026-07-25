"""The activity feed + notifications inbox respect visibility (slice 4).

The audit feed records (target_kind, target_id) for issues, pages, and spaces; the
inbox points at those same events. Both must drop events whose target the viewer can't
see — an issue in a private project, a page or space they're outside of — while admins,
the creator, and members keep the full history. The gate is applied in SQL before the
page limit, so cursor paging stays exact. Notifications are CREATED without an access
check (a watch/mention can outlive a project going private), so the inbox gates at READ
time: hidden ones are filtered out, not deleted.
"""

from fastapi.testclient import TestClient
import pytest

import sqlite3
from athena.aegis import issue_activity, issues
from athena.core import access, activity, db, notifications, users
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post(
        "/users",
        json={"email": "admin@e.com", "name": "Admin", "password": "pw"},
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "c@e.com",
            "name": "Creator",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )
    client.post(
        "/users",
        json={
            "email": "o@e.com",
            "name": "Outsider",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )


def _feed_targets(client, headers=None):
    return {
        (e["target_kind"], e["target_id"])
        for e in client.get("/activity", headers=headers or H_ADMIN).json()
    }


def test_feed_gates_issue_page_and_space_events(tmp_path):
    db_file = tmp_path / "feed.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        # Private project + space, public project + space, each with a target whose
        # creation records an activity event.
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        op = client.post(
            "/projects", json={"name": "Open", "key": "OPN"}, headers=H_CREATOR
        ).json()["id"]
        hi = client.post(
            "/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR
        ).json()["id"]
        pi = client.post(
            "/issues", json={"title": "Public", "project_id": op}, headers=H_CREATOR
        ).json()["id"]
        ps = client.post(
            "/spaces", json={"key": "SSP", "name": "SecretSpace"}, headers=H_CREATOR
        ).json()["id"]
        os_ = client.post(
            "/spaces", json={"key": "OSP", "name": "OpenSpace"}, headers=H_CREATOR
        ).json()["id"]
        hp = client.post(
            f"/spaces/{ps}/pages", json={"title": "HiddenPage"}, headers=H_CREATOR
        ).json()["id"]
        pp_ = client.post(
            f"/spaces/{os_}/pages", json={"title": "PublicPage"}, headers=H_CREATOR
        ).json()["id"]

        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
        conn.commit()

        out = _feed_targets(client, H_OUTSIDER)
        # The outsider sees the public issue/page/space events, never the private ones.
        assert ("issue", pi) in out and ("page", pp_) in out and ("space", os_) in out
        assert ("issue", hi) not in out
        assert ("page", hp) not in out
        assert ("space", ps) not in out

        # Admin and creator see everything, including the private targets.
        for h in (H_ADMIN, H_CREATOR):
            seen = _feed_targets(client, h)
            assert {("issue", hi), ("page", hp), ("space", ps)} <= seen

        # Membership opens the private project + space events to the outsider.
        access.add_project_member(conn, pp, 3, added_by=2)
        access.add_space_member(conn, ps, 3, added_by=2)
        opened = _feed_targets(client, H_OUTSIDER)
        assert {("issue", hi), ("page", hp), ("space", ps)} <= opened


def test_web_activity_feed_hides_private_targets_from_anonymous(tmp_path):
    db_file = tmp_path / "webfeed.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        hidden = client.post(
            "/issues", json={"title": "Secret", "project_id": pp}, headers=H_CREATOR
        ).json()["id"]
        public = client.post(
            "/issues", json={"title": "Loose"}, headers=H_CREATOR
        ).json()["id"]  # backlog
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        # The feed links each issue event to /aegis/issues/{id}. Anonymous sees the
        # backlog issue's event but never the private project's.
        text = client.get("/aegis/activity").text
        assert f"/aegis/issues/{public}" in text
        assert f"/aegis/issues/{hidden}" not in text


def test_web_activity_actor_pickers_only_list_visible_event_actors(tmp_path):
    db_file = tmp_path / "web-actor-pickers.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        actor_ids = {}
        for email, name in (
            ("private-only@e.com", "Private Only"),
            ("restricted-only@e.com", "Restricted Only"),
            ("quiet@e.com", "Quiet User"),
            ("visible@e.com", "Visible Actor"),
        ):
            response = client.post(
                "/users",
                json={"email": email, "name": name},
                headers=H_ADMIN,
            )
            assert response.status_code == 201
            actor_ids[name] = response.json()["id"]

        private_headers = {"X-Athena-Actor": str(actor_ids["Private Only"])}
        private_project = client.post(
            "/projects",
            json={"name": "Private Actor Project", "key": "PAP"},
            headers=private_headers,
        ).json()["id"]
        assert (
            client.post(
                "/issues",
                json={
                    "title": "Private actor event",
                    "project_id": private_project,
                },
                headers=private_headers,
            ).status_code
            == 201
        )
        assert (
            client.put(
                f"/projects/{private_project}/visibility",
                json={"visibility": "private"},
                headers=private_headers,
            ).status_code
            == 200
        )

        visible_issue = client.post(
            "/issues",
            json={"title": "Visible actor event"},
            headers={"X-Athena-Actor": str(actor_ids["Visible Actor"])},
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail) "
            "VALUES (?, 'restricted_probe', 'issue', ?, 'restricted')",
            (actor_ids["Restricted Only"], visible_issue),
        )
        conn.commit()

        def actor_picker(path):
            response = client.get(path)
            assert response.status_code == 200
            return response.text.split('<select name="actor"', 1)[1].split(
                "</select>", 1
            )[0]

        for path in ("/aegis/activity", "/aegis/activity/runs"):
            anonymous_picker = actor_picker(path)
            assert "Visible Actor" in anonymous_picker
            assert "Private Only" not in anonymous_picker
            assert "Restricted Only" not in anonymous_picker
            assert "Quiet User" not in anonymous_picker

        login = client.post(
            "/login",
            data={"email": "admin@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        for path in ("/aegis/activity", "/aegis/activity/runs"):
            admin_picker = actor_picker(path)
            assert "Visible Actor" in admin_picker
            assert "Private Only" in admin_picker
            assert "Restricted Only" in admin_picker
            assert "Quiet User" not in admin_picker
        conn.close()


def test_inbox_filters_notifications_on_hidden_targets(tmp_path):
    db_file = tmp_path / "inbox.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        iid = client.post(
            "/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        # The outsider watches the issue (as if mentioned/added before it went private).
        conn.execute(
            "INSERT INTO watches (user_id, target_kind, target_id) VALUES (3, 'issue', ?)",
            (iid,),
        )
        conn.commit()
        # The creator changes the issue → an event fans out to the watcher's inbox.
        assert (
            client.patch(
                f"/issues/{iid}", json={"status": "in_progress"}, headers=H_CREATOR
            ).status_code
            == 200
        )

        def inbox_targets():
            return {
                n["target_id"]
                for n in client.get("/notifications", headers=H_OUTSIDER).json()
            }

        def badge():
            return client.get("/notifications/unread_count", headers=H_OUTSIDER).json()[
                "count"
            ]

        # The notification exists but the outsider can't see its target → filtered out,
        # and the badge doesn't count it either.
        assert iid not in inbox_targets()
        assert badge() == 0

        hidden_notification = conn.execute(
            "SELECT n.id FROM notifications n "
            "JOIN activity a ON a.id = n.event_id "
            "WHERE n.user_id = 3 AND a.target_kind = 'issue' "
            "AND a.target_id = ? ORDER BY n.id DESC LIMIT 1",
            (iid,),
        ).fetchone()["id"]
        assert (
            client.post(
                f"/notifications/{hidden_notification}/read", headers=H_OUTSIDER
            ).status_code
            == 404
        )
        assert client.post("/notifications/read_all", headers=H_OUTSIDER).json() == {
            "count": 0
        }
        assert (
            conn.execute(
                "SELECT read_at FROM notifications WHERE id = ?",
                (hidden_notification,),
            ).fetchone()["read_at"]
            is None
        )

        # Granting membership reveals the still-unread notification and counts it.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert iid in inbox_targets()
        assert badge() >= 1


def test_inbox_gates_page_notifications_by_space(tmp_path):
    # The 'page' arm of the gate, exercised through the notifications join (not just
    # the feed): a page notification in a private space is filtered for an outsider.
    db_file = tmp_path / "inbox_page.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        ps = client.post(
            "/spaces", json={"key": "SSP", "name": "SecretSpace"}, headers=H_CREATOR
        ).json()["id"]
        pid = client.post(
            f"/spaces/{ps}/pages", json={"title": "Runbook"}, headers=H_CREATOR
        ).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
        conn.execute(
            "INSERT INTO watches (user_id, target_kind, target_id) VALUES (3, 'page', ?)",
            (pid,),
        )
        conn.commit()
        # An edit to the page fans out to the watcher's inbox.
        client.patch(f"/pages/{pid}", json={"body": "updated"}, headers=H_CREATOR)

        assert pid not in {
            n["target_id"]
            for n in client.get("/notifications", headers=H_OUTSIDER).json()
        }
        access.add_space_member(conn, ps, 3, added_by=2)
        assert pid in {
            n["target_id"]
            for n in client.get("/notifications", headers=H_OUTSIDER).json()
        }


def test_orphan_target_events_hidden_from_non_admin(tmp_path):
    # The load-bearing "safe default": an event whose target has no surviving row
    # (a hard-deleted issue/page/space) has no container to authorize, so it drops out
    # for a gated viewer — but an admin, ungated, still sees it. This is the arm a
    # broken predicate (an EXISTS rewritten as a row-keeping JOIN) would leak.
    db_file = tmp_path / "orphan.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn = db.connect(db_file)
        for kind in ("issue", "page", "space"):
            activity.record(
                conn,
                actor_id=2,
                verb="changed_status",
                target_kind=kind,
                target_id=99999,
                detail="orphan",
            )
        conn.close()

        out = _feed_targets(client, H_OUTSIDER)
        adm = _feed_targets(client, H_ADMIN)
        for kind in ("issue", "page", "space"):
            assert (kind, 99999) not in out  # hidden from the non-admin viewer
            assert (kind, 99999) in adm  # admin is ungated


def test_events_stream_gates_by_actor(tmp_path):
    # The agent/integration drain (GET /events) is gated by the consumer's token actor,
    # exactly like the human feed — an outsider's token can't drain private events.
    db_file = tmp_path / "events.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        hi = client.post(
            "/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR
        ).json()["id"]
        pi = client.post("/issues", json={"title": "Public"}, headers=H_CREATOR).json()[
            "id"
        ]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        def stream_targets(headers):
            data = client.get("/events?limit=200", headers=headers).json()
            return {(e["target_kind"], e["target_id"]) for e in data["events"]}

        out = stream_targets(H_OUTSIDER)
        assert ("issue", pi) in out and ("issue", hi) not in out
        assert ("issue", hi) in stream_targets(H_ADMIN)
        access.add_project_member(conn, pp, 3, added_by=2)
        assert ("issue", hi) in stream_targets(H_OUTSIDER)


def test_runs_omit_hidden_target_events(tmp_path):
    # reconstruct_runs builds on list_activity, so the runs lens is gated too: a run
    # the outsider views never includes that actor's work on a target they can't see.
    db_file = tmp_path / "runs.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        hi = client.post(
            "/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR
        ).json()["id"]
        pi = client.post("/issues", json={"title": "Public"}, headers=H_CREATOR).json()[
            "id"
        ]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        def run_event_targets(headers):
            runs = client.get("/activity/runs?actor_id=2", headers=headers).json()
            return {
                (e["target_kind"], e["target_id"])
                for run in runs
                for e in run["events"]
            }

        out = run_event_targets(H_OUTSIDER)
        assert ("issue", pi) in out and ("issue", hi) not in out
        assert ("issue", hi) in run_event_targets(H_ADMIN)


def test_feed_paging_excludes_hidden_rows_exactly(tmp_path):
    # The predicate is applied in SQL before LIMIT, so a page returns N *visible* rows
    # and the cursor stays gap-free even when hidden rows are interleaved. Seed 5
    # visible (backlog) + 5 hidden (private) issues alternating, then walk the feed in
    # pages of 3 as the outsider: every visible issue appears, no hidden one ever does.
    db_file = tmp_path / "paging.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR
        ).json()["id"]
        visible, hidden = [], []
        for n in range(5):
            hidden.append(
                client.post(
                    "/issues",
                    json={"title": f"h{n}", "project_id": pp},
                    headers=H_CREATOR,
                ).json()["id"]
            )
            visible.append(
                client.post(
                    "/issues", json={"title": f"v{n}"}, headers=H_CREATOR
                ).json()["id"]
            )
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        seen, before, pages = set(), None, 0
        while pages < 20:
            pages += 1
            url = "/activity?kind=issue&limit=3" + (
                f"&before_id={before}" if before else ""
            )
            rows = client.get(url, headers=H_OUTSIDER).json()
            if not rows:
                break
            for e in rows:
                seen.add(e["target_id"])
            before = rows[-1]["id"]
            if len(rows) < 3:
                break

        # Every visible issue's event was paged through; no hidden one ever leaked in.
        assert set(visible) <= seen
        assert seen.isdisjoint(hidden)


def test_issue_events_keep_every_event_time_project_scope(tmp_path):
    # A current public issue does not publish facts recorded while it lived in private
    # projects. Transition events require BOTH adjacent projects, so after A → B → C
    # access to A and C alone still cannot reveal B.
    db_file = tmp_path / "event-scopes.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        project_a = client.post(
            "/projects",
            json={"name": "Private A", "key": "PRA"},
            headers=H_CREATOR,
        ).json()["id"]
        project_b = client.post(
            "/projects",
            json={"name": "Private B", "key": "PRB"},
            headers=H_CREATOR,
        ).json()["id"]
        project_c = client.post(
            "/projects",
            json={"name": "Public C", "key": "PUB"},
            headers=H_CREATOR,
        ).json()["id"]
        issue_id = client.post(
            "/issues",
            json={"title": "Scoped history", "project_id": project_a},
            headers=H_CREATOR,
        ).json()["id"]

        conn = db.connect(db_file)
        conn.execute(
            "UPDATE projects SET visibility = 'private' WHERE id IN (?, ?)",
            (project_a, project_b),
        )
        conn.commit()

        assert (
            client.patch(
                f"/issues/{issue_id}",
                json={"priority": "high"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/issues/{issue_id}/project",
                json={"project_id": project_b},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"/issues/{issue_id}",
                json={"priority": "urgent"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/issues/{issue_id}/project",
                json={"project_id": project_c},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"/issues/{issue_id}",
                json={"priority": "low"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )

        rows = conn.execute(
            "SELECT id, verb, detail, visibility_restricted FROM activity "
            "WHERE target_kind = 'issue' AND target_id = ? ORDER BY id",
            (issue_id,),
        ).fetchall()
        scope_by_fact = {
            (row["verb"], row["detail"]): {
                scope["project_id"]
                for scope in conn.execute(
                    "SELECT p.id AS project_id FROM activity_visibility_projects avp "
                    "JOIN projects p ON p.activity_scope_key = avp.project_scope_key "
                    "WHERE avp.event_id = ?",
                    (row["id"],),
                )
            }
            for row in rows
        }
        assert scope_by_fact == {
            ("created", ""): {project_a},
            ("changed_priority", "medium → high"): {project_a},
            ("changed_project", "Private B"): {project_a, project_b},
            ("changed_priority", "high → urgent"): {project_b},
            ("changed_project", "Public C"): {project_b, project_c},
            ("changed_priority", "urgent → low"): {project_c},
        }
        assert {row["visibility_restricted"] for row in rows} == {0}

        def outsider_facts():
            response = client.get(
                f"/activity?target_kind=issue&target_id={issue_id}&limit=200",
                headers=H_OUTSIDER,
            )
            assert response.status_code == 200
            assert all(
                "visibility_restricted" not in event for event in response.json()
            )
            return {(event["verb"], event["detail"]) for event in response.json()}

        assert outsider_facts() == {("changed_priority", "urgent → low")}
        assert client.get(f"/issues/{issue_id}", headers=H_OUTSIDER).status_code == 200
        denied = client.get(f"/issues/{issue_id}/state", headers=H_OUTSIDER)
        assert denied.status_code == 403
        assert denied.json() == {
            "detail": "complete issue history is not available to this actor"
        }
        assert (
            client.get(f"/issues/{issue_id}/state", headers=H_CREATOR).status_code
            == 200
        )

        login = client.post(
            "/login",
            data={"email": "o@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        detail = client.get(f"/aegis/issues/{issue_id}")
        assert detail.status_code == 200
        assert "urgent → low" in detail.text
        assert "medium → high" not in detail.text
        assert "Private B" not in detail.text
        history = client.get(f"/aegis/issues/{issue_id}/history")
        assert history.status_code == 403
        assert "medium → high" not in history.text

        access.add_project_member(conn, project_a, 3, added_by=2)
        assert {
            ("created", ""),
            ("changed_priority", "medium → high"),
            ("changed_priority", "urgent → low"),
        } <= outsider_facts()
        assert (
            "changed_project",
            "Private B",
        ) not in outsider_facts()
        assert (
            client.get(f"/issues/{issue_id}/state", headers=H_OUTSIDER).status_code
            == 403
        )

        access.add_project_member(conn, project_b, 3, added_by=2)
        assert outsider_facts() == set(scope_by_fact)
        reopened = client.get(f"/aegis/issues/{issue_id}/history")
        assert reopened.status_code == 200
        assert "medium → high" in reopened.text
        assert "high → urgent" in reopened.text
        assert (
            client.get(f"/issues/{issue_id}/state", headers=H_OUTSIDER).status_code
            == 200
        )
        conn.close()


def test_raw_issue_activity_fails_closed_while_native_events_are_scoped(tmp_path):
    db_file = tmp_path / "restricted-default.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        project_id = client.post(
            "/projects",
            json={"name": "Visible", "key": "VIS"},
            headers=H_CREATOR,
        ).json()["id"]
        issue_id = client.post(
            "/issues",
            json={"title": "Visibility default", "project_id": project_id},
            headers=H_CREATOR,
        ).json()["id"]
        conn = db.connect(db_file)
        legacy = conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail) "
            "VALUES (2, 'legacy_probe', 'issue', ?, 'restricted')",
            (issue_id,),
        ).lastrowid
        conn.commit()
        native = activity.record(
            conn,
            actor_id=2,
            verb="native_probe",
            target_kind="issue",
            target_id=issue_id,
            detail="scoped",
        )["id"]

        flags = {
            row["id"]: row["visibility_restricted"]
            for row in conn.execute(
                "SELECT id, visibility_restricted FROM activity WHERE id IN (?, ?)",
                (legacy, native),
            )
        }
        assert flags == {legacy: 1, native: 0}
        assert (
            conn.execute(
                "SELECT p.id AS project_id FROM activity_visibility_projects avp "
                "JOIN projects p ON p.activity_scope_key = avp.project_scope_key "
                "WHERE avp.event_id = ?",
                (native,),
            ).fetchone()["project_id"]
            == project_id
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM activity_visibility_projects WHERE event_id = ?",
                (legacy,),
            ).fetchone()[0]
            == 0
        )

        outsider_ids = {
            row["id"]
            for row in client.get(
                f"/activity?target_kind=issue&target_id={issue_id}",
                headers=H_OUTSIDER,
            ).json()
        }
        admin_ids = {
            row["id"]
            for row in client.get(
                f"/activity?target_kind=issue&target_id={issue_id}",
                headers=H_ADMIN,
            ).json()
        }
        assert native in outsider_ids
        assert legacy not in outsider_ids
        assert {legacy, native} <= admin_ids
        activity_page = client.get("/aegis/activity").text
        assert "legacy_probe" not in activity_page
        assert "native_probe" in activity_page
        assert {legacy, native} <= {
            row["id"]
            for row in activity.list_activity(
                conn, target_kind="issue", target_id=issue_id
            )
        }
        conn.close()


def test_activity_scope_and_event_roll_back_together(tmp_path, monkeypatch):
    db_file = tmp_path / "scope-rollback.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        issue_id = client.post(
            "/issues",
            json={"title": "Rollback scope"},
            headers=H_CREATOR,
        ).json()["id"]

    conn = db.connect(db_file)
    baseline_events = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    baseline_scopes = conn.execute(
        "SELECT COUNT(*) FROM activity_visibility_projects"
    ).fetchone()[0]

    def fail_notifications(*args, **kwargs):
        raise RuntimeError("injected notification failure")

    monkeypatch.setattr(notifications, "notify_watchers", fail_notifications)
    with pytest.raises(RuntimeError, match="injected notification failure"):
        activity.record(
            conn,
            actor_id=2,
            verb="scope_probe",
            target_kind="issue",
            target_id=issue_id,
        )

    assert (
        conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == baseline_events
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM activity_visibility_projects").fetchone()[0]
        == baseline_scopes
    )
    assert not conn.in_transaction
    conn.close()


def test_migration_keeps_preexisting_issue_events_restricted(tmp_path):
    # Migration 0042 cannot reconstruct the project at which an old event happened.
    # Existing rows therefore retain the column's fail-closed default; only events
    # recorded after scope capture exists are released to non-admin readers.
    conn = db.connect(tmp_path / "legacy-migration.db")
    migration = None
    for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if path.name == "0042_activity_visibility_scope.sql":
            migration = path
            break
        conn.executescript(path.read_text())
    assert migration is not None

    user = users.create_user(conn, email="owner@e.com", name="Owner")
    project_id = conn.execute(
        "INSERT INTO projects (name, key, description, created_by) "
        "VALUES ('Legacy', 'LEG', '', ?)",
        (user["id"],),
    ).lastrowid
    conn.commit()
    issue = issues.create_issue(
        conn,
        title="Legacy event",
        body="",
        created_by=user["id"],
        project_id=project_id,
    )
    legacy_id = conn.execute(
        "INSERT INTO activity "
        "(actor_id, verb, target_kind, target_id, detail) "
        "VALUES (?, 'legacy_event', 'issue', ?, 'before scope capture')",
        (user["id"], issue["id"]),
    ).lastrowid
    conn.commit()

    conn.executescript(migration.read_text())
    # Apply the rest of the ledger before recording natively. The legacy row above
    # is already frozen on the pre-0042 side, which is what this test is about; the
    # recorder itself always writes today's full column set (0064 added another),
    # so it must run against the schema the app guarantees at startup.
    for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if path.name > migration.name:
            conn.executescript(path.read_text())
    native_id = activity.record(
        conn,
        actor_id=user["id"],
        verb="native_event",
        target_kind="issue",
        target_id=issue["id"],
    )["id"]

    assert {
        row["id"]: row["visibility_restricted"]
        for row in conn.execute(
            "SELECT id, visibility_restricted FROM activity WHERE id IN (?, ?)",
            (legacy_id, native_id),
        )
    } == {legacy_id: 1, native_id: 0}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM activity_visibility_projects WHERE event_id = ?",
            (legacy_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT p.id AS project_id FROM activity_visibility_projects avp "
            "JOIN projects p ON p.activity_scope_key = avp.project_scope_key "
            "WHERE avp.event_id = ?",
            (native_id,),
        ).fetchone()["project_id"]
        == project_id
    )
    conn.close()


def test_deleted_project_id_is_reserved_and_cannot_reauthorize_old_events(tmp_path):
    db_file = tmp_path / "project-scope-generation.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        old_project = client.post(
            "/projects",
            json={"name": "Old Secret", "key": "OLD"},
            headers=H_CREATOR,
        ).json()
        issue_id = client.post(
            "/issues",
            json={"title": "Outlives project", "project_id": old_project["id"]},
            headers=H_CREATOR,
        ).json()["id"]
        assert (
            client.put(
                f"/projects/{old_project['id']}/visibility",
                json={"visibility": "private"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )

        conn = db.connect(db_file)
        old_scope_key = conn.execute(
            "SELECT activity_scope_key FROM projects WHERE id = ?",
            (old_project["id"],),
        ).fetchone()["activity_scope_key"]

        assert (
            client.put(
                f"/issues/{issue_id}/project",
                json={"project_id": None},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"/projects/{old_project['id']}", headers=H_CREATOR
            ).status_code
            == 204
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO projects "
                "(id, name, key, description, created_by, activity_scope_key) "
                "VALUES (?, 'Forced Reuse', 'BAD', '', 2, ?)",
                (old_project["id"], "0" * 32),
            )
        conn.rollback()
        replacement = client.post(
            "/projects",
            json={"name": "Replacement Public", "key": "NEW"},
            headers=H_CREATOR,
        ).json()
        assert replacement["id"] > old_project["id"]
        replacement_scope_key = conn.execute(
            "SELECT activity_scope_key FROM projects WHERE id = ?",
            (replacement["id"],),
        ).fetchone()["activity_scope_key"]
        assert replacement_scope_key != old_scope_key

        assert (
            client.patch(
                f"/issues/{issue_id}",
                json={"priority": "high"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        outsider = client.get(
            f"/activity?target_kind=issue&target_id={issue_id}",
            headers=H_OUTSIDER,
        ).json()
        assert [(row["verb"], row["detail"]) for row in outsider] == [
            ("changed_priority", "medium → high")
        ]
        admin_facts = {
            (row["verb"], row["detail"])
            for row in client.get(
                f"/activity?target_kind=issue&target_id={issue_id}",
                headers=H_ADMIN,
            ).json()
        }
        assert {
            ("created", ""),
            ("removed_from_project", "Old Secret"),
            ("changed_priority", "medium → high"),
        } <= admin_facts

        # The old target stays admin-forensic only after its live project is deleted;
        # the public replacement receives a fresh id and cannot inherit that event.
        project_activity_url = (
            f"/activity?target_kind=project&target_id={old_project['id']}"
        )
        outsider_project_facts = {
            (row["verb"], row["detail"])
            for row in client.get(project_activity_url, headers=H_OUTSIDER).json()
        }
        assert (
            "project_made_private",
            "Old Secret",
        ) not in outsider_project_facts
        admin_project_facts = {
            (row["verb"], row["detail"])
            for row in client.get(project_activity_url, headers=H_ADMIN).json()
        }
        assert (
            "project_made_private",
            "Old Secret",
        ) in admin_project_facts
        conn.close()


def test_deleted_private_space_and_page_ids_are_never_reused(tmp_path):
    db_file = tmp_path / "mentor-target-reservations.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        old_space = client.post(
            "/spaces",
            json={"key": "OLD", "name": "Old Secret"},
            headers=H_CREATOR,
        ).json()
        old_page = client.post(
            f"/spaces/{old_space['id']}/pages",
            json={"title": "Old Hidden Page"},
            headers=H_CREATOR,
        ).json()
        assert (
            client.put(
                f"/spaces/{old_space['id']}/visibility",
                json={"visibility": "private"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )
        assert (
            client.delete(f"/pages/{old_page['id']}", headers=H_CREATOR).status_code
            == 204
        )
        assert (
            client.delete(f"/spaces/{old_space['id']}", headers=H_CREATOR).status_code
            == 204
        )

        new_space = client.post(
            "/spaces",
            json={"key": "NEW", "name": "New Public"},
            headers=H_CREATOR,
        ).json()
        new_page = client.post(
            f"/spaces/{new_space['id']}/pages",
            json={"title": "New Public Page"},
            headers=H_CREATOR,
        ).json()
        assert new_space["id"] > old_space["id"]
        assert new_page["id"] > old_page["id"]

        conn = db.connect(db_file)
        assert {
            (row["target_kind"], row["target_id"])
            for row in conn.execute(
                "SELECT target_kind, target_id FROM activity_target_id_reservations "
                "WHERE target_kind IN ('space', 'page')"
            )
        } >= {("space", old_space["id"]), ("page", old_page["id"])}
        conn.close()

        assert (
            client.get(
                f"/activity?target_kind=space&target_id={old_space['id']}",
                headers=H_OUTSIDER,
            ).json()
            == []
        )
        assert (
            client.get(
                f"/activity?target_kind=page&target_id={old_page['id']}",
                headers=H_OUTSIDER,
            ).json()
            == []
        )


def test_cross_project_parent_and_sprint_details_use_related_scopes(tmp_path):
    db_file = tmp_path / "related-event-scopes.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        private_project = client.post(
            "/projects",
            json={"name": "Private Relations", "key": "SEC"},
            headers=H_CREATOR,
        ).json()["id"]
        public_project = client.post(
            "/projects",
            json={"name": "Public Child", "key": "PUB"},
            headers=H_CREATOR,
        ).json()["id"]
        parent = client.post(
            "/issues",
            json={"title": "Private parent", "project_id": private_project},
            headers=H_CREATOR,
        ).json()
        child = client.post(
            "/issues",
            json={"title": "Public child", "project_id": public_project},
            headers=H_CREATOR,
        ).json()
        sprint = client.post(
            f"/projects/{private_project}/sprints",
            json={"name": "Private Sprint"},
            headers=H_CREATOR,
        ).json()
        assert (
            client.put(
                f"/projects/{private_project}/visibility",
                json={"visibility": "private"},
                headers=H_CREATOR,
            ).status_code
            == 200
        )

        conn = db.connect(db_file)
        issue_activity.record_parent_change(
            conn,
            actor_id=2,
            issue_id=child["id"],
            before=None,
            after=parent["id"],
        )
        issue_activity.record_sprint_change(
            conn,
            actor_id=2,
            issue_id=child["id"],
            before=sprint["id"],
            after=None,
        )
        related_rows = conn.execute(
            "SELECT id, verb FROM activity "
            "WHERE target_kind = 'issue' AND target_id = ? "
            "AND verb IN ('set_parent', 'removed_from_sprint')",
            (child["id"],),
        ).fetchall()
        assert {row["verb"] for row in related_rows} == {
            "set_parent",
            "removed_from_sprint",
        }
        for row in related_rows:
            assert {
                scope["project_id"]
                for scope in conn.execute(
                    "SELECT p.id AS project_id "
                    "FROM activity_visibility_projects avp "
                    "JOIN projects p "
                    "ON p.activity_scope_key = avp.project_scope_key "
                    "WHERE avp.event_id = ?",
                    (row["id"],),
                )
            } == {private_project, public_project}

        outsider = client.get(
            f"/activity?target_kind=issue&target_id={child['id']}",
            headers=H_OUTSIDER,
        ).json()
        assert [(row["verb"], row["detail"]) for row in outsider] == [("created", "")]
        access.add_project_member(conn, private_project, 3, added_by=2)
        opened = {
            (row["verb"], row["detail"])
            for row in client.get(
                f"/activity?target_kind=issue&target_id={child['id']}",
                headers=H_OUTSIDER,
            ).json()
        }
        assert ("set_parent", parent["key"]) in opened
        assert ("removed_from_sprint", "Private Sprint") in opened
        conn.close()
