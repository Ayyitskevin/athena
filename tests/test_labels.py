"""Tests for issue labels — shared vocabulary attached many-to-many to issues.

A label (name + color) is created once and reused across issues; the pairing
lives in the issue_labels join table. These tests encode the contract that
matters:

  * labels are SHARED — created by any signed-in actor (like comments), names
    are case-insensitively unique, and re-creating one is a 409 not a duplicate;
  * attaching/detaching is a WRITE on the issue, so it obeys the same
    creator-or-assignee gate as status/priority/assignment (bystander → 403);
  * attaching is idempotent and composes onto issue reads without an N+1;
  * the web layer find-or-creates by name (no separate "create label" step) and
    renders chips on list/board/detail — but still owns no data of its own.
"""
import sqlite3

from fastapi.testclient import TestClient

from athena.aegis import labels
from athena.core import db
from athena.main import create_app


def _migrated_conn(db_file):
    """A connection to a freshly-migrated DB, for unit tests that touch the
    data-access layer directly (create_app only migrates inside its lifespan)."""
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_three_users(db_file):
    # creator=1, assignee=2, bystander=3 — the standard authz cast.
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('ann@e.com', 'Ann')")
    conn.execute("INSERT INTO users (email, name) VALUES ('bob@e.com', 'Bob')")
    conn.execute("INSERT INTO users (email, name) VALUES ('cy@e.com', 'Cy')")
    conn.commit()
    conn.close()


def _get_issue(client, issue_id) -> dict:
    """Read one issue back via the list endpoint (there is no single-GET API
    route; the list is the canonical read path)."""
    for issue in client.get("/issues").json():
        if issue["id"] == issue_id:
            return issue
    raise AssertionError(f"issue {issue_id} not found")


def _make_issue(client, actor="1", **body) -> dict:
    payload = {"title": "t", **body}
    r = client.post("/issues", json=payload, headers={"X-Athena-Actor": actor})
    assert r.status_code == 201, r.text
    return r.json()


def _create_label(client, name, actor="1", color="#6b7280") -> dict:
    r = client.post(
        "/labels", json={"name": name, "color": color},
        headers={"X-Athena-Actor": actor},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- unit: data access ----------------------------------------------------


def test_attach_is_idempotent(tmp_path):
    # WHY: re-attaching the same label must not raise or duplicate — callers
    # shouldn't have to check first (the composite PK + OR IGNORE absorb it).
    db_file = tmp_path / "u1.db"
    conn = _migrated_conn(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute(
        "INSERT INTO issues (title, created_by) VALUES ('i', 1)"
    )
    conn.commit()
    lab = labels.create_label(conn, name="bug")
    labels.add_label_to_issue(conn, 1, lab["id"])
    labels.add_label_to_issue(conn, 1, lab["id"])  # again
    assert [l["name"] for l in labels.labels_for_issue(conn, 1)] == ["bug"]
    conn.close()


def test_get_or_create_is_case_insensitive(tmp_path):
    # WHY: "Bug" and "bug" must be the same label so the shared vocabulary
    # doesn't fragment by capitalization (name is UNIQUE COLLATE NOCASE).
    db_file = tmp_path / "u2.db"
    conn = _migrated_conn(db_file)
    first = labels.get_or_create_label(conn, name="Bug")
    again = labels.get_or_create_label(conn, name="bug")
    assert first["id"] == again["id"]
    assert len(labels.list_labels(conn)) == 1
    conn.close()


def test_labels_for_issues_bulk_groups_by_issue(tmp_path):
    # WHY: list/board views fetch labels for many issues at once; this single
    # query must return them keyed by issue, with label-less issues simply
    # absent (caller defaults to []).
    db_file = tmp_path / "u3.db"
    conn = _migrated_conn(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO issues (title, created_by) VALUES ('i1', 1)")
    conn.execute("INSERT INTO issues (title, created_by) VALUES ('i2', 1)")
    conn.execute("INSERT INTO issues (title, created_by) VALUES ('i3', 1)")
    conn.commit()
    bug = labels.create_label(conn, name="bug")
    feat = labels.create_label(conn, name="feature")
    labels.add_label_to_issue(conn, 1, bug["id"])
    labels.add_label_to_issue(conn, 1, feat["id"])
    labels.add_label_to_issue(conn, 2, bug["id"])
    out = labels.labels_for_issues(conn, [1, 2, 3])
    assert {l["name"] for l in out[1]} == {"bug", "feature"}
    assert {l["name"] for l in out[2]} == {"bug"}
    assert 3 not in out  # i3 has no labels
    assert labels.labels_for_issues(conn, []) == {}
    conn.close()


def test_remove_reports_whether_it_removed(tmp_path):
    # WHY: the API uses the bool return to 404 a detach of a label that isn't
    # on the issue, vs. a successful detach.
    db_file = tmp_path / "u4.db"
    conn = _migrated_conn(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO issues (title, created_by) VALUES ('i', 1)")
    conn.commit()
    lab = labels.create_label(conn, name="bug")
    labels.add_label_to_issue(conn, 1, lab["id"])
    assert labels.remove_label_from_issue(conn, 1, lab["id"]) is True
    assert labels.remove_label_from_issue(conn, 1, lab["id"]) is False  # gone now
    conn.close()


def test_deleting_issue_cascades_join_rows(tmp_path):
    # WHY: the join FK is ON DELETE CASCADE (and foreign_keys is ON per-conn);
    # deleting an issue must not orphan its label pairings.
    db_file = tmp_path / "u5.db"
    conn = _migrated_conn(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO issues (title, created_by) VALUES ('i', 1)")
    conn.commit()
    lab = labels.create_label(conn, name="bug")
    labels.add_label_to_issue(conn, 1, lab["id"])
    conn.execute("DELETE FROM issues WHERE id = 1")
    conn.commit()
    rows = conn.execute("SELECT COUNT(*) AS n FROM issue_labels").fetchone()
    assert rows["n"] == 0
    # the label itself survives — it's shared vocabulary, not owned by the issue
    assert labels.get_label(conn, lab["id"]) is not None
    conn.close()


# --- API: label vocabulary ------------------------------------------------


def test_create_and_list_labels(tmp_path):
    db_file = tmp_path / "a1.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        _create_label(client, "feature")
        _create_label(client, "bug")
        names = [l["name"] for l in client.get("/labels").json()]
        assert names == ["bug", "feature"]  # alphabetical


def test_create_duplicate_label_is_409(tmp_path):
    # WHY: the shared vocabulary has one entry per name; a second "bug" is a
    # conflict, not a silent duplicate.
    db_file = tmp_path / "a2.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        _create_label(client, "bug")
        r = client.post(
            "/labels", json={"name": "BUG"}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 409


def test_create_empty_label_name_is_422(tmp_path):
    db_file = tmp_path / "a3.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        r = client.post(
            "/labels", json={"name": "   "}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 422


def test_create_label_rejects_css_color_injection(tmp_path):
    # WHY: label colors are rendered into inline styles. Only a strict hex color
    # may cross the API boundary; arbitrary CSS text must never be persisted.
    db_file = tmp_path / "label_color.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        r = client.post(
            "/labels",
            json={"name": "p0", "color": "red; position: fixed; inset: 0;"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422
        assert client.get("/labels").json() == []


def test_label_color_migration_sanitizes_existing_rows(tmp_path):
    # WHY: older installs could already have unsafe color text persisted before
    # validation existed. The forward migration must make those rows safe too.
    db_file = tmp_path / "label_color_migration.db"
    conn = db.connect(db_file)
    conn.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE labels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, color TEXT NOT NULL DEFAULT '#6b7280')"
    )
    conn.execute(
        "INSERT INTO labels (name, color) VALUES (?, ?)",
        ("bad", "red; position: fixed; inset: 0;"),
    )
    conn.execute(
        "INSERT INTO labels (name, color) VALUES (?, ?)",
        ("good", "#ABCDEF"),
    )
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0018_label_color_safety.sql":
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (path.name,))
    conn.commit()

    assert db.migrate(conn) == ["0018_label_color_safety.sql"]
    rows = {
        row["name"]: row["color"]
        for row in conn.execute("SELECT name, color FROM labels").fetchall()
    }
    conn.close()

    assert rows == {"bad": "#6b7280", "good": "#abcdef"}


def test_label_color_is_normalized_to_lowercase_hex(tmp_path):
    db_file = tmp_path / "label_color_lower.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        r = client.post(
            "/labels",
            json={"name": "frontend", "color": "#ABCDEF"},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 201
        assert r.json()["color"] == "#abcdef"


def test_create_label_requires_a_known_actor(tmp_path):
    # WHY: label creation is open to any *signed-in* actor, but not anonymous —
    # an unknown actor is 401, same as elsewhere.
    db_file = tmp_path / "a4.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        r = client.post(
            "/labels", json={"name": "bug"}, headers={"X-Athena-Actor": "999"}
        )
        assert r.status_code == 401


# --- API: attach / detach on an issue -------------------------------------


def test_attach_label_appears_in_issue_reads(tmp_path):
    # WHY: labels compose onto the issue payload (create/get/list) so clients
    # get them without a second call.
    db_file = tmp_path / "b1.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue = _make_issue(client, actor="1")
        lab = _create_label(client, "bug")
        r = client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": lab["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 201, r.text
        assert [l["name"] for l in r.json()["labels"]] == ["bug"]
        # and it shows up in the list view too
        listed = client.get("/issues").json()
        assert [l["name"] for l in listed[0]["labels"]] == ["bug"]


def test_attach_unknown_label_is_422(tmp_path):
    db_file = tmp_path / "b2.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue = _make_issue(client, actor="1")
        r = client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": 999},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_detach_label(tmp_path):
    db_file = tmp_path / "b3.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue = _make_issue(client, actor="1")
        lab = _create_label(client, "bug")
        client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": lab["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        r = client.delete(
            f"/issues/{issue['id']}/labels/{lab['id']}",
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200
        assert r.json()["labels"] == []


def test_detach_label_not_on_issue_is_404(tmp_path):
    db_file = tmp_path / "b4.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue = _make_issue(client, actor="1")
        lab = _create_label(client, "bug")  # exists but not attached
        r = client.delete(
            f"/issues/{issue['id']}/labels/{lab['id']}",
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 404


def test_assignee_can_label(tmp_path):
    # WHY: the gate is creator-OR-assignee. Assign the issue to user 2, who is
    # not the creator, and confirm they can label it.
    db_file = tmp_path / "b5.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue = _make_issue(client, actor="1")
        r = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": 2},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 200, r.text
        lab = _create_label(client, "bug")
        r = client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": lab["id"]},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 201, r.text


# --- API authorization: labeling is a write -------------------------------


def test_bystander_cannot_attach_label(tmp_path):
    # WHY (load-bearing): attaching a label mutates the issue, so a non-creator,
    # non-assignee gets 403 — same gate as status/priority/assign.
    db_file = tmp_path / "c1.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue = _make_issue(client, actor="1")
        lab = _create_label(client, "bug")
        r = client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": lab["id"]},
            headers={"X-Athena-Actor": "3"},  # bystander
        )
        assert r.status_code == 403


def test_bystander_cannot_detach_label(tmp_path):
    db_file = tmp_path / "c2.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_three_users(db_file)
        issue = _make_issue(client, actor="1")
        lab = _create_label(client, "bug")
        client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": lab["id"]},
            headers={"X-Athena-Actor": "1"},
        )
        r = client.delete(
            f"/issues/{issue['id']}/labels/{lab['id']}",
            headers={"X-Athena-Actor": "3"},  # bystander
        )
        assert r.status_code == 403
        # and the label is still attached
        assert _get_issue(client, issue["id"])["labels"][0]["name"] == "bug"


# --- web layer ------------------------------------------------------------


def _login(client, email, name):
    # User creation is authenticated after the first (bootstrap) user; act as
    # user 1, who always exists once the first _login has run.
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    # Browser writes now carry a CSRF token; echo the cookie value in the header
    # the TestClient sends on every later request (see web/csrf.py).
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_add_label_by_name_find_or_creates(tmp_path):
    # WHY: the web user types a name; we find-or-create so there's no separate
    # "create the label first" step. The chip then renders on the detail page.
    db_file = tmp_path / "w1.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator
        issue = _make_issue(client, actor="1")
        r = client.post(
            f"/aegis/issues/{issue['id']}/labels",
            data={"name": "frontend"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # label now exists in the shared vocabulary and is attached
        assert [l["name"] for l in client.get("/labels").json()] == ["frontend"]
        page = client.get(f"/aegis/issues/{issue['id']}").text
        assert "frontend" in page


def test_web_empty_label_name_is_400(tmp_path):
    db_file = tmp_path / "w2.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        issue = _make_issue(client, actor="1")
        r = client.post(
            f"/aegis/issues/{issue['id']}/labels", data={"name": "  "}
        )
        assert r.status_code == 400


def test_web_remove_label(tmp_path):
    db_file = tmp_path / "w3.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        issue = _make_issue(client, actor="1")
        client.post(
            f"/aegis/issues/{issue['id']}/labels", data={"name": "bug"}
        )
        lab = client.get("/labels").json()[0]
        r = client.post(
            f"/aegis/issues/{issue['id']}/labels/{lab['id']}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert _get_issue(client, issue["id"])["labels"] == []


def test_web_bystander_cannot_label(tmp_path):
    # WHY: the same write gate applies in the web layer — a logged-in bystander
    # who is neither creator nor assignee gets 403.
    db_file = tmp_path / "w4.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1 — creator
        _login(client, "bob@e.com", "Bob")  # user 2 — bystander (current session)
        issue = _make_issue(client, actor="1")
        r = client.post(
            f"/aegis/issues/{issue['id']}/labels", data={"name": "bug"}
        )
        assert r.status_code == 403
        assert _get_issue(client, issue["id"])["labels"] == []


def test_web_logged_out_cannot_label(tmp_path):
    db_file = tmp_path / "w5.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        issue = _make_issue(client, actor="1")
        client.post("/logout")
        r = client.post(
            f"/aegis/issues/{issue['id']}/labels", data={"name": "bug"}
        )
        assert r.status_code == 401


# --- web list: filter by label --------------------------------------------


def test_web_list_filters_by_label(tmp_path):
    # WHY: labels are only useful if you can FIND by them. Filtering the list to
    # one label must show issues carrying it and hide the rest — this is the
    # whole point of the label vocabulary.
    db_file = tmp_path / "f1.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        bug_issue = _make_issue(client, actor="1", title="alpha-the-bug")
        other = _make_issue(client, actor="1", title="beta-no-label")
        client.post(
            f"/aegis/issues/{bug_issue['id']}/labels", data={"name": "bug"}
        )
        page = client.get("/aegis/issues?label=bug").text
        assert "alpha-the-bug" in page
        assert "beta-no-label" not in page


def test_web_list_unfiltered_shows_all(tmp_path):
    # WHY (control): without a label filter, both issues appear — so the
    # exclusion above is the filter doing its job, not a rendering quirk.
    db_file = tmp_path / "f2.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        bug_issue = _make_issue(client, actor="1", title="alpha-the-bug")
        _make_issue(client, actor="1", title="beta-no-label")
        client.post(
            f"/aegis/issues/{bug_issue['id']}/labels", data={"name": "bug"}
        )
        page = client.get("/aegis/issues").text
        assert "alpha-the-bug" in page
        assert "beta-no-label" in page


def test_web_list_label_filter_dropdown_lists_labels(tmp_path):
    # WHY: the filter control must be populated from the real vocabulary, so a
    # user can pick an existing label rather than guess its name.
    db_file = tmp_path / "f3.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        issue = _make_issue(client, actor="1", title="x")
        client.post(
            f"/aegis/issues/{issue['id']}/labels", data={"name": "frontend"}
        )
        page = client.get("/aegis/issues").text
        assert 'name="label"' in page  # the filter select exists
        assert '<option value="frontend"' in page  # populated from vocabulary
