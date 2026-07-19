"""Tests for editing a Mentor page and its version history.

The contract these encode — why each rule earns its place:

  * an edit changes the live page AND files the PRIOR content as a version, so a
    page's past is never lost when its present is overwritten;
  * version numbers are dense and 1-based, oldest content = version 1; the list
    reads newest-first so "what changed last" is on top;
  * each version is attributed to whoever wrote THAT content, not to the editor
    who replaced it — so blame across multiple editors stays honest;
  * a never-edited page has no versions (the live row is its only state);
  * editing is gated on a real actor (anonymous -> 401) against a real page
    (missing -> 404), needs at least one field (-> 422), and refuses a blank
    title (-> 422) — the live page can never be edited into a nameless state.
"""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mentor import pages, spaces


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_two_users(db_file):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES ('ann@e.com', 'Ann')")
    conn.execute("INSERT INTO users (email, name) VALUES ('bob@e.com', 'Bob')")
    conn.commit()
    conn.close()


def _space_with_page(conn, *, title="Runbook", body="v1 text", created_by=1):
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.execute("INSERT INTO users (email, name) VALUES ('b@e.com', 'B')")
    conn.commit()
    space = spaces.create_space(conn, key="ENG", name="Engineering", created_by=created_by)
    page = pages.create_page(
        conn, space_id=space["id"], title=title, body=body, created_by=created_by
    )
    return page


# --- unit: data access ----------------------------------------------------


def test_edit_changes_the_live_page(tmp_path):
    conn = _migrated_conn(tmp_path / "e.db")
    page = _space_with_page(conn)
    updated = pages.update_page(conn, page["id"], editor_id=1, body="v2 text")
    assert updated["body"] == "v2 text"
    assert updated["title"] == "Runbook"  # untouched field stays
    assert pages.get_page(conn, page["id"])["body"] == "v2 text"


def test_edit_snapshots_the_prior_content_as_version_one(tmp_path):
    conn = _migrated_conn(tmp_path / "v1.db")
    page = _space_with_page(conn, title="Runbook", body="v1 text")
    pages.update_page(conn, page["id"], editor_id=1, body="v2 text")
    history = pages.list_page_versions(conn, page["id"])
    assert len(history) == 1
    assert history[0]["version"] == 1
    # The snapshot holds the OLD content, not the new.
    assert history[0]["title"] == "Runbook"
    assert history[0]["body"] == "v1 text"


def test_versions_are_dense_and_newest_first(tmp_path):
    conn = _migrated_conn(tmp_path / "vN.db")
    page = _space_with_page(conn, body="v1")
    pages.update_page(conn, page["id"], editor_id=1, body="v2")
    pages.update_page(conn, page["id"], editor_id=1, body="v3")
    history = pages.list_page_versions(conn, page["id"])
    # Two edits => two superseded revisions (v1 and v2); the live row holds v3.
    assert [h["version"] for h in history] == [2, 1]
    assert [h["body"] for h in history] == ["v2", "v1"]


def test_each_version_keeps_its_own_author(tmp_path):
    # Ann creates (so v1 content is hers); Bob edits (so v2 content is his). The
    # snapshot of the original must stay attributed to Ann even though Bob caused it.
    conn = _migrated_conn(tmp_path / "blame.db")
    page = _space_with_page(conn, created_by=1)  # user 1 = Ann
    pages.update_page(conn, page["id"], editor_id=2, body="bob's rewrite")  # user 2 = Bob
    history = pages.list_page_versions(conn, page["id"])
    assert history[0]["edited_by"] == 1  # the superseded original was Ann's
    live = pages.get_page(conn, page["id"])
    assert live["updated_by"] == 2  # the present revision is Bob's


def test_each_version_keeps_its_authoring_run(tmp_path):
    # Run provenance mirrors authorship: the snapshot of a revision carries the RUN that
    # produced that content (the page's run_id when the snapshot was cut), not the run of
    # the edit that superseded it — so per-version provenance stays honest across runs.
    from athena.core import run_context

    conn = _migrated_conn(tmp_path / "runs.db")
    tok = run_context.set_run_id("run-alpha")
    page = _space_with_page(conn, body="v1")  # created under run-alpha
    run_context.reset_run_id(tok)
    assert pages.get_page(conn, page["id"])["run_id"] == "run-alpha"

    tok2 = run_context.set_run_id("run-beta")
    pages.update_page(conn, page["id"], editor_id=1, body="v2")  # edit under run-beta
    run_context.reset_run_id(tok2)

    history = pages.list_page_versions(conn, page["id"])
    assert history[0]["version"] == 1
    assert history[0]["run_id"] == "run-alpha"  # the superseded content's run, not beta
    assert pages.get_page(conn, page["id"])["run_id"] == "run-beta"  # live row = this edit


def test_human_write_records_no_run(tmp_path):
    # A write with no run context (a human at the keyboard) records run_id = NULL, not an
    # invented one — provenance is honest about the absence of a run.
    conn = _migrated_conn(tmp_path / "human.db")
    page = _space_with_page(conn, body="v1")  # no run context set
    assert pages.get_page(conn, page["id"])["run_id"] is None
    pages.update_page(conn, page["id"], editor_id=1, body="v2")
    assert pages.list_page_versions(conn, page["id"])[0]["run_id"] is None


def test_version_run_id_surfaced_over_rest(tmp_path):
    # The version endpoint exposes run_id so an operator can read provenance without
    # cross-referencing the activity log.
    app = create_app(tmp_path / "restrun.db")
    with TestClient(app) as client:
        conn = db.connect(tmp_path / "restrun.db")
        conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
        conn.commit()
        conn.close()
        h = {"X-Athena-Actor": "1", "X-Athena-Run": "run-rest"}
        sp = client.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=h).json()
        pg = client.post(
            f"/spaces/{sp['id']}/pages", json={"title": "Doc", "body": "v1"}, headers=h
        ).json()
        client.patch(f"/pages/{pg['id']}", json={"body": "v2"}, headers=h)
        versions = client.get(f"/pages/{pg['id']}/versions", headers=h).json()
        assert versions[0]["run_id"] == "run-rest"


def test_no_changing_fields_cuts_no_version(tmp_path):
    conn = _migrated_conn(tmp_path / "noop.db")
    page = _space_with_page(conn)
    same = pages.update_page(conn, page["id"], editor_id=1)
    assert same["id"] == page["id"]
    assert pages.list_page_versions(conn, page["id"]) == []


def test_never_edited_page_has_no_versions(tmp_path):
    conn = _migrated_conn(tmp_path / "fresh.db")
    page = _space_with_page(conn)
    assert pages.list_page_versions(conn, page["id"]) == []


def test_update_unknown_page_returns_none(tmp_path):
    conn = _migrated_conn(tmp_path / "miss.db")
    assert pages.update_page(conn, 999, editor_id=1, body="x") is None


def test_concurrent_edits_do_not_collide_on_version(tmp_path):
    # WHY (load-bearing): two editors saving the same page at the same instant
    # must not both compute the same next version and collide on
    # UNIQUE(page_id, version). BEGIN IMMEDIATE serializes the writers, so the
    # snapshots come out dense (1, 2) with no IntegrityError and no lost history.
    db_file = tmp_path / "race.db"
    setup = _migrated_conn(db_file)
    page = _space_with_page(setup)  # seeds users 1 and 2
    setup.close()

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def edit(editor_id: int, body: str) -> None:
        conn = db.connect(db_file)
        try:
            barrier.wait()  # both threads enter update_page together
            pages.update_page(conn, page["id"], editor_id=editor_id, body=body)
        except Exception as exc:  # noqa: BLE001 — the test asserts there are none
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=edit, args=(1, "from A")),
        threading.Thread(target=edit, args=(2, "from B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []  # neither writer hit a UNIQUE collision
    check = db.connect(db_file)
    history = pages.list_page_versions(check, page["id"])
    check.close()
    # Two edits => exactly two dense snapshots, no duplicate or skipped version.
    assert [h["version"] for h in history] == [2, 1]


def test_failed_update_rolls_back_the_version_snapshot(tmp_path):
    # WHY: the snapshot INSERT and the live UPDATE are one transaction. If the
    # UPDATE fails (here a non-existent editor trips the updated_by foreign key),
    # the snapshot must roll back with it — history can never gain an orphan
    # version for an edit that never actually landed.
    conn = _migrated_conn(tmp_path / "rollback.db")
    page = _space_with_page(conn, body="v1 text")  # users 1 and 2 exist; 999 does not
    with pytest.raises(sqlite3.IntegrityError):
        pages.update_page(conn, page["id"], editor_id=999, body="never lands")
    assert pages.list_page_versions(conn, page["id"]) == []  # no orphan snapshot
    assert pages.get_page(conn, page["id"])["body"] == "v1 text"  # live row untouched


# --- API ------------------------------------------------------------------


def _make_space_and_page(client, db_file, *, body="v1 text"):
    _seed_two_users(db_file)
    space = client.post(
        "/spaces", json={"key": "ENG", "name": "Engineering"},
        headers={"X-Athena-Actor": "1"},
    ).json()
    page = client.post(
        f"/spaces/{space['id']}/pages",
        json={"title": "Runbook", "body": body},
        headers={"X-Athena-Actor": "1"},
    ).json()
    return page


def test_patch_edits_and_records_history_via_api(tmp_path):
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "api.db")
        # Bob (actor 2) edits the body.
        r = client.patch(
            f"/pages/{page['id']}",
            json={"body": "v2 text"},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["body"] == "v2 text"
        assert r.json()["updated_by"] == 2
        # History now exposes the original, attributed to its creator (actor 1).
        hist = client.get(f"/pages/{page['id']}/versions")
        assert hist.status_code == 200
        assert [v["version"] for v in hist.json()] == [1]
        assert hist.json()[0]["body"] == "v1 text"
        assert hist.json()[0]["edited_by"] == 1
        # A single version is addressable on its own URL.
        one = client.get(f"/pages/{page['id']}/versions/1")
        assert one.status_code == 200
        assert one.json()["body"] == "v1 text"


def test_versions_list_empty_before_first_edit(tmp_path):
    app = create_app(tmp_path / "empty.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "empty.db")
        r = client.get(f"/pages/{page['id']}/versions")
        assert r.status_code == 200
        assert r.json() == []


def test_patch_requires_an_actor(tmp_path):
    app = create_app(tmp_path / "auth.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "auth.db")
        r = client.patch(f"/pages/{page['id']}", json={"body": "x"})
        assert r.status_code == 401


def test_patch_unknown_page_is_404(tmp_path):
    app = create_app(tmp_path / "nopage.db")
    with TestClient(app) as client:
        _seed_two_users(tmp_path / "nopage.db")
        r = client.patch(
            "/pages/999", json={"body": "x"}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 404


def test_patch_with_no_fields_is_422(tmp_path):
    app = create_app(tmp_path / "nofield.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "nofield.db")
        r = client.patch(
            f"/pages/{page['id']}", json={}, headers={"X-Athena-Actor": "1"}
        )
        assert r.status_code == 422


def test_patch_blank_title_is_422(tmp_path):
    app = create_app(tmp_path / "blank.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "blank.db")
        r = client.patch(
            f"/pages/{page['id']}",
            json={"title": "   "},
            headers={"X-Athena-Actor": "1"},
        )
        assert r.status_code == 422


def test_unknown_version_is_404(tmp_path):
    app = create_app(tmp_path / "nov.db")
    with TestClient(app) as client:
        page = _make_space_and_page(client, tmp_path / "nov.db")
        r = client.get(f"/pages/{page['id']}/versions/7")
        assert r.status_code == 404
