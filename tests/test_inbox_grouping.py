"""The inbox groups its rows by what they are about, in words (U8).

WHY (COCKPIT-8): after about fifteen agent actions the inbox held 34 unread
rows, one per notification in arrival order, each a raw verb and a target
number (``Sol claim_completed issue #12``), with no filter and no grouping.

Now rows sit under the issue or page they are about, headed by its key and
title; each says its verb in words; and the page filters by kind.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
import pytest

from support import (
    ADMIN,
    add_user,
    as_user,
    bearer,
    bootstrap,
    login,
    make_app,
    onboard_agent,
)

from athena.core import db
from athena.web import inbox as inbox_view

BOB = as_user(2)


def _groups(body: str) -> list[tuple[str, list[str]]]:
    """Each group's heading text and its rows' text, in page order."""
    found = []
    for panel in re.findall(r'<section class="panel">(.*?)</section>', body, re.DOTALL):
        heading = re.search(r"<h2>\s*<a [^>]*>(.*?)</a>", panel, re.DOTALL)
        assert heading is not None
        rows = re.findall(r"<li class=[^>]*>\s*<span>(.*?)</span>", panel, re.DOTALL)
        found.append(
            (
                " ".join(re.sub(r"<[^>]+>", " ", heading.group(1)).split()),
                [" ".join(row.split()) for row in rows],
            )
        )
    return found


def _tabs(body: str) -> dict[str, bool]:
    """Each filter tab's text, and whether it is the active one."""
    return {
        " ".join(text.split()): "active" in classes
        for classes, text in re.findall(
            r'class="search-scope-tab([^"]*)">([^<]+)</a>', body
        )
    }


@pytest.fixture
def inbox(tmp_path):
    """Ann's inbox after Bob worked on two of her issues and one of her pages."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        add_user(client, "b@e.com", name="Bob")
        project = client.post(
            "/projects", json={"name": "P", "key": "PRJ"}, headers=ADMIN
        ).json()

        def issue(title: str) -> int:
            return client.post(
                "/issues",
                json={"title": title, "project_id": project["id"]},
                headers=ADMIN,
            ).json()["id"]

        first, second = issue("Ship the importer"), issue("Fix the exporter")
        space = client.post(
            "/spaces", json={"name": "Docs", "key": "DOC"}, headers=ADMIN
        ).json()
        page = client.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Runbook", "body": "x"},
            headers=ADMIN,
        ).json()

        def comment(issue_id: int, body: str) -> None:
            made = client.post(
                f"/issues/{issue_id}/comments", json={"body": body}, headers=BOB
            )
            assert made.status_code == 201, made.text

        comment(first, "one")
        comment(second, "two")
        made = client.post(
            f"/pages/{page['id']}/comments", json={"body": "three"}, headers=BOB
        )
        assert made.status_code == 201, made.text
        comment(first, "four")
        browser = TestClient(app)
        login(browser, "a@e.com")
        yield (
            browser,
            client,
            {"first": first, "second": second, "page": page["id"]},
            db_file,
        )


def test_rows_sit_under_what_they_are_about_headed_by_its_key_and_title(inbox):
    browser, _client, ids, _db = inbox

    body = browser.get("/inbox").text

    # Newest group first; inside a group, newest row first.
    assert _groups(body) == [
        ("PRJ-1 Ship the importer", ["Bob commented on this issue"] * 2),
        ("Runbook", ["Bob commented on this page"]),
        ("PRJ-2 Fix the exporter", ["Bob commented on this issue"]),
    ]
    assert f'href="/aegis/issues/{ids["first"]}"' in body
    assert f'href="/mentor/pages/{ids["page"]}"' in body
    # No raw verb and no bare target number.
    assert "page_commented" not in body
    assert f"issue #{ids['first']}" not in body
    assert body.count("2 unread") == 1 and body.count("1 unread") == 2


def test_a_read_row_stops_counting_and_keeps_its_place(inbox):
    browser, _client, _ids, _db = inbox
    body = browser.get("/inbox").text
    newest = re.search(r'action="/inbox/(\d+)/read"', body)
    assert newest is not None

    browser.post(f"/inbox/{newest.group(1)}/read")

    after = browser.get("/inbox").text
    assert [heading for heading, _rows in _groups(after)][0] == (
        "PRJ-1 Ship the importer"
    )
    assert after.count("1 unread") == 3 and "2 unread" not in after

    # A group with nothing unread carries no count at all.
    browser.post("/inbox/read-all")
    read = browser.get("/inbox").text
    assert len(_groups(read)) == 3 and "unread</span>" not in read


def test_a_claim_is_said_in_words_without_its_generation(inbox):
    browser, client, ids, _db = inbox
    sol = onboard_agent(client)
    client.post(
        f"/issues/{ids['second']}/contributors",
        json={"user_id": sol["user"]["id"]},
        headers=ADMIN,
    )
    etag = client.get(
        f"/issues/{ids['second']}/work-context", headers=bearer(sol)
    ).json()["issue_etag"]
    claimed = client.post(
        f"/issues/{ids['second']}/claim",
        json={},
        headers={**bearer(sol), "If-Match": etag},
    )
    assert claimed.status_code == 201, claimed.text

    body = browser.get("/inbox?kind=claims").text

    assert _groups(body) == [("PRJ-2 Fix the exporter", ["Sol claimed this issue"])]
    assert "generation" not in body and claimed.json()["generation"] not in body


def test_the_page_filters_by_kind_and_says_how_many_of_each(inbox):
    browser, client, ids, _db = inbox
    # Ann lets Bob work on her issue; he moves it, and files one for her.
    client.post(
        f"/issues/{ids['second']}/contributors", json={"user_id": 2}, headers=ADMIN
    )
    moved = client.patch(
        f"/issues/{ids['second']}", json={"status": "in_progress"}, headers=BOB
    )
    assert moved.status_code == 200, moved.text
    filed = client.post(
        "/issues",
        json={"title": "Review the importer", "project_id": 1, "assignee_id": 1},
        headers=BOB,
    )
    assert filed.status_code == 201, filed.text

    everything = browser.get("/inbox").text
    comments = browser.get("/inbox?kind=comments").text
    asks = browser.get("/inbox?kind=asks").text

    assert _tabs(everything) == {
        "All 6": True,
        "Asked of you 1": False,
        "Completions 0": False,
        "Comments 4": False,
        "Claims 0": False,
        "Changes 1": False,
    }
    assert _tabs(comments)["Comments 4"] and not _tabs(comments)["All 6"]
    assert sum(len(rows) for _heading, rows in _groups(comments)) == 4
    assert _groups(asks) == [
        (
            "PRJ-3 Review the importer",
            ["Bob changed who is assigned to this issue — Ann"],
        ),
    ]
    changed = _groups(browser.get("/inbox?kind=changes").text)
    assert changed[0][1][0].startswith("Bob changed the status of this issue — ")

    # A kind with nothing says so, and a value that is no kind shows everything.
    none = browser.get("/inbox?kind=claims").text
    assert _groups(none) == [] and "Nothing of this kind in your inbox." in none
    assert _tabs(none)["Claims 0"]
    assert _groups(browser.get("/inbox?kind=nonsense").text) == _groups(everything)


def test_every_verb_has_a_kind_and_words_and_a_new_one_reads_as_itself():
    kinds = dict(inbox_view.KINDS)
    assert list(kinds) == ["asks", "completions", "comments", "claims", "changes"]
    for verb in ("delegated", "review_requested", "approval_requested"):
        assert inbox_view.kind_of(verb) == "asks"
    assert inbox_view.kind_of("claim_completed") == "completions"
    assert inbox_view.kind_of("lease_lapsed") == "claims"
    assert inbox_view.kind_of("page_edited") == "changes"
    assert inbox_view.kind_of("a_verb_added_tomorrow") == "changes"
    assert inbox_view.words("claim_completed") == "completed a claim on"
    assert inbox_view.words("a_verb_added_tomorrow") == "a verb added tomorrow"
    # Every listed kind is one the page offers.
    assert set(inbox_view._KIND.values()) <= set(kinds)
    assert inbox_view.kind_filter("asks") == "asks"
    assert inbox_view.kind_filter("nonsense") is None
    assert inbox_view.kind_filter(None) is None


def test_a_target_that_no_longer_exists_is_headed_by_its_number(inbox):
    _browser, _client, ids, db_file = inbox
    rows = [
        {
            "verb": "commented",
            "target_kind": "issue",
            "target_id": 9_999,
            "href": "/aegis/issues/9999",
            "read_at": "2026-01-01 00:00:00",
        },
        {
            "verb": "commented",
            "target_kind": "issue",
            "target_id": ids["first"],
            "href": "x",
            "read_at": None,
        },
    ]
    conn = db.connect(db_file)
    try:
        gone, kept = inbox_view.grouped(conn, rows)
        assert inbox_view.grouped(conn, []) == []
    finally:
        conn.close()

    assert (gone["key"], gone["title"], gone["unread"]) == (None, None, 0)
    assert (kept["key"], kept["title"], kept["unread"]) == (
        "PRJ-1",
        "Ship the importer",
        1,
    )
    assert kept["rows"][0]["words"] == "commented on"


def test_a_lease_events_machine_record_is_not_shown_in_the_row():
    record = "generation 95a369bf41b1683d221a09ebe87fbdf2; until 2026-11-25 01:57:48"
    assert inbox_view.detail_text({"verb": "claimed", "detail": record}) is None
    assert inbox_view.detail_text({"verb": "commented", "detail": ""}) is None
    assert inbox_view.detail_text({"verb": "commented"}) is None
    assert (
        inbox_view.detail_text({"verb": "changed_status", "detail": "open → done"})
        == "open → done"
    )
    assert inbox_view.kind_of("claimed") == "claims"
    assert inbox_view.words("lease_renewed") == "renewed the claim on"
