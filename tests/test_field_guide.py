"""The Field Guide — the workspace documents itself, as ordinary pages.

The contract these encode:

  * the guide is a SPACE, not a special surface: its pages are readable,
    linkable, searchable, and exportable like any others, through the same API;
  * it is seeded through the real commands as a real author, so every page has
    genuine provenance and lands on the activity trail;
  * seeding is idempotent by refusal — a second run never overwrites pages an
    operator may since have edited;
  * the content ships as package data, and the wheel gate pins it, so a guide
    that imports cannot be a guide whose pages are missing from the wheel;
  * the example playbook really is instantiable — F-2 is demonstrable out of the
    box rather than described.
"""

from fastapi.testclient import TestClient
import pytest

from athena import guide
from athena.core import db, links
from athena.main import create_app
from athena.mentor import pages, spaces

H1 = {"X-Athena-Actor": "1"}


def _conn(db_file, *, role="admin"):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('a@e.com', 'A', ?)", (role,)
    )
    conn.commit()
    return conn


# --- content ----------------------------------------------------------------


def test_every_manifest_page_has_content_in_the_package():
    # WHY: the manifest and the files are two things that can drift. If a page is
    # listed but its markdown is not shipped, seeding must fail loudly — this
    # asserts the pairing before any database is involved.
    for filename, title, _ in guide.PAGES:
        body = guide.page_body(filename)
        assert body.strip(), filename
        assert title.strip()
    assert len(guide.PAGES) == 9
    # Titles must be unique: a [[Title]] wikilink resolves only when exactly one
    # live page answers, so two guide pages sharing a title would silently break
    # every cross-reference to either.
    titles = [title for _, title, _ in guide.PAGES]
    assert len(set(titles)) == len(titles)


def test_missing_content_is_a_named_refusal_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(guide, "CONTENT_DIR", tmp_path / "empty")
    with pytest.raises(guide.FieldGuideError) as caught:
        guide.page_body("01_your_desk.md")
    assert "missing from the package" in str(caught.value)


def test_the_wheel_gate_covers_the_guide_content():
    # WHY: package data that nothing pins is package data that a refactor drops.
    # scripts/verify_wheel.py compares the wheel against the source tree for every
    # runtime directory; the guide has to be one of them.
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_wheel.py"
    spec = importlib.util.spec_from_file_location("athena_verify_wheel", script)
    assert spec is not None and spec.loader is not None
    verify_wheel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify_wheel)
    assert "field_guide" in verify_wheel.RUNTIME_DIRS
    shipped = verify_wheel.source_runtime_files()
    for filename, _, _ in guide.PAGES:
        assert f"field_guide/{filename}" in shipped


# --- seeding ----------------------------------------------------------------


def test_seeding_creates_the_space_and_every_page(tmp_path):
    conn = _conn(tmp_path / "g.db")
    result = guide.seed_field_guide(conn, author_id=1)
    assert result["page_count"] == len(guide.PAGES)
    space = spaces.get_space_by_key(conn, guide.GUIDE_SPACE_KEY)
    assert space is not None and space["name"] == guide.GUIDE_SPACE_NAME
    titles = [p["title"] for p in pages.list_pages_in_space(conn, space["id"])]
    assert set(titles) == {title for _, title, _ in guide.PAGES}
    # Authored by a real user through the real commands, so the pages are on the
    # trail rather than injected beneath it.
    events = conn.execute(
        "SELECT COUNT(*) AS n FROM activity WHERE verb = 'page_created'"
    ).fetchone()["n"]
    assert events == len(guide.PAGES)
    assert all(p["created_by"] == 1 for p in result["pages"])


def test_a_second_seed_refuses_and_changes_nothing(tmp_path):
    # WHY: the operator may have edited these pages. Their workspace, their pages
    # — so re-running refuses rather than overwriting, and says how to proceed.
    conn = _conn(tmp_path / "twice.db")
    guide.seed_field_guide(conn, author_id=1)
    space = spaces.get_space_by_key(conn, guide.GUIDE_SPACE_KEY)
    before = pages.list_pages_in_space(conn, space["id"])
    conn.execute(
        "UPDATE pages SET body = 'operator edit' WHERE id = ?", (before[0]["id"],)
    )
    conn.commit()

    with pytest.raises(guide.FieldGuideError) as caught:
        guide.seed_field_guide(conn, author_id=1)
    assert "already exists" in str(caught.value)

    after = pages.list_pages_in_space(conn, space["id"])
    assert [p["id"] for p in after] == [p["id"] for p in before]
    assert pages.get_page(conn, before[0]["id"])["body"] == "operator edit"


def test_the_example_page_carries_the_playbook_label(tmp_path):
    conn = _conn(tmp_path / "label.db")
    result = guide.seed_field_guide(conn, author_id=1)
    example_title = next(title for _, title, is_pb in guide.PAGES if is_pb)
    example = next(p for p in result["pages"] if p["title"] == example_title)
    labelled = conn.execute(
        "SELECT l.name FROM page_labels pl JOIN labels l ON l.id = pl.label_id "
        "WHERE pl.page_id = ?",
        (example["id"],),
    ).fetchall()
    assert [row["name"] for row in labelled] == [guide.PLAYBOOK_LABEL]


def test_an_existing_playbook_label_is_reused_not_duplicated(tmp_path):
    # WHY: a second label with the same name would split every playbook query in
    # half, and the operator's existing one is the real one.
    conn = _conn(tmp_path / "reuse.db")
    conn.execute("INSERT INTO labels (name, color) VALUES ('playbook', '#336699')")
    conn.commit()
    guide.seed_field_guide(conn, author_id=1)
    rows = conn.execute(
        "SELECT id, color FROM labels WHERE name = 'playbook'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["color"] == "#336699"


def test_the_pages_cross_link_by_title_and_the_links_resolve(tmp_path):
    # WHY: the guide is written with [[Title]] wikilinks, which resolve only when
    # exactly one live page answers. If seeding broke that, the manual would ship
    # full of broken references to itself.
    conn = _conn(tmp_path / "links.db")
    result = guide.seed_field_guide(conn, author_id=1)
    by_title = {p["title"]: p["id"] for p in result["pages"]}

    desk = by_title["Your desk"]
    outgoing = links.outgoing_links(conn, "page", desk)
    targets = {row["title"] for row in outgoing if row["exists"]}
    assert {"Searching the workspace", "Watching shared memory"} <= targets

    # ...and the reverse direction is free, which is the point of the link index.
    back = links.backlinks(conn, "page", by_title["Watching shared memory"])
    assert desk in {row["id"] for row in back if row["kind"] == "page"}


# --- over the API -----------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as c:
        c.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "secret"},
            headers=H1,
        )
        yield c


def _seed_over_api(client):
    conn = db.connect(client.app.state.db_path)
    try:
        result = guide.seed_field_guide(conn, author_id=1)
    finally:
        conn.close()
    return result


def test_the_guide_is_an_ordinary_space_over_the_api(client):
    result = _seed_over_api(client)
    space_id = result["space"]["id"]
    listed = client.get(f"/spaces/{space_id}/pages", headers=H1).json()
    assert len(listed) == len(guide.PAGES)

    # Readable page by page, searchable, and exportable — nothing special-cased.
    page = client.get(f"/pages/{result['pages'][0]['id']}", headers=H1).json()
    assert page["title"] == guide.PAGES[0][1] and page["body"]

    hits = client.get("/search", params={"q": "desk"}, headers=H1).json()
    assert any(h["kind"] == "page" for h in hits)

    exported = client.get(f"/mentor/spaces/{space_id}/export.html")
    assert exported.status_code == 200
    assert guide.GUIDE_SPACE_NAME in exported.text


def test_the_example_playbook_actually_starts_work(client):
    # WHY: "F-2 is demonstrable out of the box" has to mean it runs, not that a
    # page describes it running.
    result = _seed_over_api(client)
    example_title = next(title for _, title, is_pb in guide.PAGES if is_pb)
    example = next(p for p in result["pages"] if p["title"] == example_title)

    started = client.post(f"/pages/{example['id']}/start-playbook", json={}, headers=H1)
    assert started.status_code == 201, started.text
    body = started.json()
    # Five checklist lines, one of them ticked: four children, one skipped.
    assert len(body["children"]) == 4
    assert body["checked_skipped"] == 1
    # And the issues cite the page, so the page shows the work it started.
    backlinks = client.get(f"/pages/{example['id']}/backlinks", headers=H1).json()
    linked = {row["id"] for row in backlinks if row["kind"] == "issue"}
    assert body["parent"]["id"] in linked
    assert all(child["id"] in linked for child in body["children"])
