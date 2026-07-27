"""The knowledge graph — unlinked mentions, the ego graph, templates, daily notes.

Four features, one thesis: the link graph Athena already stores should be
traversable, growable, and trustworthy. They are tested in the three layers that
fail differently:

* the **mention rules** (`core/mentions.py`), pure and fixture-free — what counts
  as prose naming a thing, and what is code, markup, or an already-attempted link;
* the **resolvers** (`core/graph.py`, `core/mentions.py` against a database),
  where the VIEWER'S visibility decides what exists;
* the **surfaces**, where a suggestion becomes an ordinary audited edit.

The rule threaded through all of it: finding a mention proposes an edge and never
creates one, and a bounded picture says what it left out.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from athena.core import db, graph, mentions
from athena.main import create_app
from athena.mentor import page_templates

H1 = {"X-Athena-Actor": "1"}
GUIDE = "Fleet operating guide"


def _app(tmp_path, name="graph.db"):
    return create_app(tmp_path / name)


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _user(client, email):
    return client.post(
        "/users",
        json={"email": email, "name": email.split("@")[0], "password": "pw"},
        headers=H1,
    ).json()


def _login(client, email="a@e.com"):
    client.post("/login", data={"email": email, "password": "pw"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _agent(client, email="sol@e.com", scopes=("read", "docs:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _space(client, key="S", name="Space"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _page(client, space_id, title, body="", headers=H1):
    return client.post(
        f"/spaces/{space_id}/pages",
        json={"title": title, "body": body},
        headers=headers,
    ).json()


def _project(client, key="ATH"):
    return client.post("/projects", json={"key": key, "name": key}, headers=H1).json()


def _issue(client, project_id, title, body=""):
    return client.post(
        "/issues",
        json={"title": title, "body": body, "project_id": project_id},
        headers=H1,
    ).json()


# --- the mention rules (no database) ----------------------------------------


@pytest.mark.parametrize(
    "body,found,why",
    [
        ("see the Fleet operating guide today", True, "plain prose"),
        ("> quoting the Fleet operating guide", True, "a blockquote is still prose"),
        ("**Fleet operating guide** matters", True, "emphasis is prose"),
        ("```\nrun Fleet operating guide\n```", False, "fenced code is a literal"),
        ("~~~\nFleet operating guide\n~~~", False, "tilde fences count too"),
        ("use `Fleet operating guide` here", False, "an inline code span"),
        ("see [[Fleet operating guide]] ok", False, "already an attempted link"),
        ("[x](/docs/Fleet operating guide)", False, "inside a link target"),
        ("```\nFleet operating guide", False, "an unterminated fence swallows on"),
    ],
)
def test_what_counts_as_a_mention(body, found, why):
    assert (mentions.find_occurrence(body, GUIDE) >= 0) is found, why


@pytest.mark.parametrize(
    "body,needle,found",
    [
        ("Fleetwood Mac", "Fleet", False),  # never mid-word
        ("the Fleet, and", "Fleet", True),  # punctuation is a boundary
        ("ATH-12 is open", "ATH-12", True),
        ("ATH-123 is another", "ATH-12", False),  # a key is not a prefix
        ("Q&A matters", "Q&A", True),  # a needle may END in punctuation
        ("read v2.0 now", "v2.0", True),
    ],
)
def test_a_mention_never_fires_mid_word(body, needle, found):
    assert (mentions.find_occurrence(body, needle) >= 0) is found


def test_linkify_replaces_only_the_first_real_mention():
    body = "The Fleet operating guide and the Fleet operating guide again."
    out = mentions.linkify_first(body, GUIDE, f"[[{GUIDE}]]")
    assert out == f"The [[{GUIDE}]] and the {GUIDE} again."


def test_linkify_skips_code_and_edits_the_prose_after_it():
    body = f"```\n{GUIDE}\n```\nand then the {GUIDE} in prose"
    out = mentions.linkify_first(body, GUIDE, "TOKEN")
    assert out == f"```\n{GUIDE}\n```\nand then the TOKEN in prose"


def test_linkify_refuses_when_there_is_no_mention_left():
    # The honest answer to a race: the body moved, the mention is gone.
    assert mentions.linkify_first("nothing here", GUIDE, "TOKEN") is None


# --- mentions against a database --------------------------------------------


def test_a_page_title_mentioned_in_prose_is_suggested(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "How the fleet runs.")
        prose = _page(c, space["id"], "Prose", f"Read the {GUIDE} before a shift.")
        found = c.get(f"/pages/{guide['id']}/unlinked-mentions", headers=H1).json()
        assert found["needle"] == GUIDE
        assert [(i["kind"], i["id"]) for i in found["items"]] == [("page", prose["id"])]
        # The excerpt exists so the operator can judge without opening the source.
        assert GUIDE in found["items"][0]["excerpt"]


def test_an_already_linked_mention_is_not_suggested(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "How the fleet runs.")
        _page(c, space["id"], "Linked", f"See [[{GUIDE}]] here.")
        found = c.get(f"/pages/{guide['id']}/unlinked-mentions", headers=H1).json()
        assert found["items"] == []  # a linked mention is just a link


def test_a_thing_does_not_mention_itself(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        # The body repeats its own title — a real pattern (a page starting with an
        # H1). It must not offer to link the page to itself.
        guide = _page(c, space["id"], GUIDE, f"# {GUIDE}\n\nHow the fleet runs.")
        found = c.get(f"/pages/{guide['id']}/unlinked-mentions", headers=H1).json()
        assert found["items"] == []


def test_an_issue_is_mentioned_by_its_key_not_its_title(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        proj = _project(c)
        issue = _issue(c, proj["id"], "Fix the login redirect", "body")
        _page(c, space["id"], "Notes", "Blocked on ATH-1 until Friday.")
        # A page repeating the issue's TITLE must not be suggested: issue titles are
        # sentences that recur in prose constantly, and matching them would bury the
        # operator in false positives.
        _page(c, space["id"], "Chatter", "We should fix the login redirect someday.")
        found = c.get(f"/issues/{issue['id']}/unlinked-mentions", headers=H1).json()
        assert found["needle"] == "ATH-1"
        assert [i["title"] for i in found["items"]] == ["Notes"]


def test_a_backlog_issue_has_no_name_to_be_mentioned_by(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        issue = c.post("/issues", json={"title": "Loose end"}, headers=H1).json()
        found = c.get(f"/issues/{issue['id']}/unlinked-mentions", headers=H1).json()
        # Says so, rather than showing an empty list that reads as "no mentions".
        assert found["needle"] is None
        assert found["items"] == []


def test_a_comment_is_never_offered_as_a_mention_source(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "How the fleet runs.")
        host = _page(c, space["id"], "Host", "nothing here")
        c.post(
            f"/pages/{host['id']}/comments",
            json={"body": f"see the {GUIDE}"},
            headers=H1,
        )
        found = c.get(f"/pages/{guide['id']}/unlinked-mentions", headers=H1).json()
        # Comments are in the FTS index, but "link it" would have no body to edit
        # through a page command — so offering one would be an unhonorable offer.
        assert found["items"] == []


def test_a_mention_in_private_work_never_surfaces_to_an_outsider(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "b@e.com")
        pub, priv = _space(c, "PUB", "Public"), _space(c, "PRIV", "Private")
        c.put(
            f"/spaces/{priv['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        guide = _page(c, pub["id"], GUIDE, "How the fleet runs.")
        secret = _page(c, priv["id"], "Secret", f"Supersede the {GUIDE}.")
        open_doc = _page(c, pub["id"], "Open", f"Read the {GUIDE}.")

        admin_ids = {
            i["id"]
            for i in c.get(
                f"/pages/{guide['id']}/unlinked-mentions", headers=H1
            ).json()["items"]
        }
        assert admin_ids == {secret["id"], open_doc["id"]}

        bob_view = c.get(
            f"/pages/{guide['id']}/unlinked-mentions",
            headers={"X-Athena-Actor": str(bob["id"])},
        ).json()
        # The wording of private work must not leak through a public page's
        # sidebar — a mention list is a back door onto exactly that.
        assert [i["id"] for i in bob_view["items"]] == [open_doc["id"]]


def test_the_link_token_falls_back_to_the_numeric_form_when_a_title_is_ambiguous(
    tmp_path,
):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        one, two = _space(c, "A", "A"), _space(c, "B", "B")
        first = _page(c, one["id"], "Shared Title", "x")
        _page(c, two["id"], "Shared Title", "y")
    conn = db.connect(tmp_path / "graph.db")
    try:
        # resolve_title_ref refuses an ambiguous title, so [[Shared Title]] would
        # record NO link — the "link it" would silently do nothing.
        assert mentions.link_token(conn, "page", first["id"]) == (
            f"[[page:{first['id']}]]"
        )
    finally:
        conn.close()


# --- taking a suggestion is an ordinary, audited edit ------------------------


def test_linking_a_mention_edits_the_source_and_creates_a_real_backlink(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "How the fleet runs.")
        doc = _page(c, space["id"], "Open", f"Read the {GUIDE} first.")

        resp = c.post(
            f"/pages/{doc['id']}/link-mention",
            json={"target_kind": "page", "target_id": guide["id"]},
            headers=H1,
        )
        assert resp.status_code == 200
        assert resp.json()["body"] == f"Read the [[{GUIDE}]] first."

        # The proposed edge is now a real one, both ways.
        backlinks = c.get(f"/pages/{guide['id']}/backlinks", headers=H1).json()
        assert [b["id"] for b in backlinks] == [doc["id"]]
        # And it is no longer an unlinked mention.
        after = c.get(f"/pages/{guide['id']}/unlinked-mentions", headers=H1).json()
        assert after["items"] == []


def test_linking_a_mention_is_recorded_as_an_ordinary_page_edit(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        doc = _page(c, space["id"], "Open", f"Read the {GUIDE}.")
        before = len(c.get(f"/pages/{doc['id']}/versions", headers=H1).json())
        c.post(
            f"/pages/{doc['id']}/link-mention",
            json={"target_kind": "page", "target_id": guide["id"]},
            headers=H1,
        )
        # A version snapshot and a page_edited event, exactly like a hand edit —
        # the whole point of routing the suggestion through the ordinary command.
        assert len(c.get(f"/pages/{doc['id']}/versions", headers=H1).json()) == (
            before + 1
        )
        events = c.get(
            f"/activity?target_kind=page&target_id={doc['id']}", headers=H1
        ).json()
        assert any(e["verb"] == "page_edited" for e in events)


def test_linking_a_vanished_mention_is_refused_not_guessed(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        doc = _page(c, space["id"], "Open", f"Read the {GUIDE}.")
        payload = {"target_kind": "page", "target_id": guide["id"]}
        assert (
            c.post(
                f"/pages/{doc['id']}/link-mention", json=payload, headers=H1
            ).status_code
            == 200
        )
        # The mention the caller acted on is gone now. Editing anyway would
        # rewrite text they never saw.
        second = c.post(f"/pages/{doc['id']}/link-mention", json=payload, headers=H1)
        assert second.status_code == 409


def test_an_issue_can_take_a_mention_of_a_page(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space, proj = _space(c), _project(c)
        guide = _page(c, space["id"], GUIDE, "x")
        issue = _issue(c, proj["id"], "Onboard", f"Follow the {GUIDE}, then check in.")
        resp = c.post(
            f"/issues/{issue['id']}/link-mention",
            json={"target_kind": "page", "target_id": guide["id"]},
            headers=H1,
        )
        assert resp.status_code == 200
        assert resp.json()["body"] == f"Follow the [[{GUIDE}]], then check in."


# --- the ego graph ----------------------------------------------------------


def test_the_graph_holds_what_links_in_either_direction(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space, proj = _space(c), _project(c)
        guide = _page(c, space["id"], GUIDE, "x")
        _page(c, space["id"], "Inbound", f"see [[{GUIDE}]]")
        outbound = _page(c, space["id"], "Outbound", "y")
        c.patch(
            f"/pages/{guide['id']}",
            json={"body": f"see [[page:{outbound['id']}]]"},
            headers=H1,
        )
        _issue(c, proj["id"], "Ship", f"per [[{GUIDE}]]")

        g = c.get(f"/pages/{guide['id']}/graph", headers=H1).json()
        titles = {n["title"] for n in g["nodes"]}
        # Adjacency is undirected: "what neighbourhood am I in" does not care
        # which way the reference points.
        assert titles == {GUIDE, "Inbound", "Outbound", "Ship"}
        assert next(n for n in g["nodes"] if n["title"] == GUIDE)["is_focus"]
        assert not g["truncated"]


def test_a_private_neighbour_is_absent_from_an_outsiders_graph(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "b@e.com")
        pub, priv = _space(c, "PUB", "Public"), _space(c, "PRIV", "Private")
        c.put(
            f"/spaces/{priv['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        guide = _page(c, pub["id"], GUIDE, "x")
        _page(c, priv["id"], "Secret plan", f"supersedes [[{GUIDE}]]")
        _page(c, pub["id"], "Open doc", f"see [[{GUIDE}]]")

        admin = c.get(f"/pages/{guide['id']}/graph", headers=H1).json()
        outsider = c.get(
            f"/pages/{guide['id']}/graph",
            headers={"X-Athena-Actor": str(bob["id"])},
        ).json()
        assert {n["title"] for n in admin["nodes"]} == {
            GUIDE,
            "Secret plan",
            "Open doc",
        }
        assert {n["title"] for n in outsider["nodes"]} == {GUIDE, "Open doc"}
        # And the COUNT must not betray it either: a "showing 2 of 3" line would
        # be an existence oracle for the private page.
        assert outsider["total"] == 2


def test_the_private_neighbour_is_absent_from_the_rendered_page_too(tmp_path):
    # The renderer is a second, independent path to the same leak: a graph view
    # handed the wrong viewer would draw the private node into real HTML. This
    # fails if the web route stops passing the session user.
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _user(c, "b@e.com")
        pub, priv = _space(c, "PUB", "Public"), _space(c, "PRIV", "Private")
        c.put(
            f"/spaces/{priv['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        guide = _page(c, pub["id"], GUIDE, "x")
        _page(c, priv["id"], "Secret plan", f"supersedes [[{GUIDE}]]")
    with TestClient(app) as outsider:
        _login(outsider, "b@e.com")
        html = outsider.get(f"/mentor/pages/{guide['id']}/graph").text
        assert "Secret plan" not in html
    with TestClient(app) as admin:
        _login(admin)
        assert "Secret plan" in admin.get(f"/mentor/pages/{guide['id']}/graph").text


def test_a_bounded_graph_reports_what_it_left_out(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        for n in range(6):
            _page(c, space["id"], f"Neighbour {n}", f"see [[{GUIDE}]]")
        g = c.get(f"/pages/{guide['id']}/graph?max_nodes=3", headers=H1).json()
        assert g["shown"] == 3
        assert g["total"] == 7  # focus + six neighbours
        # An unlabelled partial graph reads as the whole neighbourhood.
        assert g["truncated"] is True


def test_the_layout_is_deterministic(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        for n in range(4):
            _page(c, space["id"], f"N{n}", f"see [[{GUIDE}]]")
        first = c.get(f"/pages/{guide['id']}/graph", headers=H1).json()
        second = c.get(f"/pages/{guide['id']}/graph", headers=H1).json()
        # No seed to fix, because there is no randomness: same graph, same picture.
        assert first == second


def test_depth_bounds_the_walk(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        # A chain: far -> near -> guide. `far` is two hops from the focus.
        guide = _page(c, space["id"], GUIDE, "x")
        near = _page(c, space["id"], "Near", f"see [[{GUIDE}]]")
        _page(c, space["id"], "Far", f"see [[page:{near['id']}]]")

        one = c.get(f"/pages/{guide['id']}/graph?depth=1", headers=H1).json()
        two = c.get(f"/pages/{guide['id']}/graph?depth=2", headers=H1).json()
        assert {n["title"] for n in one["nodes"]} == {GUIDE, "Near"}
        assert {n["title"] for n in two["nodes"]} == {GUIDE, "Near", "Far"}


def test_a_broken_link_is_not_a_node(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        # [[page:9999]] indexes a link row to a page that does not exist. A graph
        # of THINGS should not sprout a titleless ghost.
        _page(c, space["id"], "Broken", f"see [[{GUIDE}]] and [[page:9999]]")
        g = c.get(f"/pages/{guide['id']}/graph", headers=H1).json()
        assert all(n["title"] for n in g["nodes"])
        assert {n["title"] for n in g["nodes"]} == {GUIDE, "Broken"}


def test_a_hidden_or_missing_focus_yields_an_empty_graph(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        _space(c)
    conn = db.connect(tmp_path / "graph.db")
    try:
        empty = graph.ego_graph(
            conn, kind="page", node_id=9999, actor={"id": 1, "role": "admin"}
        )
        assert empty["focus"] is None and empty["nodes"] == []
    finally:
        conn.close()


def test_edges_only_join_nodes_that_are_actually_drawn(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        for n in range(5):
            _page(c, space["id"], f"N{n}", f"see [[{GUIDE}]]")
        g = c.get(f"/pages/{guide['id']}/graph?max_nodes=3", headers=H1).json()
        # An edge pointing at a node the ceiling cut would read as a rendering bug.
        for edge in g["edges"]:
            assert 0 <= edge["source"] < len(g["nodes"])
            assert 0 <= edge["target"] < len(g["nodes"])


# --- templates --------------------------------------------------------------


def _template_label(client):
    """The shared `template` label, created once. Label names are UNIQUE, so a
    second create is a 409 — the vocabulary is workspace-wide by design."""
    made = client.post(
        "/labels", json={"name": page_templates.TEMPLATE_LABEL}, headers=H1
    )
    if made.status_code == 201:
        return made.json()
    existing = client.get("/labels", headers=H1).json()
    return next(
        label
        for label in existing
        if label["name"].lower() == page_templates.TEMPLATE_LABEL
    )


def _make_template(client, space_id, title, body):
    page = _page(client, space_id, title, body)
    label = _template_label(client)
    client.post(
        f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=H1
    )
    return page


def test_a_template_is_just_a_labelled_page_in_this_space(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        here, elsewhere = _space(c, "A", "A"), _space(c, "B", "B")
        tmpl = _make_template(c, here["id"], "Postmortem", "## What happened")
        _make_template(c, elsewhere["id"], "Other", "x")
        _page(c, here["id"], "Ordinary", "not a template")
        listed = c.get(f"/spaces/{here['id']}/templates", headers=H1).json()
        # Scoped to the space: a template is offered where it is used.
        assert [p["id"] for p in listed] == [tmpl["id"]]


def test_a_page_from_a_template_copies_the_body_but_never_the_label(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        tmpl = _make_template(c, space["id"], "Postmortem", "## What happened\n")
        made = c.post(
            f"/spaces/{space['id']}/pages/from-template",
            json={"template_id": tmpl["id"], "title": "Outage 1"},
            headers=H1,
        ).json()
        assert made["body"] == "## What happened\n"
        # Inheriting the label would make every page from a template a template —
        # a self-replicating menu.
        assert made["labels"] == []
        assert [
            p["id"]
            for p in c.get(f"/spaces/{space['id']}/templates", headers=H1).json()
        ] == [tmpl["id"]]


def test_the_only_substitutions_are_title_and_date(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        tmpl = _make_template(
            c, space["id"], "T", "# {{title}} on {{ date }}\nowner: {{owner}}"
        )
        made = c.post(
            f"/spaces/{space['id']}/pages/from-template",
            json={"template_id": tmpl["id"], "title": "Outage 1"},
            headers=H1,
        ).json()
        assert made["body"].startswith(f"# Outage 1 on {page_templates.today()}")
        # An unknown variable is content, not an error and not a blank: the author
        # may well have meant it literally.
        assert "{{owner}}" in made["body"]


def test_a_page_that_is_not_a_template_is_refused(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        ordinary = _page(c, space["id"], "Ordinary", "x")
        resp = c.post(
            f"/spaces/{space['id']}/pages/from-template",
            json={"template_id": ordinary["id"], "title": "New"},
            headers=H1,
        )
        assert resp.status_code == 422


def test_a_template_from_another_space_is_refused(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        here, elsewhere = _space(c, "A", "A"), _space(c, "B", "B")
        foreign = _make_template(c, elsewhere["id"], "Other", "x")
        resp = c.post(
            f"/spaces/{here['id']}/pages/from-template",
            json={"template_id": foreign["id"], "title": "New"},
            headers=H1,
        )
        assert resp.status_code == 404


# --- the daily note ---------------------------------------------------------


def test_the_daily_note_is_the_same_page_on_a_second_visit(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        first = c.post(f"/spaces/{space['id']}/daily", headers=H1)
        second = c.post(f"/spaces/{space['id']}/daily", headers=H1)
        assert first.status_code == 201  # created
        assert second.status_code == 200  # found
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["title"] == page_templates.today()


def test_revisiting_the_daily_note_writes_nothing_at_all(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        page = c.post(f"/spaces/{space['id']}/daily", headers=H1).json()
        trail = f"/activity?target_kind=page&target_id={page['id']}"
        before = c.get(trail, headers=H1).json()
        c.post(f"/spaces/{space['id']}/daily", headers=H1)
        after = c.get(trail, headers=H1).json()
        # Visiting is a read. Stamping an event per visit would turn a morning
        # habit into audit noise and make the trail lie about when it was created.
        assert len(after) == len(before)


def test_the_daily_note_starts_from_the_spaces_template_when_it_has_one(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        _make_template(
            c,
            space["id"],
            page_templates.DAILY_TEMPLATE_TITLE,
            "# {{date}}\n\n## Focus",
        )
        page = c.post(f"/spaces/{space['id']}/daily", headers=H1).json()
        assert page["body"] == f"# {page_templates.today()}\n\n## Focus"


def test_a_space_with_no_template_still_gets_a_daily_note(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        # The feature must not require setup the operator has not done yet.
        assert c.post(f"/spaces/{space['id']}/daily", headers=H1).json()["body"] == ""


def test_a_page_merely_named_like_the_daily_template_is_not_one(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        space = _space(c)
        # Named right, but never labelled. Two rules for "what is a template" is
        # how drift starts, so the label is the only rule.
        _page(c, space["id"], page_templates.DAILY_TEMPLATE_TITLE, "# not a template")
        assert c.post(f"/spaces/{space['id']}/daily", headers=H1).json()["body"] == ""


def test_today_is_utc(tmp_path):
    # Written down rather than papered over: Athena stores no per-user timezone,
    # and everything else durable here is UTC.
    stamp = dt.datetime(2026, 7, 27, 23, 30, tzinfo=dt.UTC)
    assert page_templates.today(stamp) == "2026-07-27"


# --- MCP reaches the same behaviour through REST ----------------------------


def test_mcp_and_rest_agree_on_the_graph_and_the_mentions(tmp_path):
    """MCP goes through REST — there is no second traversal to drift.

    A separate graph walk or mention scan behind MCP is exactly the drift the
    shared-command rule exists to prevent, and it would be invisible until an
    agent and a browser disagreed about what links to what.
    """
    from athena.mcp.client import AthenaClient

    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        _page(c, space["id"], "Linked", f"see [[{GUIDE}]]")
        _page(c, space["id"], "Prose", f"read the {GUIDE} first")

        client = AthenaClient(client=c)
        rest_graph = c.get(f"/pages/{guide['id']}/graph", headers=H1).json()
        assert client.link_graph("page", guide["id"]) == rest_graph

        rest_mentions = c.get(
            f"/pages/{guide['id']}/unlinked-mentions", headers=H1
        ).json()
        assert client.unlinked_mentions("page", guide["id"]) == rest_mentions


def test_an_agent_can_take_a_suggested_edge_over_mcp(tmp_path):
    from athena.mcp.client import AthenaClient

    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        space = _space(c)
        guide = _page(c, space["id"], GUIDE, "x")
        doc = _page(c, space["id"], "Prose", f"read the {GUIDE} first")

        agent = _agent(c)
        c.headers.update(_bearer(agent))
        client = AthenaClient(client=c)
        updated = client.link_mention("page", doc["id"], "page", guide["id"])
        assert updated["body"] == f"read the [[{GUIDE}]] first"
        # The edge is real, not just textual.
        assert [
            b["id"] for b in c.get(f"/pages/{guide['id']}/backlinks", headers=H1).json()
        ] == [doc["id"]]
