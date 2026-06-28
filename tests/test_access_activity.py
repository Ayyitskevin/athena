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

from athena.core import access, activity, db
from athena.main import create_app

H_ADMIN = {"X-Athena-Actor": "1"}
H_CREATOR = {"X-Athena-Actor": "2"}
H_OUTSIDER = {"X-Athena-Actor": "3"}


def _bootstrap(client):
    client.post("/users", json={"email": "admin@e.com", "name": "Admin", "password": "pw"}, headers=H_ADMIN)
    client.post("/users", json={"email": "c@e.com", "name": "Creator", "password": "pw", "role": "member"}, headers=H_ADMIN)
    client.post("/users", json={"email": "o@e.com", "name": "Outsider", "password": "pw", "role": "member"}, headers=H_ADMIN)


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
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        op = client.post("/projects", json={"name": "Open", "key": "OPN"}, headers=H_CREATOR).json()["id"]
        hi = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        pi = client.post("/issues", json={"title": "Public", "project_id": op}, headers=H_CREATOR).json()["id"]
        ps = client.post("/spaces", json={"key": "SSP", "name": "SecretSpace"}, headers=H_CREATOR).json()["id"]
        os_ = client.post("/spaces", json={"key": "OSP", "name": "OpenSpace"}, headers=H_CREATOR).json()["id"]
        hp = client.post(f"/spaces/{ps}/pages", json={"title": "HiddenPage"}, headers=H_CREATOR).json()["id"]
        pp_ = client.post(f"/spaces/{os_}/pages", json={"title": "PublicPage"}, headers=H_CREATOR).json()["id"]

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
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        hidden = client.post("/issues", json={"title": "Secret", "project_id": pp}, headers=H_CREATOR).json()["id"]
        public = client.post("/issues", json={"title": "Loose"}, headers=H_CREATOR).json()["id"]  # backlog
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        # The feed links each issue event to /aegis/issues/{id}. Anonymous sees the
        # backlog issue's event but never the private project's.
        text = client.get("/aegis/activity").text
        assert f"/aegis/issues/{public}" in text
        assert f"/aegis/issues/{hidden}" not in text


def test_inbox_filters_notifications_on_hidden_targets(tmp_path):
    db_file = tmp_path / "inbox.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        iid = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        # The outsider watches the issue (as if mentioned/added before it went private).
        conn.execute("INSERT INTO watches (user_id, target_kind, target_id) VALUES (3, 'issue', ?)", (iid,))
        conn.commit()
        # The creator changes the issue → an event fans out to the watcher's inbox.
        assert client.patch(f"/issues/{iid}", json={"status": "in_progress"}, headers=H_CREATOR).status_code == 200

        def inbox_targets():
            return {n["target_id"] for n in client.get("/notifications", headers=H_OUTSIDER).json()}

        def badge():
            return client.get("/notifications/unread_count", headers=H_OUTSIDER).json()["count"]

        # The notification exists but the outsider can't see its target → filtered out,
        # and the badge doesn't count it either.
        assert iid not in inbox_targets()
        assert badge() == 0

        # Granting membership reveals the notification and counts it.
        access.add_project_member(conn, pp, 3, added_by=2)
        assert iid in inbox_targets()
        assert badge() >= 1


def test_inbox_gates_page_notifications_by_space(tmp_path):
    # The 'page' arm of the gate, exercised through the notifications join (not just
    # the feed): a page notification in a private space is filtered for an outsider.
    db_file = tmp_path / "inbox_page.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        ps = client.post("/spaces", json={"key": "SSP", "name": "SecretSpace"}, headers=H_CREATOR).json()["id"]
        pid = client.post(f"/spaces/{ps}/pages", json={"title": "Runbook"}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE spaces SET visibility = 'private' WHERE id = ?", (ps,))
        conn.execute("INSERT INTO watches (user_id, target_kind, target_id) VALUES (3, 'page', ?)", (pid,))
        conn.commit()
        # An edit to the page fans out to the watcher's inbox.
        client.patch(f"/pages/{pid}", json={"body": "updated"}, headers=H_CREATOR)

        assert pid not in {n["target_id"] for n in client.get("/notifications", headers=H_OUTSIDER).json()}
        access.add_space_member(conn, ps, 3, added_by=2)
        assert pid in {n["target_id"] for n in client.get("/notifications", headers=H_OUTSIDER).json()}


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
            activity.record(conn, actor_id=2, verb="changed_status", target_kind=kind, target_id=99999, detail="orphan")
        conn.close()

        out = _feed_targets(client, H_OUTSIDER)
        adm = _feed_targets(client, H_ADMIN)
        for kind in ("issue", "page", "space"):
            assert (kind, 99999) not in out  # hidden from the non-admin viewer
            assert (kind, 99999) in adm      # admin is ungated


def test_events_stream_gates_by_actor(tmp_path):
    # The agent/integration drain (GET /events) is gated by the consumer's token actor,
    # exactly like the human feed — an outsider's token can't drain private events.
    db_file = tmp_path / "events.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        hi = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        pi = client.post("/issues", json={"title": "Public"}, headers=H_CREATOR).json()["id"]
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
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        hi = client.post("/issues", json={"title": "Hidden", "project_id": pp}, headers=H_CREATOR).json()["id"]
        pi = client.post("/issues", json={"title": "Public"}, headers=H_CREATOR).json()["id"]
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        def run_event_targets(headers):
            runs = client.get("/activity/runs?actor_id=2", headers=headers).json()
            return {(e["target_kind"], e["target_id"]) for run in runs for e in run["events"]}

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
        pp = client.post("/projects", json={"name": "Secret", "key": "SEC"}, headers=H_CREATOR).json()["id"]
        visible, hidden = [], []
        for n in range(5):
            hidden.append(client.post("/issues", json={"title": f"h{n}", "project_id": pp}, headers=H_CREATOR).json()["id"])
            visible.append(client.post("/issues", json={"title": f"v{n}"}, headers=H_CREATOR).json()["id"])
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (pp,))
        conn.commit()

        seen, before, pages = set(), None, 0
        while pages < 20:
            pages += 1
            url = "/activity?kind=issue&limit=3" + (f"&before_id={before}" if before else "")
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
