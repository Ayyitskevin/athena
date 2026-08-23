"""The 8→9 loop: desk narration, optional path fence, honest complete."""

from fastapi.testclient import TestClient

from athena.aegis import issue_commands, issues, lease_commands
from athena.core import db
from athena.main import create_app


def _migrated(tmp_path, name):
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) VALUES ('o@e.com','O','admin',0)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('a@e.com','AgentA',1)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('b@e.com','AgentB',1)"
    )
    conn.commit()
    return conn


def _actor(conn, uid):
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def _delegated_pair(conn):
    admin = _actor(conn, 1)
    first = issues.create_issue(conn, title="one", body="a", created_by=1)
    second = issues.create_issue(conn, title="two", body="b", created_by=1)
    for issue in (first, second):
        for agent_id in (2, 3):
            issue_commands.add_contributor(
                conn,
                actor=admin,
                issue_id=issue["id"],
                user_id=agent_id,
                require_agent=True,
            )
    return first, second


def _tag(conn, issue):
    from athena.aegis import issue_etags

    return issue_etags.current_etag(conn, issues.get_issue(conn, issue["id"]))


def test_declared_paths_conflict_across_issues(tmp_path):
    conn = _migrated(tmp_path, "paths.db")
    first, second = _delegated_pair(conn)
    lease_commands.claim_issue(
        conn,
        actor=_actor(conn, 2),
        issue_id=first["id"],
        if_match=[_tag(conn, first)],
        paths=["src/athena/aegis/api.py"],
    )
    try:
        lease_commands.claim_issue(
            conn,
            actor=_actor(conn, 3),
            issue_id=second["id"],
            if_match=[_tag(conn, second)],
            paths=["src/athena/aegis"],
        )
        raise AssertionError("prefix overlap should 409")
    except issue_commands.IssueCommandError as exc:
        assert exc.kind == "conflict"
        assert "src/athena/aegis" in exc.detail
    other = lease_commands.claim_issue(
        conn,
        actor=_actor(conn, 3),
        issue_id=second["id"],
        if_match=[_tag(conn, second)],
        paths=["src/athena/mentor/pages.py"],
    )
    assert other["declared_paths"] == ["src/athena/mentor/pages.py"]


def test_path_overlap_uses_directory_boundaries(tmp_path):
    assert lease_commands.paths_overlap("src/foo", "src/foo/bar.py")
    assert not lease_commands.paths_overlap("src/foo", "src/foobar")


def test_empty_paths_still_claim(tmp_path):
    conn = _migrated(tmp_path, "empty-paths.db")
    first, _second = _delegated_pair(conn)
    lease = lease_commands.claim_issue(
        conn,
        actor=_actor(conn, 2),
        issue_id=first["id"],
        if_match=[_tag(conn, first)],
    )
    assert lease["declared_paths"] == []
    assert lease["holder_id"] == 2


def test_complete_claim_says_the_issue_is_still_open(tmp_path):
    conn = _migrated(tmp_path, "complete.db")
    first, _second = _delegated_pair(conn)
    lease = lease_commands.claim_issue(
        conn,
        actor=_actor(conn, 2),
        issue_id=first["id"],
        if_match=[_tag(conn, first)],
    )
    result = lease_commands.complete_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=first["id"],
        generation=lease["generation"],
    )
    assert result["released"] is True
    assert result["issue_still_open"] is True
    assert result["issue_status"] == "open"
    assert "unchanged" in result["next"]
    assert "PATCH" not in result["next"]


def test_rest_desk_and_work_context_narrate_the_claim(tmp_path):
    app = create_app(tmp_path / "narrate.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "o@e.com", "name": "O", "password": "pw"})
        agent = client.post(
            "/users/onboard_agent",
            json={"name": "Sol", "scopes": ["read", "issue:write", "docs:write"]},
            headers={"X-Athena-Actor": "1"},
        ).json()
        issue = client.post(
            "/issues", json={"title": "narrate"}, headers={"X-Athena-Actor": "1"}
        ).json()
        client.post(
            f"/issues/{issue['id']}/delegate",
            json={"user_id": agent["user"]["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        bearer = {"Authorization": f"Bearer {agent['token']['token']}"}
        desk = client.get("/desk", headers=bearer).json()
        item = desk["work"]["delegations"]["items"][0]
        assert item["issue_etag"].startswith('"sha256-')
        assert item["how_to_claim"]["header"] == "If-Match"
        context = client.get(
            f"/issues/{issue['id']}/work-context", headers=bearer
        ).json()
        assert context["issue_etag"] == item["issue_etag"]
        assert context["how_to_claim"]["from"] == "issue_etag"
        assert context["complete_does_not_close_issue"] is True
        claimed = client.post(
            f"/issues/{issue['id']}/claim",
            json={"paths": ["docs/README.md"]},
            headers={**bearer, "If-Match": item["issue_etag"]},
        )
        assert claimed.status_code == 201, claimed.text
        assert claimed.json()["declared_paths"] == ["docs/README.md"]
