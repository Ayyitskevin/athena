"""U6: what needs the operator is the first thing the operator sees, and it is
one number.

The reviewer (COCKPIT-6) signed in and landed on ``/aegis``, the issue list, a
page with no attention on it; the Intervene badge read 45 while the dashboard's
fleet-attention chip read 11, because the badge added unread inbox rows to the
rollup. Two surfaces naming two different "needs you" totals is the
disagreement principle 10 forbids. The rollup's total
(``docs/EXCEPTION_SURFACES.md``: everything asking for a human) is the number;
``fleet_attention.needs_you`` computes it, the badge and the card both show it,
the card's live refresh carries the badge with it, and the unread inbox is
shown beside it, never added to it.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from athena.aegis import fleet_attention, fleet_work
from athena.core import db, users
from support import (
    ADMIN,
    add_user,
    as_user,
    bootstrap,
    login,
    make_app,
)

BROWSER = {"Accept": "text/html"}
DASHBOARD = "/aegis/dashboard"
PANEL = "/aegis/dashboard?panel=fleet-attention"

_BADGE = re.compile(r'<span class="nav-count"[^>]*>\s*(\d+)\s*</span>')
_CARD = re.compile(
    r'id="fleet-attention-heading">\s*Fleet attention\s*'
    r'<span class="chip" data-tone="accent"[^>]*>\s*(\d+)\s*</span>'
)
_OOB = re.compile(
    r'<span id="intervene-counts"[^>]*hx-swap-oob="true"[^>]*>(.*?)</span>\s*$', re.S
)


def _refuse_a_sign_in(client: TestClient) -> None:
    """A failed sign-in against the admin: one recorded boundary refusal, the
    cheapest real attention signal."""
    client.post(
        "/login",
        data={"email": "a@e.com", "password": "wrong"},
        follow_redirects=False,
    )


def _workspace_where_badge_and_card_differed(client: TestClient) -> None:
    """An admin with one attention signal and unread inbox rows: the workspace
    on which the badge read the rollup plus the inbox and the card the rollup."""
    bootstrap(client)
    add_user(client, "m@e.com", name="Mel")
    _refuse_a_sign_in(client)
    # A member comments on work assigned to the admin: unread inbox rows.
    issue = client.post(
        "/issues", json={"title": "Wire the digest", "assignee_id": 1}, headers=ADMIN
    ).json()
    for n in range(3):
        made = client.post(
            f"/issues/{issue['id']}/comments",
            json={"body": f"note {n}"},
            headers=as_user(2),
        )
        assert made.status_code == 201, made.text
    unread = client.get("/notifications/unread_count", headers=ADMIN).json()["count"]
    assert unread >= 3, "the fixture needs unread inbox rows to tell the two apart"


def test_badge_and_dashboard_show_one_needs_you_number(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        _workspace_where_badge_and_card_differed(client)
        login(client, "a@e.com")
        page = client.get(DASHBOARD, headers=BROWSER).text

        badge = _BADGE.findall(page)
        card = _CARD.findall(page)
        assert len(badge) == 1, "the Intervene badge must show the needs-you number"
        assert len(card) == 1, "the dashboard card must show the needs-you number"
        assert badge == card, (
            f"the badge says {badge[0]} needs you and the dashboard says {card[0]}"
        )

        # ...and that number is the one read model's, not a template sum.
        conn = db.connect(db_file)
        try:
            admin = users.get_user(conn, 1)
            assert admin is not None
            expected = fleet_attention.needs_you(conn, actor=admin)["total"]
        finally:
            conn.close()
        assert int(badge[0]) == expected >= 1


def test_the_attention_card_leads_the_dashboard(tmp_path):
    """Sign-in lands here so that what needs the operator is the first thing
    seen: the card comes before the work totals, not below them."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _workspace_where_badge_and_card_differed(client)
        login(client, "a@e.com")
        page = client.get(DASHBOARD, headers=BROWSER).text
        card = page.index('id="fleet-attention-heading"')
        totals = page.index('class="stat-row"')
        assert card < totals, "the attention card sits below the work totals"


def test_unread_inbox_is_shown_apart_from_the_needs_you_number(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _workspace_where_badge_and_card_differed(client)
        login(client, "a@e.com")
        unread = client.get("/notifications/unread_count", headers=ADMIN).json()[
            "count"
        ]
        page = client.get(DASHBOARD, headers=BROWSER).text
        header = page.split("</header>", 1)[0]
        (badge,) = _BADGE.findall(header)
        (card,) = _CARD.findall(page)
        # The badge is not the rollup plus the inbox...
        assert int(badge) == int(card) != int(card) + unread
        # ...and the inbox keeps its own count beside it, labelled as the inbox's.
        assert f"{unread} unread" in header


def test_both_surfaces_read_the_number_from_one_function(tmp_path, monkeypatch):
    """Not a convention a template can drift from: replace the one function and
    both surfaces move together."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _workspace_where_badge_and_card_differed(client)
        login(client, "a@e.com")
        real = getattr(fleet_attention, "needs_you", None)

        def fixed(conn, **kwargs):
            assert real is not None
            return {**real(conn, **kwargs), "total": 271}

        # raising=False: where no such function exists, nothing reads it, and
        # the assertions below say so.
        monkeypatch.setattr(fleet_attention, "needs_you", fixed, raising=False)
        page = client.get(DASHBOARD, headers=BROWSER).text
        assert _BADGE.findall(page) == ["271"], "the badge does not read needs_you"
        assert _CARD.findall(page) == ["271"], "the card does not read needs_you"


def test_a_live_refresh_moves_the_badge_with_the_card(tmp_path):
    """The card refreshes itself every few seconds; the badge is in the nav,
    outside it. The refresh response carries the badge as an out-of-band swap,
    so a refreshed card never sits beside the page load's number."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _workspace_where_badge_and_card_differed(client)
        login(client, "a@e.com")
        (loaded,) = _CARD.findall(client.get(DASHBOARD, headers=BROWSER).text)

        _refuse_a_sign_in(client)  # a new signal arrives between polls
        login(client, "a@e.com")
        panel = client.get(PANEL, headers={**BROWSER, "HX-Request": "true"}).text

        (card,) = _CARD.findall(panel)
        assert int(card) > int(loaded), "the fixture needs the number to move"
        oob = _OOB.search(panel)
        assert oob is not None, "the refresh does not carry the nav badge"
        assert _BADGE.findall(oob.group(1)) == [card], (
            "the refreshed card and the badge it carries disagree"
        )
        # The full page renders the nav itself, so it carries no second copy.
        page = client.get(DASHBOARD, headers=BROWSER).text
        assert 'hx-swap-oob="true"' not in page


def test_the_rollup_is_read_only_where_it_is_shown(tmp_path, monkeypatch):
    """An admin's cookie rides on every request. The rollup is read for a page
    that renders the nav or for the card's refresh, once, and never for a JSON
    route, a health check or a 404."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _workspace_where_badge_and_card_differed(client)
        login(client, "a@e.com")
        reads: list[int] = []
        real = fleet_work.build_active_work

        def counting(*args, **kwargs):
            if kwargs.get("attention_state") == fleet_work.NEEDS_ATTENTION and (
                kwargs.get("viewer") is None and kwargs.get("limit") is None
            ):
                reads.append(1)  # the rollup's claims count, and only it
            return real(*args, **kwargs)

        monkeypatch.setattr(fleet_work, "build_active_work", counting)
        for path in ("/healthz", "/issues", "/no-such-page", "/users/me"):
            client.get(path)
            assert reads == [], f"{path} built the attention rollup"
        client.get(DASHBOARD, headers=BROWSER)
        assert len(reads) == 1, "the dashboard builds the rollup more than once"
        reads.clear()
        client.get(PANEL, headers={**BROWSER, "HX-Request": "true"})
        assert len(reads) == 1, "the card's refresh builds the rollup more than once"


def test_sign_in_lands_on_the_dashboard_whatever_the_request_carries(tmp_path):
    """Sign-in lands where the attention queue is, and only there: the sign-in
    takes no redirect target, so no field, query or header can move it."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        carried = (
            {},
            {"data": {"next": "https://evil.example/aegis"}},
            {"data": {"next": "/aegis/issues/7"}},
            {"params": {"next": "//evil.example"}},
            {"headers": {"Referer": "https://evil.example/"}},
        )
        for n, extra in enumerate(carried):
            # One account per attempt: the per-account sign-in throttle is not
            # what this test is about.
            email = f"u{n}@e.com"
            add_user(client, email, role="admin")
            data = {"email": email, "password": "pw", **extra.get("data", {})}
            landed = client.post(
                "/login",
                data=data,
                params=extra.get("params"),
                headers=extra.get("headers"),
                follow_redirects=False,
            )
            assert landed.status_code == 303, extra
            assert landed.headers["location"] == DASHBOARD, extra
            page = client.get(landed.headers["location"], headers=BROWSER).text
            assert 'id="fleet-attention-heading"' in page
            # Already signed in, the sign-in page sends you to the same place.
            again = client.get(
                "/login",
                params={"next": "https://evil.example"},
                follow_redirects=False,
            )
            assert again.status_code == 303
            assert again.headers["location"] == DASHBOARD, extra
            client.cookies.clear()
