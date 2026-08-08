"""Page label attach/detach are audited-atomic commands.

These two page-association writes ran the join-table mutation and the activity
recorder as SEPARATE commits (labels.add_label_to_page committed, then
page_activity recorded its own transaction): a crash between them could label a
page with no 'page_labeled' event on the trail — the exact split-brain the
command migration exists to close, and the last Mentor write listed as debt in
docs/COMMAND_MIGRATION.md. page_commands.attach_page_label / detach_page_label
now fold each join write and its audit into one immediate transaction, reachable
identically from REST, the browser, and (via REST) MCP. These pin atomicity,
attribution, idempotence, and transport parity.
"""

from fastapi.testclient import TestClient

from athena.core import db, labels
from athena.main import create_app
from athena.mentor import page_commands

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="page_label_cmd.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _page(client, title="Doc"):
    sid = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1).json()[
        "id"
    ]
    return client.post(
        f"/spaces/{sid}/pages", json={"title": title}, headers=H1
    ).json()["id"]


def _label(client, name="runbook"):
    return client.post(
        "/labels", json={"name": name, "color": "#ff0000"}, headers=H1
    ).json()["id"]


def _events(db_file, verb):
    conn = db.connect(db_file)
    rows = conn.execute(
        "SELECT actor_id, target_id, detail, run_id FROM activity "
        "WHERE verb = ? ORDER BY id",
        (verb,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_attach_records_exactly_once_and_detach_matches(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c)
        lid = _label(c)
        assert (
            c.post(f"/pages/{pid}/labels", json={"label_id": lid}, headers=H1)
        ).status_code == 201
        # Idempotent re-attach records NOTHING new — the command decides, not the
        # transport, so a retry can't double-write the trail.
        assert (
            c.post(f"/pages/{pid}/labels", json={"label_id": lid}, headers=H1)
        ).status_code == 201
        assert c.delete(f"/pages/{pid}/labels/{lid}", headers=H1).status_code == 200
    labeled = _events(db_file, "page_labeled")
    unlabeled = _events(db_file, "page_unlabeled")
    assert [(e["actor_id"], e["target_id"], e["detail"]) for e in labeled] == [
        (1, pid, "runbook")
    ]
    assert [(e["actor_id"], e["target_id"], e["detail"]) for e in unlabeled] == [
        (1, pid, "runbook")
    ]


def test_attach_rolls_back_the_join_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c)
        lid = _label(c)
    conn = db.connect(db_file)
    original = page_activity.record_page_label_added

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_label_added = boom
    try:
        try:
            page_commands.attach_page_label(
                conn, actor={"id": 1}, page_id=pid, label_id=lid
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_label_added = original
    # The attach rolled back with its failed event — no pairing, no event. Before
    # the migration the pairing would have survived with no trail entry.
    assert labels.labels_for_page(conn, pid) == []
    conn.close()
    assert _events(db_file, "page_labeled") == []


def test_detach_rolls_back_the_join_when_audit_fails(tmp_path):
    import athena.mentor.page_activity as page_activity

    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c)
        lid = _label(c)
        assert (
            c.post(f"/pages/{pid}/labels", json={"label_id": lid}, headers=H1)
        ).status_code == 201
    conn = db.connect(db_file)
    original = page_activity.record_page_label_removed

    def boom(*a, **k):
        raise RuntimeError("audit down")

    page_activity.record_page_label_removed = boom
    try:
        try:
            page_commands.detach_page_label(
                conn, actor={"id": 1}, page_id=pid, label_id=lid
            )
            raise AssertionError("expected the recorder failure to propagate")
        except RuntimeError:
            pass
    finally:
        page_activity.record_page_label_removed = original
    # The detach rolled back with its failed event — the label is still attached.
    assert [la["id"] for la in labels.labels_for_page(conn, pid)] == [lid]
    conn.close()
    assert _events(db_file, "page_unlabeled") == []


def test_command_guards_match_the_transport_contract(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        pid = _page(c)
        lid = _label(c)
        # Unknown label is the command's 'invalid' → 422 (not an FK 500); a label
        # that isn't attached is the command's 'not_found' → 404.
        assert (
            c.post(f"/pages/{pid}/labels", json={"label_id": 9999}, headers=H1)
        ).status_code == 422
        assert c.delete(f"/pages/{pid}/labels/{lid}", headers=H1).status_code == 404


def test_web_add_by_name_shares_the_command(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        c.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        r = c.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        c.headers["X-CSRF-Token"] = c.cookies.get("athena_csrf", "")
        pid = _page(c)
        assert (
            c.post(
                f"/mentor/pages/{pid}/labels",
                data={"name": "frontend"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        lid = c.get(f"/pages/{pid}").json()["labels"][0]["id"]
        # Double-submit removal stays a forgiving 303 no-op in the UI while the
        # trail still shows exactly one attach and one detach.
        for _ in range(2):
            assert (
                c.post(
                    f"/mentor/pages/{pid}/labels/{lid}/delete",
                    follow_redirects=False,
                ).status_code
                == 303
            )
    assert [(e["actor_id"], e["detail"]) for e in _events(db_file, "page_labeled")] == [
        (1, "frontend")
    ]
    assert [
        (e["actor_id"], e["detail"]) for e in _events(db_file, "page_unlabeled")
    ] == [(1, "frontend")]


def test_mcp_client_reaches_the_page_label_writes(tmp_path):
    from athena.mcp.client import AthenaClient

    app, db_file = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        pid = _page(tc)
        lid = _label(tc)
        raw = tc.post(
            "/tokens", json={"name": "t", "scopes": ["read", "docs:write"]}, headers=H1
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        tc.headers.pop("X-Athena-Actor", None)
        ath = AthenaClient(client=tc, run_id="page-label-run")
        assert [la["id"] for la in ath.label_page(pid, lid)["labels"]] == [lid]
        assert ath.unlabel_page(pid, lid)["labels"] == []
    finally:
        tc.__exit__(None, None, None)
    # MCP reaches the same command through REST — both events carry the run id.
    assert [e["run_id"] for e in _events(db_file, "page_labeled")] == ["page-label-run"]
    assert [e["run_id"] for e in _events(db_file, "page_unlabeled")] == [
        "page-label-run"
    ]
