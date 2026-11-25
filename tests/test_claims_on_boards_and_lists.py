"""Boards and the issue list say who holds the live claim (U2).

WHY (COCKPIT-5): agents work by claim, not by assignment, and the cockpit's
boards and lists keyed on the assignee alone: 16 of 18 issues an agent was
working sat under "Unassigned".

Now a card and a list row name the holder of the live lease beside the
assignee, a card's lane follows whoever is working it (the holder when there
is one, else the assignee), and the board can group its lanes by holder.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest

from support import (
    ADMIN,
    bearer,
    bootstrap,
    lapse_lease,
    login,
    make_app,
    onboard_agent,
)

from athena.aegis import leases
from athena.core import db


def _lane(body: str, key: str) -> str:
    start = body.index(f'data-lane="{key}"')
    return body[start : body.index("</section>", start)]


def _lanes(body: str) -> list[tuple[str, str]]:
    """Each lane's key and heading, in page order."""
    return re.findall(
        r'data-lane="([^"]+)"\s+aria-labelledby="[^"]+">\s*'
        r'<div class="board-swimlane-header">\s*<h2 id="[^"]+">([^<]+)</h2>',
        body,
    )


def _cards(lane: str) -> set[int]:
    return {int(found) for found in re.findall(r'data-issue-id="(\d+)"', lane)}


def _card(body: str, issue_id: int) -> str:
    start = body.index(f'data-issue-id="{issue_id}"')
    return body[start : body.index('<form class="board-move"', start)]


@pytest.fixture
def floor(tmp_path):
    """Five issues: A unassigned and claimed by the agent Sol, B assigned to a
    human, C neither, D assigned to Sol and not claimed, E assigned to a human
    and claimed by Sol."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        sol_id = sol["user"]["id"]
        project = client.post(
            "/projects", json={"name": "P", "key": "PRJ"}, headers=ADMIN
        ).json()

        def issue(title, **fields):
            made = client.post(
                "/issues",
                json={"title": title, "project_id": project["id"], **fields},
                headers=ADMIN,
            )
            assert made.status_code == 201, made.text
            return made.json()["id"]

        ids = {
            "A": issue("A"),
            "B": issue("B", assignee_id=1),
            "C": issue("C"),
            "D": issue("D", assignee_id=sol_id),
            "E": issue("E", assignee_id=1),
        }
        for name in ("A", "E"):
            client.post(
                f"/issues/{ids[name]}/contributors",
                json={"user_id": sol_id},
                headers=ADMIN,
            )
            packet = client.get(
                f"/issues/{ids[name]}/work-context", headers=bearer(sol)
            ).json()
            claimed = client.post(
                f"/issues/{ids[name]}/claim",
                json={},
                headers={**bearer(sol), "If-Match": packet["issue_etag"]},
            )
            assert claimed.status_code == 201, claimed.text
        browser = TestClient(app)
        login(browser, "a@e.com")
        yield browser, ids, sol_id, db_file


def test_a_cards_lane_follows_whoever_is_working_it(floor):
    browser, ids, _sol, _db = floor

    body = browser.get("/aegis/boards").text

    assert _lanes(body) == [
        ("agent", "Agent work"),
        ("human", "Human work"),
        ("unassigned", "Unassigned and unclaimed"),
    ]
    # Claimed by an agent, whoever it is assigned to; or assigned to one.
    assert _cards(_lane(body, "agent")) == {ids["A"], ids["D"], ids["E"]}
    assert _cards(_lane(body, "human")) == {ids["B"]}
    assert _cards(_lane(body, "unassigned")) == {ids["C"]}


def test_a_card_names_the_holder_beside_the_assignee(floor):
    browser, ids, _sol, _db = floor

    body = browser.get("/aegis/boards").text

    claimed = _card(body, ids["A"])
    assert '<span class="name">Unassigned</span>' in claimed
    assert "claimed by Sol</span>" in claimed
    both = _card(body, ids["E"])
    assert '<span class="name">Ann</span>' in both
    assert "claimed by Sol</span>" in both
    for unclaimed in ("B", "C", "D"):
        assert "claimed by" not in _card(body, ids[unclaimed])


def test_the_board_groups_by_claim_holder_when_asked(floor):
    browser, ids, sol_id, _db = floor

    body = browser.get("/aegis/boards?group=claim").text

    assert _lanes(body) == [
        (f"holder-{sol_id}", "Claimed by Sol"),
        ("unclaimed", "No live claim"),
    ]
    assert _cards(_lane(body, f"holder-{sol_id}")) == {ids["A"], ids["E"]}
    assert _cards(_lane(body, "unclaimed")) == {ids["B"], ids["C"], ids["D"]}
    assert '<option value="claim" selected>' in body
    # A value that is not a grouping is the default one.
    assert _lanes(browser.get("/aegis/boards?group=nonsense").text)[0] == (
        "agent",
        "Agent work",
    )


def test_a_claim_by_a_person_is_human_work_and_holder_lanes_go_by_name(floor):
    browser, ids, sol_id, _db = floor
    api = TestClient(browser.app)
    etag = api.get(f"/issues/{ids['C']}/work-context", headers=ADMIN).json()[
        "issue_etag"
    ]
    claimed = api.post(
        f"/issues/{ids['C']}/claim", json={}, headers={**ADMIN, "If-Match": etag}
    )
    assert claimed.status_code == 201, claimed.text

    body = browser.get("/aegis/boards").text
    by_claim = browser.get("/aegis/boards?group=claim").text

    # Unassigned, and a person holds it: human work, not agent work.
    assert ids["C"] in _cards(_lane(body, "human"))
    assert "claimed by Ann</span>" in _card(body, ids["C"])
    assert _cards(_lane(body, "unassigned")) == set()
    # Ann (account 1) before Sol (account 2) by name, not by id or by arrival.
    assert _lanes(by_claim) == [
        ("holder-1", "Claimed by Ann"),
        (f"holder-{sol_id}", "Claimed by Sol"),
        ("unclaimed", "No live claim"),
    ]


def test_a_lapsed_lease_is_not_a_claim(floor):
    browser, ids, sol_id, db_file = floor
    lapse_lease(db_file, ids["A"])

    body = browser.get("/aegis/boards").text
    by_claim = browser.get("/aegis/boards?group=claim").text

    assert "claimed by" not in _card(body, ids["A"])
    assert ids["A"] in _cards(_lane(body, "unassigned"))
    assert _cards(_lane(by_claim, f"holder-{sol_id}")) == {ids["E"]}


def test_a_move_keeps_the_grouping(floor):
    browser, ids, sol_id, _db = floor
    body = browser.get("/aegis/boards?group=claim").text
    form = re.search(
        rf'<form class="board-move"[^>]+action="/aegis/boards/move/{ids["B"]}">'
        r"(.*?)</form>",
        body,
        re.DOTALL,
    )
    assert form is not None
    assert '<input type="hidden" name="group" value="claim">' in form.group(1)
    etag = re.search(r'name="if_match" value="([^"]+)"', form.group(1))
    assert etag is not None
    fields = {
        "new_status": "in_progress",
        "if_match": etag.group(1).replace("&#34;", '"'),
        "group": "claim",
        "csrf_token": browser.cookies.get("athena_csrf"),
    }

    moved = browser.post(
        f"/aegis/boards/move/{ids['B']}", data=fields, follow_redirects=False
    )

    assert moved.status_code == 303, moved.text
    assert parse_qs(urlparse(moved.headers["location"]).query)["group"] == ["claim"]
    swapped = browser.post(
        f"/aegis/boards/move/{ids['C']}",
        data={**fields, "if_match": "stale", "group": "nonsense"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    # A grouping that is not one is dropped, not echoed into the URL.
    assert "group" not in swapped.headers.get("hx-redirect", "")


def test_the_issue_list_has_a_claim_column(floor):
    browser, ids, _sol, _db = floor

    body = browser.get("/aegis/issues").text

    assert "<th>Claim</th>" in body

    def row(issue_id: int) -> str:
        start = body.index(f'href="/aegis/issues/{issue_id}"')
        return body[start : body.index("</tr>", start)]

    assert ">Sol</span>" in row(ids["A"])
    assert '<td class="muted">Unassigned</td>' in row(ids["A"])
    assert ">Sol</span>" in row(ids["E"])
    for unclaimed in ("B", "C", "D"):
        assert '<td class="muted">None</td>' in row(ids[unclaimed])


def test_the_holders_of_many_issues_are_read_in_batches(floor, monkeypatch):
    _browser, ids, sol_id, db_file = floor
    monkeypatch.setattr(leases, "_HOLDER_BATCH", 2)
    conn = db.connect(db_file)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        held = leases.live_holders(conn, list(ids.values()))
        assert leases.live_holders(conn, []) == {}
    finally:
        conn.close()

    assert set(held) == {ids["A"], ids["E"]}
    assert held[ids["A"]] | {"expires_at": ""} == {
        "holder_id": sol_id,
        "holder_name": "Sol",
        "holder_is_agent": True,
        "expires_at": "",
    }
    # Five ids, two a statement: three reads, and none for no ids.
    assert len([s for s in statements if "issue_leases" in s]) == 3
