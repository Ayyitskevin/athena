"""Project-scoped issue keys — Jira-style ATH-1, ATH-2, … .

These encode the contract behind the feature, not just HTTP shapes:

  * each project counts its issues from 1 with its own key prefix (ATH-1, ATH-2),
    while a different project counts independently (MISE-1);
  * a key is RETIRED, never reused: moving an issue away frees its number but the
    next issue in that project still gets a fresh higher one — no two issues ever
    share a number, even across deletes/moves;
  * a backlog issue (no project) has no key at all;
  * an issue is addressable two ways — by numeric id and by key (ATH-12) — on both
    the REST API and the web detail page;
  * a project key is required, validated for shape, and unique (409 on a clash);
    editing it re-labels existing issues without renumbering them;
  * a [[ATH-12]] cross-link resolves to that issue (and records a backlink); an
    unresolvable key ref renders broken and records no backlink.
"""

from fastapi.testclient import TestClient

from athena.aegis import issues, projects
from athena.core import db, links
from athena.main import create_app
from athena.web.render import render_body


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(db_file, email="a@e.com", name="A"):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _make_project(client, name, key, actor="1") -> dict:
    r = client.post(
        "/projects",
        json={"name": name, "key": key},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _make_issue(client, actor="1", **body) -> dict:
    r = client.post(
        "/issues", json={"title": "t", **body}, headers={"X-Athena-Actor": actor}
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- unit: per-project numbering ------------------------------------------


def test_keys_count_from_one_per_project(tmp_path):
    # WHY: the number is project-scoped — two projects each start at 1, so the key
    # is only unique together with the project prefix.
    conn = _migrated_conn(tmp_path / "n.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    ath = projects.create_project(conn, name="Athena", key="ATH", created_by=1)
    mise = projects.create_project(conn, name="Mise", key="MISE", created_by=1)
    a1 = issues.create_issue(
        conn, title="t", body="", created_by=1, project_id=ath["id"]
    )
    a2 = issues.create_issue(
        conn, title="t", body="", created_by=1, project_id=ath["id"]
    )
    m1 = issues.create_issue(
        conn, title="t", body="", created_by=1, project_id=mise["id"]
    )
    assert (a1["key"], a2["key"], m1["key"]) == ("ATH-1", "ATH-2", "MISE-1")


def test_backlog_issue_has_no_key(tmp_path):
    # WHY: a key only exists inside a project; an issue with no project has none.
    conn = _migrated_conn(tmp_path / "b.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    issue = issues.create_issue(conn, title="loose", body="", created_by=1)
    assert issue["key"] is None


def test_moving_an_issue_retires_its_number(tmp_path):
    # WHY: the no-reuse guarantee. An issue that leaves a project frees its number,
    # but the counter never rewinds — the next issue gets a fresh higher number, so
    # the freed one is never handed out again.
    conn = _migrated_conn(tmp_path / "m.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    ath = projects.create_project(conn, name="Athena", key="ATH", created_by=1)
    mise = projects.create_project(conn, name="Mise", key="MISE", created_by=1)
    issues.create_issue(conn, title="t", body="", created_by=1, project_id=ath["id"])
    a2 = issues.create_issue(
        conn, title="t", body="", created_by=1, project_id=ath["id"]
    )
    assert a2["key"] == "ATH-2"
    # Move ATH-2 to Mise: it gives up 2 and is handed a fresh MISE number.
    moved = issues.set_project(conn, a2["id"], mise["id"])
    assert moved["key"] == "MISE-1"
    # A new Athena issue must NOT reclaim the retired 2 — it gets 3.
    a3 = issues.create_issue(
        conn, title="t", body="", created_by=1, project_id=ath["id"]
    )
    assert a3["key"] == "ATH-3"
    # And the retired ATH-2 ref no longer resolves to anything.
    assert issues.get_by_ref(conn, "ATH-2") is None


def test_reassigning_same_project_keeps_number(tmp_path):
    # WHY: a no-op move (set to the project it's already in) must not burn a new
    # number — only a real change reallocates.
    conn = _migrated_conn(tmp_path / "s.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    ath = projects.create_project(conn, name="Athena", key="ATH", created_by=1)
    a1 = issues.create_issue(
        conn, title="t", body="", created_by=1, project_id=ath["id"]
    )
    again = issues.set_project(conn, a1["id"], ath["id"])
    assert again["key"] == "ATH-1"


def test_removing_from_project_clears_key(tmp_path):
    conn = _migrated_conn(tmp_path / "r.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    ath = projects.create_project(conn, name="Athena", key="ATH", created_by=1)
    a1 = issues.create_issue(
        conn, title="t", body="", created_by=1, project_id=ath["id"]
    )
    cleared = issues.set_project(conn, a1["id"], None)
    assert cleared["key"] is None


def test_get_by_ref_resolves_id_and_key(tmp_path):
    conn = _migrated_conn(tmp_path / "g.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    ath = projects.create_project(conn, name="Athena", key="ATH", created_by=1)
    a1 = issues.create_issue(
        conn, title="hi", body="", created_by=1, project_id=ath["id"]
    )
    assert issues.get_by_ref(conn, str(a1["id"]))["id"] == a1["id"]
    assert issues.get_by_ref(conn, "ATH-1")["id"] == a1["id"]
    assert issues.get_by_ref(conn, "ath-1")["id"] == a1["id"]  # key is case-insensitive
    assert issues.get_by_ref(conn, "NOPE-1") is None
    assert issues.get_by_ref(conn, "9999") is None
    assert issues.get_by_ref(conn, "9223372036854775808") is None
    assert issues.get_by_ref(conn, "ATH-9223372036854775808") is None
    assert issues.get_by_ref(conn, "9" * 5_000) is None


# --- API: keys on the resource + addressability ---------------------------


def test_issue_out_carries_key(tmp_path):
    app = create_app(tmp_path / "o.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "o.db")
        proj = _make_project(client, "Athena", "ATH")
        issue = _make_issue(client, project_id=proj["id"])
        assert issue["key"] == "ATH-1"


def test_show_is_addressable_by_key_and_id(tmp_path):
    # WHY: the headline feature — GET /issues/ATH-1 and GET /issues/<id> return the
    # same issue. Only this read entry point widens; writes stay numeric.
    app = create_app(tmp_path / "addr.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "addr.db")
        proj = _make_project(client, "Athena", "ATH")
        issue = _make_issue(client, project_id=proj["id"])
        by_id = client.get(f"/issues/{issue['id']}")
        by_key = client.get("/issues/ATH-1")
        assert by_id.status_code == by_key.status_code == 200
        assert by_id.json()["id"] == by_key.json()["id"] == issue["id"]


def test_show_unknown_key_is_404(tmp_path):
    app = create_app(tmp_path / "u404.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "u404.db")
        assert client.get("/issues/ZZZ-1").status_code == 404


def test_create_project_requires_valid_key(tmp_path):
    app = create_app(tmp_path / "vk.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "vk.db")
        # Missing key — Pydantic 422.
        assert (
            client.post(
                "/projects", json={"name": "X"}, headers={"X-Athena-Actor": "1"}
            ).status_code
            == 422
        )
        # Bad shape (leading digit) — 422 from the boundary check.
        assert (
            client.post(
                "/projects",
                json={"name": "X", "key": "1AB"},
                headers={"X-Athena-Actor": "1"},
            ).status_code
            == 422
        )


def test_duplicate_key_is_409(tmp_path):
    # WHY: the key is a unique identity (case-insensitive), like the name.
    app = create_app(tmp_path / "dk.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "dk.db")
        _make_project(client, "Athena", "ATH")
        r = client.post(
            "/projects",
            json={"name": "Other", "key": "ath"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 409


def test_editing_key_relabels_without_renumbering(tmp_path):
    # WHY: the per-project number is independent of the prefix, so swapping the
    # prefix re-labels existing issues (ATH-1 -> OPS-1) but keeps their numbers.
    app = create_app(tmp_path / "ek.db")
    with TestClient(app) as client:
        _seed_user(tmp_path / "ek.db")
        proj = _make_project(client, "Athena", "ATH")
        issue = _make_issue(client, project_id=proj["id"])
        assert issue["key"] == "ATH-1"
        r = client.patch(
            f"/projects/{proj['id']}",
            json={"key": "OPS"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["key"] == "OPS"
        assert client.get(f"/issues/{issue['id']}").json()["key"] == "OPS-1"
        # New key is now the addressable one.
        assert client.get("/issues/OPS-1").json()["id"] == issue["id"]


# --- cross-links ----------------------------------------------------------


def test_key_crosslink_resolves_and_backlinks(tmp_path):
    # WHY: [[ATH-1]] is sugar for "issue 1 in project ATH" — it must render as a
    # live link AND show up as a backlink on the target, like [[issue:N]].
    conn = _migrated_conn(tmp_path / "x.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    ath = projects.create_project(conn, name="Athena", key="ATH", created_by=1)
    target = issues.create_issue(
        conn, title="Target", body="", created_by=1, project_id=ath["id"]
    )
    source = issues.create_issue(
        conn, title="Source", body="see [[ATH-1]]", created_by=1, project_id=ath["id"]
    )
    html = render_body(conn, source["body"])
    assert f'href="/aegis/issues/{target["id"]}"' in str(html)
    assert "Target" in str(html)
    # The link was recorded numerically, so the target lists the source as a backlink.
    back = links.backlinks(conn, "issue", target["id"])
    assert any(b["id"] == source["id"] for b in back)


def test_broken_key_crosslink_renders_broken_without_backlink(tmp_path):
    # WHY: an unresolvable key ref shows broken (so the author sees it dangling) but
    # records no backlink — the documented asymmetry vs a numeric [[issue:N]].
    conn = _migrated_conn(tmp_path / "xb.db")
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    ath = projects.create_project(conn, name="Athena", key="ATH", created_by=1)
    source = issues.create_issue(
        conn,
        title="Source",
        body="ghost [[ATH-99]]",
        created_by=1,
        project_id=ath["id"],
    )
    html = str(render_body(conn, source["body"]))
    assert "xref broken" in html
    assert "[[ATH-99]]" in html
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM links WHERE source_kind='issue' AND source_id=?",
        (source["id"],),
    ).fetchone()
    assert rows["n"] == 0


# --- web ------------------------------------------------------------------


def _login(client, email, name):
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_detail_addressable_by_key(tmp_path):
    app = create_app(tmp_path / "wk.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        client.post("/aegis/projects", data={"name": "Athena", "key": "ATH"})
        proj = client.get("/projects").json()[0]
        _make_issue(client, actor="1", project_id=proj["id"])
        page = client.get("/aegis/issues/ATH-1")
        assert page.status_code == 200
        assert "ATH-1" in page.text


def test_web_create_project_rejects_bad_key(tmp_path):
    app = create_app(tmp_path / "wbk.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        r = client.post("/aegis/projects", data={"name": "Athena", "key": "1bad"})
        assert r.status_code == 400


def test_web_create_project_duplicate_key_is_409(tmp_path):
    app = create_app(tmp_path / "wdk.db")
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        client.post("/aegis/projects", data={"name": "Athena", "key": "ATH"})
        r = client.post("/aegis/projects", data={"name": "Other", "key": "ath"})
        assert r.status_code == 409
