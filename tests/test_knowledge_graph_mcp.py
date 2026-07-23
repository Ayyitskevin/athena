"""Agents can walk the knowledge graph the database already stores.

The links table and page-version history were reachable over REST (backlinks,
versions, restore) but invisible to MCP, and a page's OUTGOING cross-links had no
endpoint at all. This adds GET /pages/{id}/outgoing-links and the MCP read/mutation
tools page_backlinks / page_outgoing_links / list_page_versions / get_page_version /
restore_page_version. These pin the outgoing-links endpoint (content + gating) and
the MCP graph-traversal path end to end.
"""

from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="kg.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _space(client, key="ENG", name="Eng"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _page(client, space_id, title, body=""):
    return client.post(
        f"/spaces/{space_id}/pages", json={"title": title, "body": body}, headers=H1
    ).json()


def _issue(client, title="work"):
    return client.post("/issues", json={"title": title}, headers=H1).json()


def test_outgoing_links_endpoint_returns_forward_edges(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        sp = _space(c)
        issue = _issue(c)
        target = _page(c, sp["id"], "Target")
        # A page whose body references the target page AND an issue.
        src = _page(
            c,
            sp["id"],
            "Source",
            body=f"see [[page:{target['id']}]] and [[issue:{issue['id']}]]",
        )
        out = c.get(f"/pages/{src['id']}/outgoing-links", headers=H1)
        assert out.status_code == 200
        links = {(link["kind"], link["id"]): link for link in out.json()}
        assert links[("page", target["id"])]["exists"] is True
        assert links[("page", target["id"])]["title"] == "Target"
        assert links[("issue", issue["id"])]["exists"] is True
        # And the backlinks of the target include the source (the mirror edge).
        back = c.get(f"/pages/{target['id']}/backlinks", headers=H1).json()
        assert (src["id"]) in [b["id"] for b in back if b["kind"] == "page"]


def test_outgoing_links_404_for_missing_page(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        assert c.get("/pages/4242/outgoing-links", headers=H1).status_code == 404


def test_mcp_graph_and_version_tools_end_to_end(tmp_path):
    app, db_file = _app(tmp_path)
    tc = TestClient(app)
    tc.__enter__()
    try:
        _bootstrap(tc)
        sp = _space(tc)
        target = _page(tc, sp["id"], "Target")
        link = "[[page:%d]]" % target["id"]
        v1_body = "v1 points at " + link
        v2_body = "v2 points at " + link
        src = _page(tc, sp["id"], "Source", body=v1_body)
        # Edit so v1 becomes history — keep the cross-link so outgoing survives too.
        tc.patch(f"/pages/{src['id']}", json={"body": v2_body}, headers=H1)
        raw = tc.post(
            "/tokens", json={"name": "t", "scopes": ["read", "docs:write"]}, headers=H1
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        tc.headers.pop("X-Athena-Actor", None)
        ath = AthenaClient(client=tc, run_id="kg-run")

        # Graph traversal (current body v2 still carries the link).
        outgoing = ath.page_outgoing_links(src["id"])
        assert [(o["kind"], o["id"]) for o in outgoing] == [("page", target["id"])]
        backlinks = ath.page_backlinks(target["id"])
        assert [(b["kind"], b["id"]) for b in backlinks] == [("page", src["id"])]

        # Version history + restore.
        versions = ath.list_page_versions(src["id"])
        assert [v["version"] for v in versions] == [1]
        assert versions[0]["body"] == v1_body
        snapshot = ath.get_page_version(src["id"], 1)
        assert snapshot["body"] == v1_body
        restored = ath.restore_page_version(src["id"], 1)
        assert restored["body"] == v1_body
        # The live page now reads the restored (v1) content again.
        assert ath.get_page(src["id"])["body"] == v1_body
    finally:
        tc.__exit__(None, None, None)
    # After restore, run attribution lands under the agent's run id.
    conn = db.connect(db_file)
    ev = conn.execute(
        "SELECT run_id FROM activity WHERE verb = 'page_restored'"
    ).fetchone()
    conn.close()
    assert ev["run_id"] == "kg-run"
