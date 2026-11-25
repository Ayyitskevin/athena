"""A long board column pages (U13).

WHY (COCKPIT-15): with 226 issues the board was 41,477 px tall. Every card
of every column was laid out at once, and nothing paged.

A column now shows its first twenty cards and puts the rest behind "Show N
more". Every card is still in the page, so a drag and the keyboard move
work on all of them, and the column's count is still its whole count.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from support import ADMIN, bootstrap, login, make_app

from athena.web import boards


def _column(body: str, lane: str, status: str) -> str:
    start = body.index(f'data-lane="{lane}"')
    lane_markup = body[start : body.index("</section>", start)]
    begin = lane_markup.index(f'data-status="{status}"')
    following = lane_markup.find('<div class="board-column"', begin)
    return lane_markup[begin : following if following != -1 else len(lane_markup)]


def _ids(markup: str) -> list[int]:
    return [int(found) for found in re.findall(r'data-issue-id="(\d+)"', markup)]


def _board(tmp_path, counts: dict[str, int]):
    """A signed-in browser on a board with ``counts[status]`` unassigned issues."""
    app, _db = make_app(tmp_path)
    client = TestClient(app)
    client.__enter__()
    bootstrap(client)
    made: dict[str, list[int]] = {}
    for status, count in counts.items():
        made[status] = []
        for number in range(count):
            issue = client.post(
                "/issues", json={"title": f"{status} {number}"}, headers=ADMIN
            ).json()
            if status != "open":
                client.patch(
                    f"/issues/{issue['id']}", json={"status": status}, headers=ADMIN
                )
            made[status].append(issue["id"])
    browser = TestClient(app)
    login(browser, "a@e.com")
    return client, browser, made


def test_a_long_column_shows_a_page_and_keeps_the_rest_behind_a_disclosure(tmp_path):
    assert boards.COLUMN_PAGE == 20
    client, browser, made = _board(tmp_path, {"open": 25, "in_progress": 20})
    try:
        body = browser.get("/aegis/boards").text
    finally:
        client.__exit__(None, None, None)

    long_column = _column(body, "unassigned", "open")
    first, rest = long_column.split('<details class="board-more">')
    assert _ids(first) == made["open"][:20]
    assert "<summary>Show 5 more</summary>" in rest
    assert _ids(rest) == made["open"][20:]
    # The count is the column's, not the page's.
    assert '<span class="count">25</span>' in first
    # Every card, shown or not, can be dragged and moved by keyboard.
    assert long_column.count('draggable="true"') == 25
    assert long_column.count('<form class="board-move"') == 25

    # Exactly a page: nothing to disclose.
    full_column = _column(body, "unassigned", "in_progress")
    assert _ids(full_column) == made["in_progress"]
    assert "board-more" not in full_column
    assert body.count('<details class="board-more">') == 1


def test_an_empty_column_still_says_so(tmp_path):
    client, browser, _made = _board(tmp_path, {"open": 1})
    try:
        body = browser.get("/aegis/boards").text
    finally:
        client.__exit__(None, None, None)

    assert "No issues" in _column(body, "unassigned", "in_progress")
    assert "board-more" not in body
