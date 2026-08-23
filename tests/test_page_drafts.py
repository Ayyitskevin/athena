"""Stage R-2 — drafts, and the boundary that keeps them from becoming content.

A draft is user-private state, not content. Every test here is that sentence
made checkable: autosave must leave `pages`, `page_versions` and `activity`
completely untouched; a draft must be invisible to everyone but its author,
including admins; and a draft must never turn into a page except through the
ordinary audited save.

The subtle one is staleness. Two people can draft the same page at once and
neither blocks the other, so a draft can fall behind the page it was taken from.
Restoring it blindly would drop whatever landed in between — so the editor says
so, and says it before the author saves rather than after.
"""

from fastapi.testclient import TestClient
import pytest

from athena.core import db
from athena.main import create_app
from athena.mentor import page_drafts

H1 = {"X-Athena-Actor": "1"}
PASSWORD = "pw-long-enough"


def _client(app, email="a@e.com", name="Ann"):
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": PASSWORD})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")
    return client


def _setup(tmp_path, name="d.db"):
    app = create_app(tmp_path / name)
    with TestClient(app) as boot:
        boot.post(
            "/users", json={"email": "a@e.com", "name": "Ann", "password": PASSWORD}
        )
        boot.post(
            "/users",
            json={"email": "v@e.com", "name": "Vic", "password": PASSWORD},
            headers=H1,
        )
        space = boot.post(
            "/spaces", json={"key": "DOC", "name": "Docs"}, headers=H1
        ).json()
        page = boot.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Page", "body": "saved body"},
            headers=H1,
        ).json()
    return app, page, tmp_path / name


def _autosave(client, page_id, body, title="Page"):
    return client.post(
        f"/mentor/pages/{page_id}/draft",
        data={
            "title": title,
            "body": body,
            "csrf_token": client.headers["X-CSRF-Token"],
        },
    )


# ---------------------------------------------------------------------------
# The boundary: a draft is not content.
# ---------------------------------------------------------------------------


def test_autosave_touches_neither_the_page_nor_its_history_nor_the_trail(tmp_path):
    """The entire promise. If autosave wrote a version or an event, an author
    typing for ten minutes would bury the page's real history in keystrokes."""
    app, page, db_path = _setup(tmp_path)
    author = _client(app)

    conn = db.connect(db_path)
    try:
        before_versions = conn.execute(
            "SELECT COUNT(*) AS n FROM page_versions WHERE page_id = ?",
            (page["id"],),
        ).fetchone()["n"]
        before_events = conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()[
            "n"
        ]

        for text in ("typing", "typing more", "typing even more"):
            assert _autosave(author, page["id"], text).status_code == 200

        assert (
            conn.execute(
                "SELECT body FROM pages WHERE id = ?", (page["id"],)
            ).fetchone()["body"]
            == "saved body"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM page_versions WHERE page_id = ?",
                (page["id"],),
            ).fetchone()["n"]
            == before_versions
        )
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM activity").fetchone()["n"]
            == before_events
        )
        # One row per (page, author), not one per keystroke.
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM page_drafts").fetchone()["n"] == 1
        )
    finally:
        conn.close()


def test_a_draft_is_invisible_to_everyone_but_its_author(tmp_path):
    """Not admins, not space members. It is unfinished thinking, and the space's
    visibility rules govern the PAGE rather than one person's scratch copy."""
    app, page, _ = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "my private half-thought")

    other = _client(app, email="v@e.com", name="Vic")
    form = other.get(f"/mentor/pages/{page['id']}/edit").text
    assert "unsaved work" not in form
    assert "my private half-thought" not in form
    # Even asking for the restore view gives them the saved page.
    restored = other.get(f"/mentor/pages/{page['id']}/edit?restore=1").text
    assert "my private half-thought" not in restored
    assert "saved body" in restored


def test_two_authors_draft_the_same_page_without_colliding(tmp_path):
    app, page, db_path = _setup(tmp_path)
    author = _client(app)
    other = _client(app, email="v@e.com", name="Vic")
    _autosave(author, page["id"], "Ann's version")
    _autosave(other, page["id"], "Vic's version")

    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT owner_id, body FROM page_drafts WHERE page_id = ? "
            "ORDER BY owner_id",
            (page["id"],),
        ).fetchall()
        assert [row["body"] for row in rows] == ["Ann's version", "Vic's version"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Offering, restoring, discarding.
# ---------------------------------------------------------------------------


def test_the_form_offers_the_draft_but_still_shows_the_saved_page(tmp_path):
    """A draft is offered, never applied — an editor that silently swapped in
    unsaved text would make an author doubt what they are looking at."""
    app, page, _ = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "unsaved text")

    form = author.get(f"/mentor/pages/{page['id']}/edit").text
    assert "unsaved work" in form
    assert "Restore draft" in form
    # The textarea still carries the SAVED body.
    textarea = form.split("<textarea")[1].split("</textarea>")[0]
    assert "saved body" in textarea
    assert "unsaved text" not in textarea


def test_restoring_is_a_read_that_writes_nothing(tmp_path):
    """Restore re-renders the form with the draft's text. Nothing is written
    until Save, so the button cannot lose anything by being pressed."""
    app, page, db_path = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "unsaved text")

    restored = author.get(f"/mentor/pages/{page['id']}/edit?restore=1").text
    assert "holds your draft" in restored
    textarea = restored.split("<textarea")[1].split("</textarea>")[0]
    assert "unsaved text" in textarea

    conn = db.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT body FROM pages WHERE id = ?", (page["id"],)
            ).fetchone()["body"]
            == "saved body"
        )
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM page_drafts").fetchone()["n"] == 1
        )
    finally:
        conn.close()


def test_saving_makes_the_draft_the_page_and_then_drops_it(tmp_path):
    """Once the text IS the page it has a version row and a real home, so the
    draft is a stale copy. Dropping it is what makes the next "you have unsaved
    work" mean it."""
    app, page, db_path = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "finished text")

    saved = author.post(
        f"/mentor/pages/{page['id']}/edit",
        data={
            "title": "Page",
            "body": "finished text",
            "csrf_token": author.headers["X-CSRF-Token"],
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303

    conn = db.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT body FROM pages WHERE id = ?", (page["id"],)
            ).fetchone()["body"]
            == "finished text"
        )
        # The ordinary save DID cut a version and record an event — the draft
        # never suppressed the audited path, it only stayed out of it.
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM page_versions WHERE page_id = ?",
                (page["id"],),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM activity WHERE verb = 'page_edited'"
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM page_drafts").fetchone()["n"] == 0
        )
    finally:
        conn.close()


def test_discarding_removes_only_that_authors_copy(tmp_path):
    app, page, db_path = _setup(tmp_path)
    author = _client(app)
    other = _client(app, email="v@e.com", name="Vic")
    _autosave(author, page["id"], "Ann's")
    _autosave(other, page["id"], "Vic's")

    dropped = author.post(
        f"/mentor/pages/{page['id']}/draft/discard",
        data={"csrf_token": author.headers["X-CSRF-Token"]},
        follow_redirects=False,
    )
    assert dropped.status_code == 303

    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT body FROM page_drafts").fetchall()
        assert [row["body"] for row in rows] == ["Vic's"]
        # The page itself is untouched by a discard.
        assert (
            conn.execute(
                "SELECT body FROM pages WHERE id = ?", (page["id"],)
            ).fetchone()["body"]
            == "saved body"
        )
    finally:
        conn.close()


def test_a_draft_matching_the_saved_page_is_not_offered(tmp_path):
    """Autosave fires while typing, so a draft can end up identical to the page.
    Offering to restore it would make an author wonder what they had forgotten."""
    app, page, _ = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "saved body")
    assert "unsaved work" not in author.get(f"/mentor/pages/{page['id']}/edit").text


# ---------------------------------------------------------------------------
# Staleness — the case that can lose someone else's work.
# ---------------------------------------------------------------------------


def test_a_draft_left_behind_by_someone_elses_save_says_so(tmp_path):
    app, page, _ = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "my long edit")

    # Someone else saves the page in the meantime.
    with TestClient(app) as api:
        api.patch(f"/pages/{page['id']}", json={"body": "their change"}, headers=H1)

    form = author.get(f"/mentor/pages/{page['id']}/edit").text
    assert "saved by someone else" in form
    assert "their changes will be lost" in form
    # The draft is still offered — it is the author's work, not an error.
    assert "Restore draft" in form


def test_staleness_is_computed_from_the_page_etag(tmp_path):
    app, page, db_path = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "text")

    conn = db.connect(db_path)
    try:
        from athena.mentor import page_etags, pages

        draft = page_drafts.get_draft(conn, page_id=page["id"], owner_id=1)
        assert draft is not None
        live = pages.get_page(conn, page["id"])
        assert page_drafts.is_stale(draft, page_etags.current_etag(conn, live)) is False
        assert page_drafts.is_stale(draft, "some-other-etag") is True
        # A draft with no recorded basis is never called stale — there is
        # nothing to have fallen behind.
        assert page_drafts.is_stale({**draft, "based_on": ""}, "anything") is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bounds and gates.
# ---------------------------------------------------------------------------


def test_autosave_is_bounded_gated_and_csrf_protected(tmp_path):
    app, page, _ = _setup(tmp_path)
    author = _client(app)

    anonymous = TestClient(app)
    assert (
        anonymous.post(
            f"/mentor/pages/{page['id']}/draft", data={"title": "t", "body": "b"}
        ).status_code
        == 401
    )
    assert (
        author.post(
            f"/mentor/pages/{page['id']}/draft",
            data={"title": "t", "body": "b"},
            headers={"X-CSRF-Token": "wrong"},
        ).status_code
        == 403
    )
    assert _autosave(author, page["id"], "x" * 200_001).status_code == 413
    # A page the author cannot see is a 404, exactly like a missing one.
    assert _autosave(author, 99999, "x").status_code == 404


def test_the_data_layer_refuses_oversized_drafts(tmp_path):
    app, page, db_path = _setup(tmp_path)
    conn = db.connect(db_path)
    try:
        with pytest.raises(page_drafts.DraftTooLarge):
            page_drafts.save_draft(
                conn,
                page_id=page["id"],
                owner_id=1,
                title="t",
                body="x" * (page_drafts.MAX_BODY_CHARS + 1),
                based_on="",
            )
        with pytest.raises(page_drafts.DraftTooLarge):
            page_drafts.save_draft(
                conn,
                page_id=page["id"],
                owner_id=1,
                title="x" * (page_drafts.MAX_TITLE_CHARS + 1),
                body="b",
                based_on="",
            )
    finally:
        conn.close()


def test_purging_a_page_takes_its_drafts_with_it(tmp_path):
    """A private ghost row outliving its page would be invisible garbage that
    nothing can reach or clean up."""
    app, page, db_path = _setup(tmp_path)
    author = _client(app)
    _autosave(author, page["id"], "text")

    conn = db.connect(db_path)
    try:
        from athena.mentor import pages

        assert (
            conn.execute("SELECT COUNT(*) AS n FROM page_drafts").fetchone()["n"] == 1
        )
        pages.purge_page(conn, page["id"])
        conn.commit()
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM page_drafts").fetchone()["n"] == 0
        )
    finally:
        conn.close()


def test_autosave_after_someone_elses_save_still_flags_stale(tmp_path):
    # WHY: the draft's baseline is the page the author actually SAW — the form
    # carries it through every autosave. The first implementation re-read the
    # CURRENT etag at autosave time, so the moment someone else saved, the very
    # next autosave marked the draft fresh and the stale warning could never
    # fire again for that session (review finding: silent clobber re-enabled).
    import html as html_lib
    import re

    app, page, _ = _setup(tmp_path)
    author = _client(app)
    form = author.get(f"/mentor/pages/{page['id']}/edit").text
    baseline = html_lib.unescape(
        re.search(r'name="based_on" value="([^"]*)"', form).group(1)
    )
    assert baseline

    # Someone else saves FIRST; the author's autosave timer fires after.
    with TestClient(app) as api:
        api.patch(f"/pages/{page['id']}", json={"body": "their change"}, headers=H1)
    author.post(
        f"/mentor/pages/{page['id']}/draft",
        data={
            "title": "Page",
            "body": "my edit, started before theirs",
            "based_on": baseline,
            "csrf_token": author.headers["X-CSRF-Token"],
        },
    )

    reloaded = author.get(f"/mentor/pages/{page['id']}/edit").text
    assert "saved by someone else" in reloaded
    assert "their changes will be lost" in reloaded


def test_restoring_carries_the_drafts_own_baseline_forward(tmp_path):
    # WHY: restoring continues the editing session that produced the draft, so
    # the re-rendered form must carry the DRAFT's baseline — stamping today's
    # etag there would quietly mark stale work fresh on the next autosave.
    import html as html_lib
    import re

    app, page, db_path = _setup(tmp_path)
    author = _client(app)
    form = author.get(f"/mentor/pages/{page['id']}/edit").text
    baseline = html_lib.unescape(
        re.search(r'name="based_on" value="([^"]*)"', form).group(1)
    )
    author.post(
        f"/mentor/pages/{page['id']}/draft",
        data={
            "title": "Page",
            "body": "session work",
            "based_on": baseline,
            "csrf_token": author.headers["X-CSRF-Token"],
        },
    )
    with TestClient(app) as api:
        api.patch(f"/pages/{page['id']}", json={"body": "their change"}, headers=H1)

    restored = author.get(f"/mentor/pages/{page['id']}/edit?restore=1").text
    carried = html_lib.unescape(
        re.search(r'name="based_on" value="([^"]*)"', restored).group(1)
    )
    assert carried == baseline

    conn = db.connect(db_path)
    try:
        draft = page_drafts.get_draft(conn, page_id=page["id"], owner_id=1)
        assert draft is not None and draft["based_on"] == baseline
    finally:
        conn.close()
