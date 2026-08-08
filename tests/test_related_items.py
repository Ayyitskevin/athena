"""Related items — co-citation over the links the workspace already stores.

The claim under test: "what cites what this cites, but is not linked to it
yet" can be answered from the existing ``links`` rows, derived at read time,
with no embeddings, no stored score, and no new table. The properties that
would rot silently, each pinned:

* **direct neighbours are excluded** — the list answers what backlinks cannot,
  or it is just backlinks again with a score on it;
* **ranking is shared-neighbour count, ties deterministic** — the same
  workspace gives the same list every time;
* **visibility gates both roles** — an invisible candidate is not a result,
  and an invisible intermediate does not CONDUCT, or the score becomes an
  existence oracle for hidden things;
* **the bound is disclosed** — a cut list says what it cut, like every other
  bounded read here;
* **every surface agrees** — REST (both kinds), the work-context packet, and
  MCP all serve the same derivation.
"""

import pytest
from fastapi.testclient import TestClient

from athena.core import db, graph
from athena.main import create_app
from athena.mcp.client import AthenaClient

H1 = {"X-Athena-Actor": "1"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _space(client, key="S", name="Space"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _page(client, space_id, title, body=""):
    return client.post(
        f"/spaces/{space_id}/pages",
        json={"title": title, "body": body},
        headers=H1,
    ).json()


def _project(client, key="ATH"):
    return client.post("/projects", json={"key": key, "name": key}, headers=H1).json()


def _issue(client, project_id, title, body=""):
    return client.post(
        "/issues",
        json={"title": title, "project_id": project_id, "body": body},
        headers=H1,
    ).json()


@pytest.fixture()
def world(tmp_path):
    """A small citation web, built over the real API so links derive normally.

    Hub pages A and B; issue FOCUS cites both; issue TWIN also cites both;
    issue HALF cites only A; issue LINKED cites A, B, and FOCUS itself (so it
    is a DIRECT neighbour of the focus); issue LONER cites nothing.
    Expected related list for FOCUS: TWIN (shared 2), then HALF (shared 1) —
    LINKED absent despite sharing both hubs, because it is already linked.
    """
    app = create_app(tmp_path / "related.db")
    with TestClient(app) as c:
        _bootstrap(c)
        space = _space(c)
        hub_a = _page(c, space["id"], "Hub A")
        hub_b = _page(c, space["id"], "Hub B")
        project = _project(c)
        cite = lambda *ids: " ".join(f"[[page:{i}]]" for i in ids)  # noqa: E731
        focus = _issue(c, project["id"], "Focus", cite(hub_a["id"], hub_b["id"]))
        twin = _issue(c, project["id"], "Twin", cite(hub_a["id"], hub_b["id"]))
        half = _issue(c, project["id"], "Half", cite(hub_a["id"]))
        linked = _issue(
            c,
            project["id"],
            "Linked",
            cite(hub_a["id"], hub_b["id"]) + f" [[issue:{focus['id']}]]",
        )
        _issue(c, project["id"], "Loner")
        return {
            "app": app,
            "db": tmp_path / "related.db",
            "space": space["id"],
            "hub_a": hub_a["id"],
            "hub_b": hub_b["id"],
            "project": project["id"],
            "focus": focus["id"],
            "twin": twin["id"],
            "half": half["id"],
            "linked": linked["id"],
        }


@pytest.fixture()
def conn(world):
    connection = db.connect(world["db"])
    yield connection
    connection.close()


# --- the derivation ----------------------------------------------------------


def test_ranked_by_shared_neighbours_with_direct_links_excluded(conn, world):
    result = graph.related_items(
        conn, kind="issue", node_id=world["focus"], actor={"id": 1}
    )
    got = [(i["id"], i["shared"]) for i in result["items"] if i["kind"] == "issue"]
    # TWIN shares both hubs; HALF shares one; LINKED — which also shares both —
    # is absent because it cites the focus directly, and LONER shares nothing.
    assert got == [(world["twin"], 2), (world["half"], 1)]
    assert world["linked"] not in [i["id"] for i in result["items"]]
    # The hubs themselves are direct neighbours of the focus: absent too.
    page_ids = [i["id"] for i in result["items"] if i["kind"] == "page"]
    assert world["hub_a"] not in page_ids and world["hub_b"] not in page_ids
    assert result["truncated"] is False and result["shown"] == result["total"]


def test_relatedness_is_symmetric_through_the_undirected_walk(conn, world):
    # From TWIN's side the focus is related the same way — the adjacency is
    # undirected, so co-citation cannot depend on who happened to write links.
    result = graph.related_items(
        conn, kind="issue", node_id=world["twin"], actor={"id": 1}
    )
    ids = [(i["kind"], i["id"]) for i in result["items"]]
    assert ("issue", world["focus"]) in ids


def test_the_bound_is_disclosed(conn, world):
    result = graph.related_items(
        conn, kind="issue", node_id=world["focus"], actor={"id": 1}, limit=1
    )
    assert result["shown"] == 1
    assert result["total"] == 2
    assert result["truncated"] is True
    assert result["limit"] == 1
    # The kept item is the strongest, not an arbitrary slice.
    assert result["items"][0]["id"] == world["twin"]


def test_a_missing_or_hidden_focus_is_the_same_empty_answer(conn, world):
    missing = graph.related_items(conn, kind="issue", node_id=99999, actor={"id": 1})
    assert missing["focus"] is None and missing["items"] == []


# --- visibility: absent, and non-conducting ----------------------------------


def test_hidden_things_neither_appear_nor_conduct(world, tmp_path):
    # Rebuild the shape with the hubs in a PRIVATE space and a viewer outside
    # it: TWIN still co-cites both hubs, but for that viewer the hubs do not
    # exist — so TWIN must not appear related, or the score leaks that a hidden
    # bridge connects them. For the member the same rows say TWIN plainly.
    app = create_app(tmp_path / "hidden.db")
    with TestClient(app) as c:
        _bootstrap(c)
        c.post(
            "/users",
            json={"email": "out@e.com", "name": "Out", "password": "pw"},
            headers=H1,
        )
        space = _space(c, key="PRV", name="Private")
        assert (
            c.put(
                f"/spaces/{space['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )
        hub = _page(c, space["id"], "Hidden hub")
        project = _project(c, key="VIS")
        focus = _issue(c, project["id"], "Focus", f"[[page:{hub['id']}]]")
        twin = _issue(c, project["id"], "Twin", f"[[page:{hub['id']}]]")

    conn = db.connect(tmp_path / "hidden.db")
    try:
        outsider = graph.related_items(
            conn, kind="issue", node_id=focus["id"], actor={"id": 2, "role": "member"}
        )
        assert outsider["items"] == [] and outsider["total"] == 0
        insider = graph.related_items(
            conn, kind="issue", node_id=focus["id"], actor={"id": 1, "role": "admin"}
        )
        assert [i["id"] for i in insider["items"]] == [twin["id"]]
    finally:
        conn.close()


# --- the surfaces ------------------------------------------------------------


def test_both_rest_routes_serve_the_derivation(world):
    with TestClient(world["app"]) as c:
        issue_side = c.get(f"/issues/{world['focus']}/related", headers=H1)
        assert issue_side.status_code == 200
        assert [i["id"] for i in issue_side.json()["items"]] == [
            world["twin"],
            world["half"],
        ]
        # The page side: Hub A is cited by focus, twin, half, and linked — all
        # direct neighbours — so its related list holds what co-cites Hub B
        # beside it, minus anything already linked to Hub A.
        page_side = c.get(f"/pages/{world['hub_a']}/related", headers=H1)
        assert page_side.status_code == 200
        assert page_side.json()["focus"] == {"kind": "page", "id": world["hub_a"]}
        # And a hidden/missing focus 404s at the route, same as its siblings.
        assert c.get("/issues/99999/related", headers=H1).status_code == 404
        # limit is clamped, not trusted.
        clamped = c.get(
            f"/issues/{world['focus']}/related?limit=9999", headers=H1
        ).json()
        assert clamped["limit"] == graph.MAX_RELATED_LIMIT


def test_the_work_context_packet_carries_the_section(world):
    with TestClient(world["app"]) as c:
        packet = c.get(f"/issues/{world['focus']}/work-context", headers=H1).json()
        related = packet["related"]
        assert [i["id"] for i in related["items"]] == [world["twin"], world["half"]]
        assert related["shown"] == 2
        assert related["visible_total"] == 2
        assert related["truncated"] is False
        # Direct references live in their own section, not here.
        assert "references" in packet


def test_mcp_serves_the_same_answer_for_both_kinds(world):
    with TestClient(world["app"]) as tc:
        tc.headers.update(H1)
        client = AthenaClient(client=tc)
        issue_side = client.related_items("issue", world["focus"])
        assert [i["id"] for i in issue_side["items"]] == [
            world["twin"],
            world["half"],
        ]
        page_side = client.related_items("page", world["hub_a"], limit=3)
        assert page_side["limit"] == 3
