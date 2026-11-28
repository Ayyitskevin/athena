"""The operator's first minute (K3 of docs/plans/CONSOLIDATION_SPEC.md).

WHY: a review of the running demo found four things an operator meets before
doing anything. The logo led a signed-in operator to two link cards. No page
had a gutter: a heading touched the sidebar and a panel ran to the window's
edge. The issue page and the issue list said "Created By 1". And a claim in
an issue's history read "generation 634e140f...; until ...; path_fence
issue_only; coordination_root null". Each is held here by the page itself.
"""

from __future__ import annotations

from pathlib import Path
import re

from fastapi.testclient import TestClient
from support import ADMIN, bearer, bootstrap, login, make_app, onboard_agent

from athena.main import create_app
from athena.web import render

STYLES = Path(render.__file__).resolve().parent.parent / "static" / "styles.css"


def test_the_logo_takes_a_signed_in_operator_to_the_dashboard(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        signed_out = client.get("/")
        assert signed_out.status_code == 200
        assert "A self-hosted workspace" in signed_out.text

        login(client, "a@e.com")
        followed = client.get("/", follow_redirects=False)
        assert followed.status_code == 303
        assert followed.headers["location"] == "/aegis/dashboard"
        landed = client.get("/")
        assert "<h1>Dashboard</h1>" in landed.text
        assert "Fleet attention" in landed.text


def _declarations(selector: str) -> list[str]:
    """Each rule body the stylesheet gives ``selector`` exactly."""
    css = STYLES.read_text(encoding="utf-8")
    return re.findall(rf"(?m)^\s*{re.escape(selector)} \{{([^}}]*)\}}", css)


def test_every_page_has_a_gutter_and_the_document_layout_is_the_exception():
    # The work region is every page's <main>. With no padding of its own and
    # no wrapper in any template, no page had a gutter.
    desktop, phone = _declarations(".work")
    assert "padding: var(--sp-6)" in desktop
    assert "padding: var(--sp-5) var(--sp-4)" in phone
    # The Mentor document layout pads its own tree and reading column.
    (full_bleed,) = _declarations(".work:has(> .doc-layout)")
    assert full_bleed.strip() == "padding: 0;"


def test_an_issue_names_who_created_it_on_its_page_and_in_the_list(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client, name="Ann Operator")
        made = client.post("/issues", json={"title": "Named creator"}, headers=ADMIN)
        assert made.status_code == 201, made.text
        issue_id = made.json()["id"]
        login(client, "a@e.com")

        page = " ".join(client.get(f"/aegis/issues/{issue_id}").text.split())
        assert "<strong>Created by</strong> Ann Operator" in page
        assert "Created By" not in page

        listed = " ".join(client.get("/aegis/issues").text.split())
        assert "<th>Created by</th>" in listed
        assert '<td class="muted">Ann Operator</td>' in listed
        assert '<td class="muted">1</td>' not in listed


def test_a_claim_in_the_history_reads_as_a_claim(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client, email="sol@e.com", name="Sol")
        made = client.post(
            "/issues",
            json={"title": "Held", "assignee_id": sol["user"]["id"]},
            headers=ADMIN,
        )
        issue_id = made.json()["id"]
        tag = client.get(f"/issues/{issue_id}", headers=bearer(sol)).headers["ETag"]
        claim = client.post(
            f"/issues/{issue_id}/claim",
            json={},
            headers={**bearer(sol), "If-Match": tag},
        )
        assert claim.status_code == 201, claim.text
        generation = claim.json()["generation"]
        login(client, "a@e.com")

        page = " ".join(client.get(f"/aegis/issues/{issue_id}").text.split())
        (row,) = re.findall(
            r'claimed <span class="activity-detail"[^>]*>([^<]*)<', page
        )
        assert re.fullmatch(r"until \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", row), row
        # The machine's identifiers are not the visible text; the stored
        # detail, generation included, stays in the row's title and the trail.
        visible = re.sub(r"<[^>]*>", " ", page)
        assert generation not in visible
        assert "coordination_root" not in visible and "path_fence" not in visible
        assert f"generation {generation}" in page


def test_claim_words_keeps_what_it_cannot_read():
    worded = render.claim_words(
        {
            "detail": "generation abc123; until 2026-11-28 15:38:01; "
            'path_fence rooted; coordination_root {"key": "repo"}'
        }
    )
    assert worded == "until 2026-11-28 15:38:01 UTC; paths fenced within their root"
    unresolved = render.claim_words(
        {
            "detail": "generation abc123; until 2026-11-28 15:38:01; "
            "path_fence unresolved; coordination_root null"
        }
    )
    assert unresolved.endswith("; paths fenced as written, root unknown")
    # An imported or older row in another form is shown whole, never dropped.
    assert render.claim_words({"detail": "claimed by hand"}) == "claimed by hand"
    assert render.claim_words({"detail": None}) == ""


def test_the_landing_page_is_still_public_metadata(tmp_path):
    # Signed out, / is what it was: nothing about the workspace.
    app = create_app(tmp_path / "public.db")
    with TestClient(app) as client:
        page = client.get("/")
    assert page.status_code == 200 and "Fleet attention" not in page.text
