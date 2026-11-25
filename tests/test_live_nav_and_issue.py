"""The navigation badge and the issue page refresh themselves (U12).

WHY (COCKPIT-14): only three panels updated live: the dashboard's attention
card, Mission Control's active work and the run controls. The "needs you"
badge in the navigation changed only on the dashboard (the card swaps it as
it refreshes) or on a reload, so an operator reading an issue or a Mentor page
was not told that something new needed them. And the issue page, where an
operator watches one piece of work, showed its claim, run and last event as of
the moment it loaded: an agent could claim, run and finish while the page
still read "unclaimed".

Both now refresh with the same mechanism (``web/live.py``): an element asks a
route for its own markup every ``ATHENA_LIVE_REFRESH_SECONDS`` and swaps
itself, past the same gate the page applies, and says how fresh it is.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
import pytest

from support import (
    ADMIN,
    add_user,
    bearer,
    bootstrap,
    login,
    make_app,
    onboard_agent,
)

from athena import config
from athena.core import db
from athena.web import live

BADGE_POLL = f"/aegis/dashboard?panel={live.NAV_COUNTS}"


@pytest.fixture
def instance(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        yield app, client, db_file


def _badge(text: str) -> str:
    found = re.search(
        r'<span id="intervene-counts"[^>]*>(?:\s*|.*?</span>\s*</span>)',
        text,
        re.DOTALL,
    )
    assert found, "no badge in the answer"
    return found.group(0)


def _needs_you(text: str) -> int:
    found = re.search(r'<span class="nav-count"[^>]*>(\d+)<', text)
    return int(found.group(1)) if found else 0


def _ask_for_approval(client) -> None:
    """An agent asks to close an issue and an approval waits on the operator."""
    agent = onboard_agent(
        client,
        email=f"a{_ask_for_approval.n}@e.com",
        name=f"Agent {_ask_for_approval.n}",
    )
    _ask_for_approval.n += 1
    client.put(
        f"/approvals/policies/{agent['user']['id']}",
        json={"action_kind": "issue.close"},
        headers=ADMIN,
    )
    issue = client.post("/issues", json={"title": "close me"}, headers=ADMIN).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    etag = client.get(f"/issues/{issue['id']}", headers=bearer(agent)).headers["ETag"]
    closing = client.patch(
        f"/issues/{issue['id']}",
        json={"status": "done"},
        headers={**bearer(agent), "If-Match": etag},
    )
    assert closing.status_code == 202, closing.text


_ask_for_approval.n = 1


# --- the navigation badge --------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/aegis/issues", "/mentor", "/inbox", "/admin/agents", "/aegis/dashboard"]
)
def test_every_signed_in_page_arms_the_badge_poll(instance, path):
    _, client, _ = instance
    login(client, "a@e.com")
    badge = _badge(client.get(path).text)
    assert f'hx-get="{BADGE_POLL}"' in badge
    assert f'hx-trigger="every {config.LIVE_REFRESH_SECONDS}s"' in badge
    # outerHTML re-arms the timer: the answer carries the same attributes.
    assert 'hx-swap="outerHTML"' in badge


def test_the_badge_poll_answers_the_badge_alone_with_what_is_true_now(instance):
    _, client, _ = instance
    login(client, "a@e.com")
    page = client.get("/aegis/issues").text
    assert _needs_you(page) == 0

    # Something starts needing the operator after the page loaded...
    _ask_for_approval(client)
    polled = client.get(BADGE_POLL)
    assert polled.status_code == 200
    assert "<html" not in polled.text.lower()
    assert polled.text.strip().startswith('<span id="intervene-counts"')
    # ...and the next poll says so, and arms the one after it.
    assert _needs_you(polled.text) == 1
    assert f'hx-get="{BADGE_POLL}"' in polled.text


def test_a_members_badge_carries_their_unread_count_and_never_the_fleets(instance):
    _, client, _ = instance
    member = add_user(client, "m@e.com", name="Mel")
    _ask_for_approval(client)  # the fleet needs the admin, not the member
    login(client, "m@e.com")
    before = client.get(BADGE_POLL).text
    assert 'class="nav-count"' not in before and "unread" not in before

    issue = client.post("/issues", json={"title": "for Mel"}, headers=ADMIN).json()
    client.post(
        f"/issues/{issue['id']}/comments",
        json={"body": f"look [[user:{member['id']}]]"},
        headers=ADMIN,
    )
    after = client.get(BADGE_POLL).text
    assert "1 unread" in after
    assert 'class="nav-count"' not in after  # the "needs you" number is admin-only
    assert f'hx-get="{BADGE_POLL}"' in after


def test_a_signed_out_or_paused_poll_gets_an_empty_badge_and_stops(instance):
    _, client, db_file = instance
    login(client, "a@e.com")
    _ask_for_approval(client)
    assert _needs_you(client.get(BADGE_POLL).text) == 1

    conn = db.connect(db_file)
    try:
        conn.execute(
            "UPDATE users SET paused_at = '2026-08-15 00:00:00' WHERE email = ?",
            ("a@e.com",),
        )
        conn.commit()
    finally:
        conn.close()
    paused = client.get(BADGE_POLL)
    assert paused.status_code == 200
    # Empty, and with no poll on it: the paused tab stops asking.
    assert (
        paused.text.strip() == '<span id="intervene-counts" class="nav-counts"></span>'
    )

    with TestClient(client.app) as stranger:
        signed_out = stranger.get(BADGE_POLL)
        assert "hx-get" not in signed_out.text
        assert 'class="nav-count"' not in signed_out.text
        page = stranger.get("/aegis/issues")
        assert f"panel={live.NAV_COUNTS}" not in page.text


def test_the_dashboard_cards_swap_keeps_the_badge_polling(instance):
    # The attention card's refresh replaces the badge out of band (U6); the copy
    # it swaps in must carry the poll, or the badge would stop after one swap.
    _, client, _ = instance
    login(client, "a@e.com")
    card = client.get(f"/aegis/dashboard?panel={live.FLEET_ATTENTION}").text
    swapped = re.search(r'<span id="intervene-counts"[^>]*>', card).group(0)
    assert 'hx-swap-oob="true"' in swapped
    assert f'hx-get="{BADGE_POLL}"' in swapped


# --- the issue page ----------------------------------------------------------------


def _supervision(text: str) -> str:
    found = re.search(r'<section id="issue-supervision".*?</section>', text, re.DOTALL)
    assert found, "no supervision panel"
    return found.group(0)


def test_the_issue_page_arms_a_poll_on_its_supervision(instance):
    _, client, _ = instance
    issue = client.post("/issues", json={"title": "watched"}, headers=ADMIN).json()
    login(client, "a@e.com")
    panel = _supervision(client.get(f"/aegis/issues/{issue['id']}").text)
    poll = f"/aegis/issues/{issue['id']}?panel={live.ISSUE_SUPERVISION}"
    assert f'hx-get="{poll}"' in panel
    assert f'hx-trigger="every {config.LIVE_REFRESH_SECONDS}s"' in panel
    assert 'hx-swap="outerHTML"' in panel
    assert "Refreshing every" in panel


def test_a_claim_made_after_the_page_loaded_shows_on_the_next_poll(instance):
    _, client, _ = instance
    agent = onboard_agent(client)
    issue = client.post("/issues", json={"title": "watched"}, headers=ADMIN).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    login(client, "a@e.com")
    loaded = _supervision(client.get(f"/aegis/issues/{issue['id']}").text)
    assert "unclaimed" in loaded

    etag = client.get(f"/issues/{issue['id']}", headers=bearer(agent)).headers["ETag"]
    claimed = client.post(
        f"/issues/{issue['id']}/claim",
        json={},
        headers={**bearer(agent, run="run-7"), "If-Match": etag},
    )
    assert claimed.status_code == 201, claimed.text

    polled = client.get(
        f"/aegis/issues/{issue['id']}?panel={live.ISSUE_SUPERVISION}"
    ).text
    assert "<html" not in polled.lower()
    assert polled.strip().startswith('<section id="issue-supervision"')
    panel = _supervision(polled)
    assert "Sol" in panel and "live lease" in panel
    assert "run-7" in panel
    # The admin's release control comes with it, armed with this possession.
    assert f'value="{claimed.json()["generation"]}"' in panel
    assert f"panel={live.ISSUE_SUPERVISION}" in panel


def test_the_supervision_poll_is_gated_exactly_like_the_page(instance):
    _, client, _ = instance
    private = client.post(
        "/projects",
        json={"name": "Hidden", "key": "HID", "visibility": "private"},
        headers=ADMIN,
    ).json()
    hidden = client.post(
        "/issues",
        json={"title": "secret", "project_id": private["id"]},
        headers=ADMIN,
    ).json()
    add_user(client, "m@e.com", name="Mel")
    login(client, "m@e.com")
    for ref in (hidden["id"], 999_999):
        page = client.get(f"/aegis/issues/{ref}")
        poll = client.get(f"/aegis/issues/{ref}?panel={live.ISSUE_SUPERVISION}")
        assert page.status_code == poll.status_code == 404
        assert "secret" not in poll.text
        assert 'id="issue-supervision"' not in poll.text


def test_an_anonymous_reader_gets_the_supervision_without_a_poll(instance):
    # The poll is for the operator watching work. A public issue read without a
    # session renders as before, and arms nothing.
    _, client, _ = instance
    issue = client.post("/issues", json={"title": "public"}, headers=ADMIN).json()
    with TestClient(client.app) as stranger:
        page = stranger.get(f"/aegis/issues/{issue['id']}")
        assert page.status_code == 200
        panel = _supervision(page.text)
        assert "unclaimed" in panel
        assert "hx-get" not in panel and "hx-trigger" not in panel
        assert "Refreshing every" not in panel


def test_polling_off_arms_neither(instance, monkeypatch):
    _, client, _ = instance
    monkeypatch.setattr(config, "LIVE_REFRESH_SECONDS", 0)
    issue = client.post("/issues", json={"title": "watched"}, headers=ADMIN).json()
    login(client, "a@e.com")
    page = client.get(f"/aegis/issues/{issue['id']}").text
    assert 'hx-trigger="every' not in _badge(page)
    panel = _supervision(page)
    assert "hx-trigger" not in panel
    assert "Automatic refresh is off; reload to update." in panel


def test_a_page_rendered_by_a_refused_post_still_polls_the_issue_route(instance):
    # A refused comment re-renders the issue page at the POST address (U11). A
    # poll built from that address would ask a POST route with GET and be
    # refused every ten seconds; it is built from the issue instead.
    _, client, _ = instance
    issue = client.post("/issues", json={"title": "watched"}, headers=ADMIN).json()
    login(client, "a@e.com")
    refused = client.post(f"/aegis/issues/{issue['id']}/comments", data={"body": " "})
    assert refused.status_code == 400
    panel = _supervision(refused.text)
    poll = f"/aegis/issues/{issue['id']}?panel={live.ISSUE_SUPERVISION}"
    assert f'hx-get="{poll}"' in panel
    assert client.get(poll).status_code == 200
