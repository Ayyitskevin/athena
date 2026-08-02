"""Two browsers editing one issue — the losing editor's path.

The page equivalent shipped first; this is its twin, and it **deliberately
differs in one place**. On a page the winner's text goes into the fields, because
the loser's copy is safe in `page_drafts`. Issues have no draft store, so the
only place the loser's text survives is the form itself — putting the winner's
text there would leave the loser hand-copying out of a `<pre>`, which is lossy
rather than merely inconsistent.

So for issues: the loser's text stays in the fields, the winner's is shown beside
it, and the notice states the part that is genuinely worse than the page path —
this text is not stored anywhere, and navigating away loses it. Saying so is the
contract; a notice that implied durability would be a lie the code cannot back.
"""

import html as html_module
import re

from fastapi.testclient import TestClient
import pytest

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _login(client, email, password="secret"):
    client.post("/login", data={"email": email, "password": password})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _etag_field(text: str) -> str:
    """The ETag as a browser would send it back — entities unescaped, or the tag
    posts mangled, quietly takes the malformed-precondition path, and 'passes'."""
    match = re.search(r'name="if_match" value="([^"]*)"', text)
    assert match, "the issue edit form must carry the issue's ETag"
    return html_module.unescape(match.group(1))


def _rendered(response) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(response.text))


def _body_field(text: str) -> str:
    """Whatever the textarea currently holds."""
    match = re.search(r'name="body"[^>]*>(.*?)</textarea>', text, re.S)
    assert match, "no body textarea"
    return html_module.unescape(match.group(1))


@pytest.fixture()
def app(tmp_path):
    return create_app(tmp_path / "iconflict.db")


@pytest.fixture()
def issue_id(app):
    with TestClient(app) as c:
        c.post(
            "/users",
            json={"email": "ann@e.com", "name": "Ann", "password": "secret"},
            headers=H1,
        )
        c.post(
            "/users",
            json={"email": "bob@e.com", "name": "Bob", "password": "secret"},
            headers=H1,
        )
        project = c.post(
            "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
        ).json()
        # Assigned to Bob. Aegis gates issue writes to creator-or-assignee (unlike
        # Mentor's open wiki model), so the realistic two-editor conflict here is
        # exactly this pair: the person who filed it and the person doing it, both
        # editing the description. Any two users cannot collide on an issue.
        issue = c.post(
            "/issues",
            json={
                "title": "Migrate",
                "body": "original",
                "project_id": project["id"],
            },
            headers=H1,
        ).json()
        assigned = c.put(
            f"/issues/{issue['id']}/assignee", json={"assignee_id": 2}, headers=H1
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["assignee_id"] == 2
        return issue["id"]


def test_the_second_save_is_refused_and_the_page_is_not_overwritten(app, issue_id):
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        ann_form = ann.get(f"/aegis/issues/{issue_id}/edit").text
        bob_form = bob.get(f"/aegis/issues/{issue_id}/edit").text
        assert _etag_field(ann_form) == _etag_field(bob_form)
        stale = _etag_field(bob_form)

        won = ann.post(
            f"/aegis/issues/{issue_id}/edit",
            data={
                "title": "Migrate",
                "body": "ann's plan",
                "if_match": _etag_field(ann_form),
            },
            follow_redirects=False,
        )
        assert won.status_code == 303

        lost = bob.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "Migrate", "body": "bob's plan", "if_match": stale},
        )
        assert lost.status_code == 409
        assert ann.get(f"/issues/{issue_id}", headers=H1).json()["body"] == "ann's plan"


def test_the_losing_text_stays_in_the_form_and_theirs_is_shown_beside_it(app, issue_id):
    # WHY: the inversion from pages, and the reason for it. With no draft store,
    # the fields are the only place the loser's text exists.
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        stale = _etag_field(bob.get(f"/aegis/issues/{issue_id}/edit").text)
        ann.post(
            f"/aegis/issues/{issue_id}/edit",
            data={
                "title": "Migrate",
                "body": "ann's plan",
                "if_match": _etag_field(ann.get(f"/aegis/issues/{issue_id}/edit").text),
            },
        )
        lost = bob.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "Migrate", "body": "bob's plan", "if_match": stale},
        )
        # Bob's text is in the editable field...
        assert _body_field(lost.text) == "bob's plan"
        # ...and Ann's is shown for comparison.
        shown = _rendered(lost)
        assert "ann's plan" in shown
        assert "Someone else saved this issue while you were editing" in shown
        assert "nothing was overwritten and nothing was merged" in shown
        # The honest part: it says the text is NOT stored.
        assert "not saved anywhere" in shown
        assert "leaving this page loses it" in shown


def test_the_refused_form_can_be_saved_again_deliberately(app, issue_id):
    # WHY: the loop this design had to avoid. If the conflict re-render stamped
    # the STALE tag, the author could never save at all.
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        stale = _etag_field(bob.get(f"/aegis/issues/{issue_id}/edit").text)
        ann.post(
            f"/aegis/issues/{issue_id}/edit",
            data={
                "title": "Migrate",
                "body": "ann's plan",
                "if_match": _etag_field(ann.get(f"/aegis/issues/{issue_id}/edit").text),
            },
        )
        lost = bob.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "Migrate", "body": "bob's plan", "if_match": stale},
        )
        saved = bob.post(
            f"/aegis/issues/{issue_id}/edit",
            data={
                "title": "Migrate",
                "body": "bob's plan",
                "if_match": _etag_field(lost.text),
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert ann.get(f"/issues/{issue_id}", headers=H1).json()["body"] == "bob's plan"


def test_an_uncontested_save_still_just_works(app, issue_id):
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        form = ann.get(f"/aegis/issues/{issue_id}/edit").text
        saved = ann.post(
            f"/aegis/issues/{issue_id}/edit",
            data={
                "title": "Migrate",
                "body": "quiet edit",
                "if_match": _etag_field(form),
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert ann.get(f"/issues/{issue_id}", headers=H1).json()["body"] == "quiet edit"


def test_consecutive_saves_from_one_editor_are_not_conflicts(app, issue_id):
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        for body in ("first", "second", "third"):
            form = ann.get(f"/aegis/issues/{issue_id}/edit").text
            r = ann.post(
                f"/aegis/issues/{issue_id}/edit",
                data={"title": "Migrate", "body": body, "if_match": _etag_field(form)},
                follow_redirects=False,
            )
            assert r.status_code == 303, body
        assert ann.get(f"/issues/{issue_id}", headers=H1).json()["body"] == "third"


def test_a_form_from_before_this_field_existed_still_saves(app, issue_id):
    # WHY: a tab open across the upgrade sends no tag. Refusing it would block an
    # author out of their own issue over a field they cannot see.
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        saved = ann.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "Migrate", "body": "from an old tab"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert (
            ann.get(f"/issues/{issue_id}", headers=H1).json()["body"]
            == "from an old tab"
        )


def test_a_garbage_precondition_does_not_lock_the_author_out(app, issue_id):
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        saved = ann.post(
            f"/aegis/issues/{issue_id}/edit",
            data={"title": "Migrate", "body": "still saved", "if_match": "not-an-etag"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert (
            ann.get(f"/issues/{issue_id}", headers=H1).json()["body"] == "still saved"
        )
