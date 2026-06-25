"""The cross-link resolver — Athena's one-roof payoff (issue<->page references).

These tests encode the contract that matters, not just that functions run:

  * a body's [[issue:N]]/[[page:N]] tokens are EXTRACTED distinctly and in order;
  * those tokens are INDEXED into the `links` table on every write, and a write
    is the single source of truth — re-saving a body replaces its links exactly,
    so a removed reference disappears and a self-reference is never recorded;
  * "what references THIS?" (backlinks) is the marquee query, and it resolves a
    target LAZILY — a reference to a not-yet-created thing is stored and lights up
    its backlinks the moment that thing exists (the forward-reference promise);
  * the REST backlinks endpoints are open reads that 404 on a missing subject;
  * the web detail pages render live references as links and broken ones visibly,
    and they escape author text (no XSS through a body).
"""
from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import db, links
from athena.main import create_app
from athena.mentor import pages, spaces
from athena.web.render import render_body


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(conn):
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()


def _space(conn):
    return spaces.create_space(conn, key="ENG", name="Eng", created_by=1)


# --- unit: extract_refs -----------------------------------------------------


def test_extract_refs_distinct_in_first_seen_order():
    # WHY: a body that names [[page:7]] twice and [[issue:42]] once must record
    # two links, not three — the index is a set of references, deduped, but order
    # is preserved so renders/links are stable.
    text = "see [[page:7]] and [[issue:42]], also [[page:7]] again"
    assert links.extract_refs(text) == [("page", 7), ("issue", 42)]


def test_extract_refs_ignores_unknown_kinds_and_nonnumeric():
    # WHY: the grammar is strict — only known kinds with integer ids count. Garbage
    # like [[wiki:1]] or [[issue:abc]] is left as literal text, never indexed.
    assert links.extract_refs("[[wiki:1]] [[issue:abc]] [[page:]]") == []


def test_extract_refs_handles_empty_and_none():
    assert links.extract_refs("") == []
    assert links.extract_refs(None) == []


# --- unit: sync on write (the index follows the body) -----------------------


def test_create_issue_indexes_its_references(tmp_path):
    conn = _migrated_conn(tmp_path / "ci.db")
    _seed_user(conn)
    src = issues.create_issue(conn, title="src", body="ref [[page:1]]", created_by=1)
    rows = conn.execute(
        "SELECT target_kind, target_id FROM links WHERE source_kind='issue' "
        "AND source_id=?",
        (src["id"],),
    ).fetchall()
    assert [(r["target_kind"], r["target_id"]) for r in rows] == [("page", 1)]


def test_create_page_indexes_its_references(tmp_path):
    conn = _migrated_conn(tmp_path / "cp.db")
    _seed_user(conn)
    sp = _space(conn)
    pg = pages.create_page(
        conn, space_id=sp["id"], title="p", body="see [[issue:9]]", created_by=1
    )
    rows = conn.execute(
        "SELECT target_kind, target_id FROM links WHERE source_kind='page' "
        "AND source_id=?",
        (pg["id"],),
    ).fetchall()
    assert [(r["target_kind"], r["target_id"]) for r in rows] == [("issue", 9)]


def test_editing_a_body_replaces_its_links(tmp_path):
    # WHY: the body is the single source of truth. Removing a reference in an edit
    # must delete its link row — diffing/append would leave a stale phantom link.
    conn = _migrated_conn(tmp_path / "edit.db")
    _seed_user(conn)
    src = issues.create_issue(
        conn, title="s", body="[[page:1]] [[page:2]]", created_by=1
    )
    issues.update_issue(conn, src["id"], body="now only [[page:2]]")
    rows = conn.execute(
        "SELECT target_id FROM links WHERE source_kind='issue' AND source_id=?",
        (src["id"],),
    ).fetchall()
    assert [r["target_id"] for r in rows] == [2]


def test_title_only_edit_leaves_links_untouched(tmp_path):
    # WHY: only the body carries tokens — a status/title edit must not trigger a
    # re-sync (and must not wipe the existing links by syncing an absent body).
    conn = _migrated_conn(tmp_path / "title.db")
    _seed_user(conn)
    src = issues.create_issue(conn, title="s", body="[[page:5]]", created_by=1)
    issues.update_issue(conn, src["id"], title="renamed")
    rows = conn.execute(
        "SELECT target_id FROM links WHERE source_id=? AND source_kind='issue'",
        (src["id"],),
    ).fetchall()
    assert [r["target_id"] for r in rows] == [5]


def test_self_reference_is_not_indexed(tmp_path):
    # WHY: a thing linking to itself is noise, not a cross-reference. The id isn't
    # known until after insert, so create can't write it; the first edit naming
    # itself must be dropped.
    conn = _migrated_conn(tmp_path / "self.db")
    _seed_user(conn)
    src = issues.create_issue(conn, title="s", body="placeholder", created_by=1)
    issues.update_issue(conn, src["id"], body=f"I am [[issue:{src['id']}]]")
    rows = conn.execute(
        "SELECT * FROM links WHERE source_id=? AND source_kind='issue'",
        (src["id"],),
    ).fetchall()
    assert rows == []


# --- unit: backlinks + lazy resolution --------------------------------------


def test_backlinks_resolve_title_and_existence(tmp_path):
    conn = _migrated_conn(tmp_path / "bl.db")
    _seed_user(conn)
    target = issues.create_issue(conn, title="the target", body="", created_by=1)
    src = issues.create_issue(
        conn, title="s", body=f"see [[issue:{target['id']}]]", created_by=1
    )
    refs = links.backlinks(conn, "issue", target["id"])
    assert len(refs) == 1
    assert refs[0]["kind"] == "issue"
    assert refs[0]["id"] == src["id"]
    assert refs[0]["title"] == "s"
    assert refs[0]["exists"] is True


def test_forward_reference_lights_up_when_target_is_created(tmp_path):
    # WHY: the marquee promise — you can reference a page before it exists, and the
    # backlink appears the moment it's created. Existence is resolved at READ time,
    # never frozen at write time.
    conn = _migrated_conn(tmp_path / "fwd.db")
    _seed_user(conn)
    sp = _space(conn)
    # An issue references page #1 before any page exists.
    src = issues.create_issue(conn, title="s", body="[[page:1]]", created_by=1)
    # Nothing to resolve yet: outgoing link is present but broken.
    out = links.outgoing_links(conn, "issue", src["id"])
    assert out[0]["exists"] is False and out[0]["title"] is None
    # Create the page that was referenced — its backlinks now include the issue.
    pg = pages.create_page(conn, space_id=sp["id"], title="p1", created_by=1)
    assert pg["id"] == 1
    back = links.backlinks(conn, "page", 1)
    assert [b["id"] for b in back] == [src["id"]]


def test_outgoing_links_ordered_and_resolved(tmp_path):
    conn = _migrated_conn(tmp_path / "out.db")
    _seed_user(conn)
    t1 = issues.create_issue(conn, title="one", body="", created_by=1)
    t2 = issues.create_issue(conn, title="two", body="", created_by=1)
    src = issues.create_issue(
        conn,
        title="s",
        body=f"[[issue:{t2['id']}]] and [[issue:{t1['id']}]]",
        created_by=1,
    )
    out = links.outgoing_links(conn, "issue", src["id"])
    # Ordered by (kind, id) for deterministic render, regardless of body order.
    assert [o["id"] for o in out] == sorted([t1["id"], t2["id"]])
    assert all(o["exists"] for o in out)


# --- API: backlinks endpoints -----------------------------------------------


def _seed_api_user(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    conn.close()


def test_issue_backlinks_endpoint(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "api.db")
        h = {"X-Athena-Actor": "1"}
        target = client.post("/issues", json={"title": "target"}, headers=h).json()
        src = client.post(
            "/issues",
            json={"title": "s", "body": f"[[issue:{target['id']}]]"},
            headers=h,
        ).json()
        r = client.get(f"/issues/{target['id']}/backlinks")
        assert r.status_code == 200
        assert [b["id"] for b in r.json()] == [src["id"]]


def test_issue_backlinks_404_for_missing_issue(tmp_path):
    app = create_app(tmp_path / "api404.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "api404.db")
        assert client.get("/issues/999/backlinks").status_code == 404


def test_page_backlinks_endpoint(tmp_path):
    app = create_app(tmp_path / "pgapi.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "pgapi.db")
        h = {"X-Athena-Actor": "1"}
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=h).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "p"}, headers=h
        ).json()
        # An issue that references the page.
        issue = client.post(
            "/issues",
            json={"title": "s", "body": f"[[page:{pg['id']}]]"},
            headers=h,
        ).json()
        r = client.get(f"/pages/{pg['id']}/backlinks")
        assert r.status_code == 200
        body = r.json()
        assert body[0]["kind"] == "issue" and body[0]["id"] == issue["id"]


def test_page_backlinks_404_for_missing_page(tmp_path):
    app = create_app(tmp_path / "pg404.db")
    with TestClient(app) as client:
        _seed_api_user(tmp_path / "pg404.db")
        assert client.get("/pages/999/backlinks").status_code == 404


# --- unit: inline render (presentation, XSS-safe) ---------------------------


def test_render_body_links_live_reference(tmp_path):
    conn = _migrated_conn(tmp_path / "r1.db")
    _seed_user(conn)
    target = issues.create_issue(conn, title="Real Target", body="", created_by=1)
    html = str(render_body(conn, f"see [[issue:{target['id']}]]"))
    assert f'href="/aegis/issues/{target["id"]}"' in html
    assert 'class="xref"' in html
    assert "Real Target" in html


def test_render_body_marks_broken_reference(tmp_path):
    conn = _migrated_conn(tmp_path / "r2.db")
    _seed_user(conn)
    html = str(render_body(conn, "dangling [[page:404]]"))
    assert "xref broken" in html
    assert "[[page:404]]" in html  # shown literally, not dropped


def test_render_body_escapes_author_html(tmp_path):
    # WHY: the body is author-supplied — a <script> in it must be escaped, never
    # emitted as live markup. This is the one safe path for body rendering.
    conn = _migrated_conn(tmp_path / "r3.db")
    _seed_user(conn)
    html = str(render_body(conn, "<script>alert(1)</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --- web: detail pages render references ------------------------------------


def _web_login(client):
    client.post(
        "/users",
        json={"email": "a@e.com", "name": "A", "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": "a@e.com", "password": "secret"})


def test_issue_detail_renders_inline_link_and_backlinks(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        _web_login(client)
        h = {"X-Athena-Actor": "1"}
        target = client.post("/issues", json={"title": "Target"}, headers=h).json()
        src = client.post(
            "/issues",
            json={"title": "Source", "body": f"links [[issue:{target['id']}]]"},
            headers=h,
        ).json()
        # The source page renders the reference as an inline xref link.
        src_html = client.get(f"/aegis/issues/{src['id']}").text
        assert f'href="/aegis/issues/{target["id"]}"' in src_html
        assert "xref" in src_html
        # The target page shows the source under "Referenced by".
        target_html = client.get(f"/aegis/issues/{target['id']}").text
        assert "Referenced by" in target_html
        assert f"/aegis/issues/{src['id']}" in target_html


def test_page_detail_renders_backlinks(tmp_path):
    app = create_app(tmp_path / "webpg.db")
    with TestClient(app) as client:
        _web_login(client)
        h = {"X-Athena-Actor": "1"}
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=h).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Doc"}, headers=h
        ).json()
        issue = client.post(
            "/issues",
            json={"title": "Issue", "body": f"[[page:{pg['id']}]]"},
            headers=h,
        ).json()
        html = client.get(f"/mentor/pages/{pg['id']}").text
        assert "Referenced by" in html
        assert f"/aegis/issues/{issue['id']}" in html
