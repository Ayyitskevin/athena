"""Two browsers editing one page — the losing editor's path.

The browser edit form used to carry no precondition, so the second save silently
won and the first author's work vanished with no trace and no notice. It now
carries the page's ETag as rendered, and a save that would land on someone else's
is refused.

What happens next is the contract these tests encode, and each clause is a
promise the wording makes:

  * **nothing is overwritten** — the page still holds the winner's text;
  * **nothing is merged** — Athena does not claim to have resolved something a
    person has to read to resolve;
  * **nothing the author typed is thrown away** — their text is written to their
    own draft, because "it is still in the form" stops being true the moment they
    navigate, and a bare 412 page eats work;
  * **the draft stays marked stale** — it is recorded against the baseline they
    were editing FROM, so the next visit still warns them;
  * both texts are visible at once, and putting theirs back is one click.
"""

import html as html_module
import re

from fastapi.testclient import TestClient
import pytest

from athena.main import create_app
from athena.mentor import page_drafts, pages

H1 = {"X-Athena-Actor": "1"}


def _login(client, email, password="secret"):
    """Sign in and arm the CSRF header for this client (one browser)."""
    client.post("/login", data={"email": email, "password": password})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _etag_field(html: str) -> str:
    """The ETag the form carries, as a browser would send it back.

    An ETag is a quoted string, so Jinja escapes the quotes into entities in the
    attribute. A browser unescapes them before submitting; a test that skipped
    that step would post a mangled tag, quietly take the malformed-precondition
    path, and "pass" while proving nothing.
    """
    match = re.search(r'name="if_match" value="([^"]*)"', html)
    assert match, "the edit form must carry the page's ETag"
    return html_module.unescape(match.group(1))


def _rendered(response) -> str:
    """The page as a reader sees it, for asserting on wording.

    Two normalizations, both about asserting the message rather than its encoding:
    entities are unescaped (Jinja turns an apostrophe into `&#39;`, so a raw check
    for "bob\'s rewrite" fails against correct HTML), and whitespace runs collapse
    (a sentence wrapped across source lines is still one sentence to a reader).
    """
    return re.sub(r"\s+", " ", html_module.unescape(response.text))


@pytest.fixture()
def app(tmp_path):
    return create_app(tmp_path / "conflict.db")


@pytest.fixture()
def seeded(app):
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
        space = c.post("/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1).json()
        page = c.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Runbook", "body": "original"},
            headers=H1,
        ).json()
        return {"space": space, "page": page}


def test_the_second_save_is_refused_and_loses_nothing(app, seeded):
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")

        # Both open the editor against the same version of the page.
        ann_form = ann.get(f"/mentor/pages/{page_id}/edit").text
        bob_form = bob.get(f"/mentor/pages/{page_id}/edit").text
        assert _etag_field(ann_form) == _etag_field(bob_form)
        stale_tag = _etag_field(bob_form)

        # Ann saves first and wins.
        won = ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "ann's rewrite",
                "if_match": _etag_field(ann_form),
            },
            follow_redirects=False,
        )
        assert won.status_code == 303

        # Bob saves second, against a tag that no longer describes the page.
        lost = bob.post(
            f"/mentor/pages/{page_id}/edit",
            data={"title": "Runbook", "body": "bob's rewrite", "if_match": stale_tag},
        )
        assert lost.status_code == 409

        # Nothing was overwritten: the page is still Ann's.
        current = bob.get(f"/pages/{page_id}", headers=H1).json()
        assert current["body"] == "ann's rewrite"
        # Nothing was merged.
        assert "bob's rewrite" not in current["body"]

        # Bob sees both: theirs in the form, his own beside it.
        assert "ann's rewrite" in _rendered(lost)  # the form holds the winner
        assert "bob's rewrite" in _rendered(lost)  # his own, shown as "not saved"
        # The wording is part of the contract: it must claim exactly what happened.
        shown = _rendered(lost)
        assert "Someone else saved this page while you were editing" in shown
        assert "nothing was overwritten and nothing was merged" in shown
        # ...and the way back to his own text is one click.
        assert f"/mentor/pages/{page_id}/edit?restore=1" in lost.text


def test_the_losing_text_survives_navigation_as_a_draft(app, seeded):
    # WHY: the failure mode of a bare 412 page. "Your work is still in the form"
    # stops being true the moment the author navigates away, so the text has to
    # live somewhere durable — the draft store that already exists for this.
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        bob_form = bob.get(f"/mentor/pages/{page_id}/edit").text
        stale_tag = _etag_field(bob_form)
        ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "ann's rewrite",
                "if_match": _etag_field(ann.get(f"/mentor/pages/{page_id}/edit").text),
            },
        )
        bob.post(
            f"/mentor/pages/{page_id}/edit",
            data={"title": "Runbook", "body": "bob's rewrite", "if_match": stale_tag},
        )

        # Bob navigates away entirely and comes back.
        bob.get("/mentor")
        reopened = bob.get(f"/mentor/pages/{page_id}/edit")
        assert "You have unsaved work on this page" in reopened.text
        # And it is still marked STALE — recorded against the baseline he was
        # editing from, not the page's new tag, so the warning survives.
        assert "saved by someone else since you last typed" in reopened.text

        restored = bob.get(f"/mentor/pages/{page_id}/edit?restore=1")
        assert "bob's rewrite" in _rendered(restored)


def test_the_draft_belongs_to_the_losing_author_alone(app, seeded):
    # WHY: drafts are personal state. A conflict must not publish one editor's
    # unsaved text into another's editor.
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        stale_tag = _etag_field(bob.get(f"/mentor/pages/{page_id}/edit").text)
        ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "ann's rewrite",
                "if_match": _etag_field(ann.get(f"/mentor/pages/{page_id}/edit").text),
            },
        )
        bob.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "bob's secret draft",
                "if_match": stale_tag,
            },
        )
        assert "bob's secret draft" not in _rendered(
            ann.get(f"/mentor/pages/{page_id}/edit")
        )


def test_an_uncontested_save_still_just_works(app, seeded):
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        form = ann.get(f"/mentor/pages/{page_id}/edit").text
        saved = ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "quiet edit",
                "if_match": _etag_field(form),
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert ann.get(f"/pages/{page_id}", headers=H1).json()["body"] == "quiet edit"
        # A clean save discards the author's draft, as it always did.
        with TestClient(app) as probe:
            _login(probe, "ann@e.com")
            assert (
                "You have unsaved work"
                not in probe.get(f"/mentor/pages/{page_id}/edit").text
            )


def test_saving_twice_from_one_editor_is_not_a_conflict(app, seeded):
    # WHY: the precondition must track the page, not the session. After a save the
    # author's next form carries the new tag, so consecutive edits are fine —
    # otherwise every second save would be refused.
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        for body in ("first", "second", "third"):
            form = ann.get(f"/mentor/pages/{page_id}/edit").text
            r = ann.post(
                f"/mentor/pages/{page_id}/edit",
                data={"title": "Runbook", "body": body, "if_match": _etag_field(form)},
                follow_redirects=False,
            )
            assert r.status_code == 303, body
        assert ann.get(f"/pages/{page_id}", headers=H1).json()["body"] == "third"


def test_restoring_a_draft_can_still_be_saved(app, seeded):
    # WHY: the trap this design had to avoid. `based_on` follows the editing
    # session so stale work stays marked stale; if the precondition reused it, a
    # restored draft could never be saved at all.
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        stale_tag = _etag_field(bob.get(f"/mentor/pages/{page_id}/edit").text)
        ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "ann's rewrite",
                "if_match": _etag_field(ann.get(f"/mentor/pages/{page_id}/edit").text),
            },
        )
        bob.post(
            f"/mentor/pages/{page_id}/edit",
            data={"title": "Runbook", "body": "bob's rewrite", "if_match": stale_tag},
        )
        restored = bob.get(f"/mentor/pages/{page_id}/edit?restore=1")
        assert "bob's rewrite" in _rendered(restored)
        # The form still carries TODAY's tag, so Bob can deliberately save over Ann.
        saved = bob.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "bob's rewrite",
                "if_match": _etag_field(restored.text),
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert (
            bob.get(f"/pages/{page_id}", headers=H1).json()["body"] == "bob's rewrite"
        )


def test_a_form_from_before_this_field_existed_still_saves(app, seeded, tmp_path):
    # WHY: a tab left open across the upgrade sends no if_match. Refusing it would
    # block an author out of their own page over a field they cannot see or fix,
    # so an absent precondition keeps the old behavior.
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        saved = ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={"title": "Runbook", "body": "from an old tab"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert (
            ann.get(f"/pages/{page_id}", headers=H1).json()["body"] == "from an old tab"
        )


def test_a_garbage_precondition_does_not_lock_the_author_out(app, seeded):
    # WHY: the hidden field is a concurrency aid, not an authorization check. A
    # mangled value must not become a wall between an author and their own page.
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann:
        _login(ann, "ann@e.com")
        saved = ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={"title": "Runbook", "body": "still saved", "if_match": "not-an-etag"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert ann.get(f"/pages/{page_id}", headers=H1).json()["body"] == "still saved"


def test_a_page_deleted_mid_conflict_reports_gone_not_conflict(app, seeded):
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        stale_tag = _etag_field(bob.get(f"/mentor/pages/{page_id}/edit").text)
        ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "ann's rewrite",
                "if_match": _etag_field(ann.get(f"/mentor/pages/{page_id}/edit").text),
            },
        )
        ann.delete(f"/pages/{page_id}", headers=H1)
        gone = bob.post(
            f"/mentor/pages/{page_id}/edit",
            data={"title": "Runbook", "body": "bob's rewrite", "if_match": stale_tag},
        )
        assert gone.status_code == 404
        assert pages.get_page(_conn(app), page_id) is None


def _conn(app):
    from athena.core import db

    return db.connect(app.state.db_path)


def test_the_draft_row_records_the_baseline_not_the_new_tag(app, seeded):
    # WHY: the single most load-bearing line in the conflict path, asserted at the
    # row rather than through the rendered warning.
    page_id = seeded["page"]["id"]
    with TestClient(app) as ann, TestClient(app) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        stale_tag = _etag_field(bob.get(f"/mentor/pages/{page_id}/edit").text)
        ann.post(
            f"/mentor/pages/{page_id}/edit",
            data={
                "title": "Runbook",
                "body": "ann's rewrite",
                "if_match": _etag_field(ann.get(f"/mentor/pages/{page_id}/edit").text),
            },
        )
        bob.post(
            f"/mentor/pages/{page_id}/edit",
            data={"title": "Runbook", "body": "bob's rewrite", "if_match": stale_tag},
        )
        conn = _conn(app)
        draft = page_drafts.get_draft(conn, page_id=page_id, owner_id=2)
        assert draft is not None
        assert draft["body"] == "bob's rewrite"
        assert draft["based_on"] == stale_tag
