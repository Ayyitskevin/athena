"""One partial renders a completion report wherever one appears (U9).

COCKPIT-12: a completion report was raw JSON on five surfaces and readable only
on the work-context page, whose unwrapped ``<pre>`` made the page 7,632 px
wide. A report is text an agent wrote: it is data. Every surface that shows a
``claim_completed`` event now renders the report through
``aegis/partials/completion_report.html``, through Jinja's autoescape (never
``|safe``), with long values wrapping and keys the partial does not know shown
rather than dropped.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from support import ADMIN, bearer, bootstrap, login, make_app, onboard_agent

from athena.aegis import issue_activity
from athena.core import activity, db, notifications
from athena.web import render
from athena.web.router import get_templates

RUN = "iris-run-0001"
#: One unbroken 400-character value: the shape that widened the page.
LONG = "x" * 400
REPORT = {
    "verdict": "PASS-WITH-NOTES",
    "findings": {"P2": 1},
    "results": [
        {
            "condition": "<b>bold</b> condition",
            "state": "met",
            "note": "ran it <script>alert(1)</script>",
        }
    ],
    "unfinished": ["<i>later</i>"],
    "uncertainty": [LONG],
    "evidence": [
        {"kind": "file", "source": "repo", "ref": "src/app.py", "scope": "source-only"}
    ],
    "usage": {"input_tokens": 1234, "output_tokens": 56, "model": "fable-5"},
}
SURFACES = (
    "/aegis/issues/{issue}",
    "/aegis/issues/{issue}/work-context",
    "/aegis/issues/{issue}/history",
    "/aegis/activity",
    "/aegis/activity/runs?actor={agent}",
    "/aegis/activity/runs/{run}/lineage",
    "/aegis/dashboard",
    "/admin/agents",
    "/inbox",
)


def _reported(client: TestClient) -> tuple[dict, dict]:
    """An agent claims an issue the admin watches, then completes it with REPORT."""
    agent = onboard_agent(client, email="iris@e.com", name="Iris")
    issue = client.post("/issues", json={"title": "reported work"}, headers=ADMIN)
    assert issue.status_code == 201, issue.text
    issue_id = issue.json()["id"]
    delegated = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    assert delegated.status_code in (200, 201), delegated.text
    login(client, "a@e.com")
    watched = client.post(f"/aegis/issues/{issue_id}/watch", follow_redirects=False)
    assert watched.status_code == 303, watched.text
    read = client.get(f"/issues/{issue_id}", headers=bearer(agent))
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        headers={**bearer(agent, run=RUN), "If-Match": read.headers["ETag"]},
    )
    assert claimed.status_code in (200, 201), claimed.text
    done = client.post(
        f"/issues/{issue_id}/complete",
        json={"generation": claimed.json()["generation"], **REPORT},
        headers=bearer(agent, run=RUN),
    )
    assert done.status_code == 200, done.text
    return agent, issue.json()


@pytest.fixture
def pages(tmp_path) -> dict[str, str]:
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent, issue = _reported(client)
        rendered = {}
        for surface in SURFACES:
            url = surface.format(issue=issue["id"], agent=agent["user"]["id"], run=RUN)
            response = client.get(url)
            assert response.status_code == 200, (url, response.text[:300])
            rendered[surface] = response.text
    return rendered


@pytest.mark.parametrize("surface", SURFACES)
def test_every_surface_renders_the_report_through_the_partial(pages, surface):
    page = pages[surface]
    # The report is read, not dumped: no raw payload marker, no JSON keys.
    assert "reported_completion_v1" not in page
    assert '"verdict"' not in page and "&#34;verdict&#34;" not in page
    assert "Reported completion (advisory)" in page
    assert "reported verdict PASS-WITH-NOTES" in page
    # Agent-written text is data: escaped, never markup.
    assert "&lt;b&gt;bold&lt;/b&gt; condition" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page
    assert "<b>bold</b>" not in page
    assert "&lt;i&gt;later&lt;/i&gt;" in page
    # Long values sit in the wrapping report block, and usage is not dropped.
    report = page[page.index('<details class="report"') :]
    assert LONG in report
    assert "input_tokens" in report and "1234" in report


def test_the_partial_shows_keys_it_does_not_know(tmp_path):
    """A key the partial was not taught is shown, not dropped, and its value is
    escaped like everything else."""
    app, _ = make_app(tmp_path)
    with TestClient(app):
        partial = get_templates().env.get_template(
            "aegis/partials/completion_report.html"
        )
        html = str(
            partial.module.show_report(
                {
                    "verdict": "PASS",
                    "mood": "<em>cheerful</em>",
                    "extra": {"nested": ["<u>a</u>", 2]},
                }
            )
        )
    assert "reported verdict PASS" in html
    assert "mood" in html and "&lt;em&gt;cheerful&lt;/em&gt;" in html
    assert "<em>" not in html
    assert "nested" in html and "&lt;u&gt;a&lt;/u&gt;" in html
    # One block, the same class every surface wraps.
    assert len(re.findall(r'<details class="report"', html)) == 1


def test_an_unknown_value_is_shown_flat_and_never_walked(tmp_path):
    """An unknown key's value is printed once as escaped text: a self-referential
    or deeply nested value cannot recurse the renderer."""
    looped: dict = {"name": "<i>me</i>"}
    looped["self"] = looped
    deep: list = []
    cursor = deep
    for _ in range(400):
        cursor.append([])
        cursor = cursor[0]
    app, _ = make_app(tmp_path)
    with TestClient(app):
        partial = get_templates().env.get_template(
            "aegis/partials/completion_report.html"
        )
        html = str(partial.module.show_report({"looped": looped, "deep": deep}))
    assert "looped" in html and "&lt;i&gt;me&lt;/i&gt;" in html and "<i>" not in html
    assert "deep" in html


def test_an_imported_completion_in_the_inbox_is_not_a_native_report(tmp_path):
    """The inbox row names its event's import stamp ``event_imported_at``. A
    completion another system reported is shown as its text, never rendered as
    Athena's own report."""
    app, db_file = make_app(tmp_path)
    detail = (
        "generation "
        + "a" * 32
        + issue_activity.COMPLETION_REPORT_MARKER
        + '{"verdict":"PASS"}'
    )
    with TestClient(app) as client:
        bootstrap(client)
        issue = client.post("/issues", json={"title": "imported"}, headers=ADMIN)
        conn = db.connect(db_file)
        try:
            event = activity.record(
                conn,
                actor_id=1,
                verb="claim_completed",
                target_kind="issue",
                target_id=issue.json()["id"],
                detail=detail,
                imported_at="2026-01-01 00:00:00",
                commit=False,
            )
            notifications.notify_users(conn, event_id=event["id"], user_ids=[1])
            conn.commit()
        finally:
            conn.close()
        login(client, "a@e.com")
        inbox = client.get("/inbox").text

    native = {"verb": "claim_completed", "detail": detail}
    assert render.completion_parts({**native, "event_imported_at": None})["report"]
    imported = {**native, "event_imported_at": "2026-01-01 00:00:00"}
    assert render.completion_parts(imported)["report"] is None
    assert "reported_completion_v1" in inbox  # shown as the text it is
    assert "Reported completion (advisory)" not in inbox
