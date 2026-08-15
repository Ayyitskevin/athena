"""The three cockpit surfaces refresh themselves, and refuse to do it for anyone
who could not read them in the first place.

VISION's Observe promise is that the operator can see what each agent is doing right
now. Three surfaces carry that: the dashboard's fleet-attention card, Mission
Control's active claimed work, and the recorded run controls. Each polls its own
page for its own markup and swaps itself, which means the poll is not a second code
path — it re-enters the route it came from, past the same admin gate, carrying the
same filters. These tests pin that equivalence, because the day it stops being true
is the day a partial endpoint quietly serves fleet state to someone the page would
have refused.
"""

from __future__ import annotations

import re
from html import unescape

from fastapi.testclient import TestClient
import pytest

from athena import config
from athena.main import create_app
from athena.web import live

BROWSER = {"Accept": "text/html"}

#: Every surface: the page, the panel it refreshes, and a string only the panel's
#: own markup contains. Parameterizing these together is deliberate — a fourth
#: surface added later inherits every property below by adding one row.
SURFACES = [
    ("/aegis/dashboard", live.FLEET_ATTENTION, "Fleet attention"),
    ("/admin/agents/runs", live.ACTIVE_WORK, "Active claimed work"),
    ("/admin/run-controls", live.RUN_CONTROLS, "Recorded controls"),
]


def _client(tmp_path, name="live.db"):
    client = TestClient(create_app(tmp_path / name))
    client.__enter__()
    return client


def _admin(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
    client.post(
        "/login", data={"email": "a@e.com", "password": "pw"}, follow_redirects=False
    )


@pytest.mark.parametrize("path,panel,marker", SURFACES)
def test_the_page_arms_a_poll_at_the_configured_interval(tmp_path, path, panel, marker):
    client = _client(tmp_path)
    try:
        _admin(client)
        page = client.get(path, headers=BROWSER).text
        assert marker in page
        assert f'hx-get="{path}?panel={panel}"' in page
        assert f'hx-trigger="every {config.LIVE_REFRESH_SECONDS}s"' in page
        # outerHTML is what re-arms the timer: the swapped-in response carries the
        # same attributes. Any other swap polls exactly once and then stops, which
        # would look like "live" for one interval and be stale forever after.
        assert 'hx-swap="outerHTML"' in page
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("path,panel,marker", SURFACES)
def test_the_panel_returns_itself_and_not_the_page(tmp_path, path, panel, marker):
    client = _client(tmp_path)
    try:
        _admin(client)
        panel_html = client.get(f"{path}?panel={panel}", headers=BROWSER).text
        assert marker in panel_html
        assert "<html" not in panel_html.lower(), "the poll swapped in a whole page"
        # ...and it re-arms itself, so the second refresh happens.
        assert f'hx-trigger="every {config.LIVE_REFRESH_SECONDS}s"' in panel_html
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("path,panel,marker", SURFACES)
def test_the_poll_is_gated_exactly_like_the_page(tmp_path, path, panel, marker):
    """The property the whole design rests on. A panel is reachable only through the
    route that owns it, so a caller refused the page is refused its live data too —
    there is no partial-only endpoint that could be given a weaker gate by
    accident."""
    client = _client(tmp_path)
    try:
        _admin(client)  # an admin exists; the anonymous client below is not it
        anonymous = TestClient(client.app)
        with anonymous:
            page = anonymous.get(path, headers=BROWSER, follow_redirects=False)
            poll = anonymous.get(
                f"{path}?panel={panel}", headers=BROWSER, follow_redirects=False
            )
            # Whatever the page answers a stranger with, the poll answers the same...
            assert poll.status_code == page.status_code
            # ...and neither one hands over the panel's contents.
            assert marker not in page.text
            assert marker not in poll.text
    finally:
        client.__exit__(None, None, None)


def test_a_non_admin_gets_no_attention_card_and_no_poll(tmp_path):
    """The dashboard is readable by anyone; the attention card inside it is not.
    A viewer therefore renders a page with no polling markup at all, and asking for
    the panel directly returns nothing rather than the card."""
    client = _client(tmp_path, "viewer.db")
    try:
        _admin(client)
        admin_page = client.get("/aegis/dashboard", headers=BROWSER).text
        assert "Fleet attention" in admin_page

        viewer = TestClient(client.app)
        with viewer:
            page = viewer.get("/aegis/dashboard", headers=BROWSER)
            assert page.status_code == 200
            assert "Fleet attention" not in page.text
            assert f"panel={live.FLEET_ATTENTION}" not in page.text

            panel = viewer.get(
                f"/aegis/dashboard?panel={live.FLEET_ATTENTION}", headers=BROWSER
            )
            assert panel.status_code == 200
            assert panel.text.strip() == ""
    finally:
        client.__exit__(None, None, None)


def test_a_paused_admin_polls_nothing(tmp_path):
    """Pause is meant to freeze an account without burning its session. If a paused
    admin's open tab kept polling, the lever would leak exactly the fleet state it
    was pulled to cut off."""
    db_file = tmp_path / "paused.db"
    client = _client(tmp_path, "paused.db")
    try:
        _admin(client)
        assert "Fleet attention" in client.get("/aegis/dashboard", headers=BROWSER).text

        from athena.core import db

        conn = db.connect(db_file)
        conn.execute(
            "UPDATE users SET paused_at = ? WHERE email = ?",
            ("2026-08-15 00:00:00", "a@e.com"),
        )
        conn.commit()
        conn.close()

        # The session cookie is untouched; the middleware treats the user as signed
        # out, so the card and its poll are simply not there.
        page = client.get("/aegis/dashboard", headers=BROWSER)
        assert "Fleet attention" not in page.text
        assert f"panel={live.FLEET_ATTENTION}" not in page.text
        panel = client.get(
            f"/aegis/dashboard?panel={live.FLEET_ATTENTION}", headers=BROWSER
        )
        assert panel.text.strip() == ""
    finally:
        client.__exit__(None, None, None)


def test_a_refresh_keeps_the_filter_the_operator_chose(tmp_path):
    """A poll that dropped the query string would silently widen a filtered view to
    the whole fleet — the operator would be watching something other than what they
    asked for, with nothing on screen saying so."""
    client = _client(tmp_path, "filter.db")
    try:
        _admin(client)
        page = client.get(
            "/admin/agents/runs?attention_state=needs_attention", headers=BROWSER
        ).text
        # Jinja escapes the separator, so the attribute reads `&amp;` — correct
        # markup that a browser decodes before it parses the URL. Unescape it here
        # for the same reason, rather than "fixing" the template into emitting a raw
        # `&` that would be invalid HTML.
        url = unescape(re.search(r'hx-get="([^"]+)"', page).group(1))
        assert "attention_state=needs_attention" in url
        assert f"panel={live.ACTIVE_WORK}" in url

        refreshed = client.get(url, headers=BROWSER)
        assert refreshed.status_code == 200
        assert "Active claimed work" in refreshed.text
    finally:
        client.__exit__(None, None, None)


def test_asking_for_a_panel_twice_does_not_stack_parameters(tmp_path):
    """The refresh URL is rebuilt from parsed parameters rather than appended to the
    raw query, so polling a panel's own URL stays a fixed point instead of growing a
    `panel=` on every hop."""
    client = _client(tmp_path, "stack.db")
    try:
        _admin(client)
        first = f"/admin/run-controls?panel={live.RUN_CONTROLS}"
        html = client.get(first, headers=BROWSER).text
        assert f'hx-get="{first}"' in html
        assert html.count(f"panel={live.RUN_CONTROLS}") == 1
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("path,panel,marker", SURFACES)
def test_polling_can_be_turned_off_entirely(tmp_path, monkeypatch, path, panel, marker):
    """0 disables it. The pages still render — they just stop asking, and say so
    rather than leaving a reader to assume the numbers are current."""
    monkeypatch.setattr(config, "LIVE_REFRESH_SECONDS", 0)
    client = _client(tmp_path, "off.db")
    try:
        _admin(client)
        page = client.get(path, headers=BROWSER).text
        assert marker in page
        assert "hx-trigger" not in page
        assert "Automatic refresh is off" in page
    finally:
        client.__exit__(None, None, None)


def test_the_interval_is_bounded_rather_than_taken_as_typed(monkeypatch):
    """The floor is a load statement, not a preference: the nav rollup measures
    ~0.5 ms for a real fleet, so a 1s poll would multiply a polling admin's cost
    tenfold without a human eye noticing the difference."""
    from athena.config import _refresh_env

    monkeypatch.setenv("ATHENA_LIVE_REFRESH_SECONDS", "0")
    assert _refresh_env("ATHENA_LIVE_REFRESH_SECONDS", 10) == 0
    monkeypatch.setenv("ATHENA_LIVE_REFRESH_SECONDS", "30")
    assert _refresh_env("ATHENA_LIVE_REFRESH_SECONDS", 10) == 30

    for bad in ("1", "4", "3601", "-5"):
        monkeypatch.setenv("ATHENA_LIVE_REFRESH_SECONDS", bad)
        with pytest.raises(ValueError, match="must be 0"):
            _refresh_env("ATHENA_LIVE_REFRESH_SECONDS", 10)

    monkeypatch.setenv("ATHENA_LIVE_REFRESH_SECONDS", "soon")
    with pytest.raises(ValueError, match="integer"):
        _refresh_env("ATHENA_LIVE_REFRESH_SECONDS", 10)
