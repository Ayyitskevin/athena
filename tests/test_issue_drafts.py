"""Issue drafts — the Aegis twin of page drafts, and the end of an asymmetry.

`EDITING.md` documented the difference plainly: a page conflict could put the
winner's text in the fields because the loser's copy was safe in `page_drafts`,
while an issue conflict had to keep the loser's text in the form and admit it
was not saved anywhere. `issue_drafts` (0074) erases that. These tests pin the
same boundary the page-draft suite pins, on the issue side:

* **a draft is user-private state, not content** — autosave touches neither the
  issue, nor the trail, nor any watcher, and no one but its owner can see it;
* **offered, never applied** — the form shows the saved issue; restore is a
  read; only an explicit save through the audited command makes text content;
* **staleness is visible, never destructive** — a draft that fell behind says
  so, and is never discarded by anything but its owner or a successful save.

The conflict-path flip itself is pinned in `test_issue_edit_conflict.py`.
"""

import html as html_module
import re

from fastapi.testclient import TestClient
import pytest

from athena.aegis import issue_drafts
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _login(client, email, password="secret"):
    client.post("/login", data={"email": email, "password": password})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _rendered(response) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(response.text))


def _body_field(text: str) -> str:
    match = re.search(r'name="body"[^>]*>(.*?)</textarea>', text, re.S)
    assert match, "no body textarea"
    return html_module.unescape(match.group(1))


@pytest.fixture()
def world(tmp_path):
    app = create_app(tmp_path / "idrafts.db")
    with TestClient(app) as c:
        for email, name in (("ann@e.com", "Ann"), ("bob@e.com", "Bob")):
            c.post(
                "/users",
                json={"email": email, "name": name, "password": "secret"},
                headers=H1,
            )
        project = c.post(
            "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
        ).json()
        issue = c.post(
            "/issues",
            json={"title": "Migrate", "body": "original", "project_id": project["id"]},
            headers=H1,
        ).json()
        # Assigned to Bob: issue writes are creator-or-assignee gated, so Ann
        # (creator) and Bob (assignee) are the two people who can draft it.
        c.put(f"/issues/{issue['id']}/assignee", json={"assignee_id": 2}, headers=H1)
        return {"app": app, "db": tmp_path / "idrafts.db", "issue": issue["id"]}


# ---------------------------------------------------------------------------
# A draft is user-private state, not content
# ---------------------------------------------------------------------------


def test_autosave_touches_neither_the_issue_nor_the_trail(world):
    with TestClient(world["app"]) as ann:
        _login(ann, "ann@e.com")
        before_events = ann.get("/activity", headers=H1).json()
        held = ann.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "Migrate", "body": "half a thought"},
        )
        assert held.status_code == 200
        assert "Draft held" in held.text
        # The issue is untouched, and the trail gained nothing.
        assert (
            ann.get(f"/issues/{world['issue']}", headers=H1).json()["body"]
            == "original"
        )
        assert ann.get("/activity", headers=H1).json() == before_events


def test_a_draft_is_invisible_to_everyone_but_its_author(world):
    with TestClient(world["app"]) as ann, TestClient(world["app"]) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        ann.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "Migrate", "body": "ann's private thinking"},
        )
        # Bob — the assignee, a legitimate editor of this issue — never sees it.
        bob_form = bob.get(f"/aegis/issues/{world['issue']}/edit").text
        assert "ann's private thinking" not in bob_form
        assert "You have unsaved work" not in bob_form


def test_two_authors_draft_the_same_issue_without_colliding(world):
    conn = db.connect(world["db"])
    try:
        for owner, text in ((1, "ann's"), (2, "bob's")):
            issue_drafts.save_draft(
                conn,
                issue_id=world["issue"],
                owner_id=owner,
                title="Migrate",
                body=text,
                based_on="",
            )
        assert (
            issue_drafts.get_draft(conn, issue_id=world["issue"], owner_id=1)["body"]
            == "ann's"
        )
        assert (
            issue_drafts.get_draft(conn, issue_id=world["issue"], owner_id=2)["body"]
            == "bob's"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Offered, never applied
# ---------------------------------------------------------------------------


def test_the_form_offers_the_draft_but_still_shows_the_saved_issue(world):
    with TestClient(world["app"]) as ann:
        _login(ann, "ann@e.com")
        ann.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "Migrate", "body": "unsaved work"},
        )
        form = ann.get(f"/aegis/issues/{world['issue']}/edit")
        assert "You have unsaved work on this issue" in _rendered(form)
        # The FIELDS still hold the saved issue — restoring is a decision.
        assert _body_field(form.text) == "original"


def test_restoring_is_a_read_that_writes_nothing(world):
    with TestClient(world["app"]) as ann:
        _login(ann, "ann@e.com")
        ann.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "Migrate", "body": "unsaved work"},
        )
        restored = ann.get(f"/aegis/issues/{world['issue']}/edit?restore=1")
        assert _body_field(restored.text) == "unsaved work"
        assert "holds your draft, not the saved issue" in _rendered(restored)
        # Nothing was written: the issue is unchanged and the draft survives.
        assert (
            ann.get(f"/issues/{world['issue']}", headers=H1).json()["body"]
            == "original"
        )


def test_saving_makes_the_draft_the_issue_and_then_drops_it(world):
    with TestClient(world["app"]) as ann:
        _login(ann, "ann@e.com")
        ann.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "Migrate", "body": "finished thought"},
        )
        form = ann.get(f"/aegis/issues/{world['issue']}/edit?restore=1").text
        etag = html_module.unescape(
            re.search(r'name="if_match" value="([^"]*)"', form).group(1)
        )
        saved = ann.post(
            f"/aegis/issues/{world['issue']}/edit",
            data={"title": "Migrate", "body": "finished thought", "if_match": etag},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert (
            ann.get(f"/issues/{world['issue']}", headers=H1).json()["body"]
            == "finished thought"
        )
        # The draft is gone — the next "unsaved work" banner will mean it.
        assert (
            "You have unsaved work"
            not in ann.get(f"/aegis/issues/{world['issue']}/edit").text
        )


def test_discarding_removes_only_that_authors_copy(world):
    conn = db.connect(world["db"])
    try:
        for owner in (1, 2):
            issue_drafts.save_draft(
                conn,
                issue_id=world["issue"],
                owner_id=owner,
                title="Migrate",
                body=f"draft {owner}",
                based_on="",
            )
    finally:
        conn.close()
    with TestClient(world["app"]) as ann:
        _login(ann, "ann@e.com")
        gone = ann.post(
            f"/aegis/issues/{world['issue']}/draft/discard", follow_redirects=False
        )
        assert gone.status_code == 303
    conn = db.connect(world["db"])
    try:
        assert issue_drafts.get_draft(conn, issue_id=world["issue"], owner_id=1) is None
        assert (
            issue_drafts.get_draft(conn, issue_id=world["issue"], owner_id=2)["body"]
            == "draft 2"
        )
    finally:
        conn.close()


def test_a_draft_matching_the_saved_issue_is_not_offered(world):
    with TestClient(world["app"]) as ann:
        _login(ann, "ann@e.com")
        ann.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "Migrate", "body": "original"},
        )
        assert (
            "You have unsaved work"
            not in ann.get(f"/aegis/issues/{world['issue']}/edit").text
        )


# ---------------------------------------------------------------------------
# Staleness is visible, never destructive
# ---------------------------------------------------------------------------


def test_autosave_after_someone_elses_save_still_flags_stale(world):
    with TestClient(world["app"]) as ann, TestClient(world["app"]) as bob:
        _login(ann, "ann@e.com")
        _login(bob, "bob@e.com")
        # Ann opens the editor; her form carries the CURRENT etag as based_on.
        ann_form = ann.get(f"/aegis/issues/{world['issue']}/edit").text
        based_on = html_module.unescape(
            re.search(r'name="based_on" value="([^"]*)"', ann_form).group(1)
        )
        # Bob saves — the issue moves under Ann.
        bob_form = bob.get(f"/aegis/issues/{world['issue']}/edit").text
        bob_etag = html_module.unescape(
            re.search(r'name="if_match" value="([^"]*)"', bob_form).group(1)
        )
        bob.post(
            f"/aegis/issues/{world['issue']}/edit",
            data={"title": "Migrate", "body": "bob won", "if_match": bob_etag},
        )
        # Ann's autosave carries HER session's baseline, not today's etag —
        # so the draft records the issue she actually saw, and reads as stale.
        ann.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "Migrate", "body": "ann typing on", "based_on": based_on},
        )
        reopened = ann.get(f"/aegis/issues/{world['issue']}/edit")
        assert "saved by someone else since you last typed" in _rendered(reopened)
        # The draft is flagged, not discarded: her work is intact.
        assert "ann typing on" not in _body_field(reopened.text)
        restored = ann.get(f"/aegis/issues/{world['issue']}/edit?restore=1")
        assert _body_field(restored.text) == "ann typing on"


# ---------------------------------------------------------------------------
# Bounds, gates, and lifecycle
# ---------------------------------------------------------------------------


def test_autosave_is_gated_like_the_edit_itself(world):
    app = world["app"]
    with TestClient(app) as c:
        # A third user — neither creator nor assignee — cannot draft the issue.
        c.post(
            "/users",
            json={"email": "cat@e.com", "name": "Cat", "password": "secret"},
            headers=H1,
        )
    with TestClient(app) as cat:
        _login(cat, "cat@e.com")
        refused = cat.post(
            f"/aegis/issues/{world['issue']}/draft",
            data={"title": "x", "body": "y"},
        )
        assert refused.status_code in (403, 404)


def test_the_data_layer_refuses_oversized_drafts(world):
    conn = db.connect(world["db"])
    try:
        with pytest.raises(issue_drafts.DraftTooLarge):
            issue_drafts.save_draft(
                conn,
                issue_id=world["issue"],
                owner_id=1,
                title="x" * (issue_drafts.MAX_TITLE_CHARS + 1),
                body="",
                based_on="",
            )
        with pytest.raises(issue_drafts.DraftTooLarge):
            issue_drafts.save_draft(
                conn,
                issue_id=world["issue"],
                owner_id=1,
                title="t",
                body="x" * (issue_drafts.MAX_BODY_CHARS + 1),
                based_on="",
            )
    finally:
        conn.close()


def test_both_cascades_are_declared(world):
    # ON DELETE CASCADE both ways — no private ghosts. Neither deletion is
    # reachable by raw SQL in a real workspace (users offboard through their
    # command; issues archive rather than hard-delete), so the honest check is
    # the schema's own declaration, the same contract 0071 makes for pages.
    conn = db.connect(world["db"])
    try:
        rules = {
            (row["table"], row["from"]): row["on_delete"]
            for row in conn.execute("PRAGMA foreign_key_list(issue_drafts)")
        }
        assert rules[("issues", "issue_id")] == "CASCADE"
        assert rules[("users", "owner_id")] == "CASCADE"
    finally:
        conn.close()
