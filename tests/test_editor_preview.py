"""Stage R-1 — a preview that cannot drift from what readers will see.

The promise is not "a preview exists" but "the preview is the display". Two
renderers would satisfy the first and quietly break the second the day one of
them learned something the other did not, so each surface's render policy is a
single named function — `render_page_body`, `render_issue_body` — and both the
view and the preview call it.

The sharpest test of that is the case an author might wish were different:
embeds are deliberately not resolved on issues, so an issue preview must show
the same "not rendered here" box the saved issue will. A preview that rendered a
live embed there would be lying about what saving produces.
"""

from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
PASSWORD = "pw-long-enough"


def _login(client):
    client.post(
        "/users", json={"email": "a@e.com", "name": "Ann", "password": PASSWORD}
    )
    client.post("/login", data={"email": "a@e.com", "password": PASSWORD})
    csrf = client.cookies.get("athena_csrf", "")
    client.headers["X-CSRF-Token"] = csrf
    return csrf


def _page(client, body="body"):
    space = client.post("/spaces", json={"key": "DOC", "name": "Docs"}, headers=H1)
    return client.post(
        f"/spaces/{space.json()['id']}/pages",
        json={"title": "Page", "body": body},
        headers=H1,
    ).json()


def test_the_preview_renders_markdown_the_way_the_page_will(tmp_path):
    with TestClient(create_app(tmp_path / "p.db")) as client:
        csrf = _login(client)
        rendered = client.post(
            "/mentor/pages/preview",
            data={"body": "## Live\n\n- one\n- two", "csrf_token": csrf},
        )
        assert rendered.status_code == 200
        assert "<h2>Live</h2>" in rendered.text
        assert "<li>one</li>" in rendered.text


def test_the_editor_opens_with_the_preview_already_filled(tmp_path):
    """An author opening an existing page should see it as readers do before
    touching a key — a blank pane would suggest the page is empty."""
    with TestClient(create_app(tmp_path / "f.db")) as client:
        _login(client)
        page = _page(client, body="# Hello\n\nsome *text*")
        form = client.get(f"/mentor/pages/{page['id']}/edit").text
        assert "editor-split" in form
        assert "<h1>Hello</h1>" in form
        assert "<em>text</em>" in form


def test_each_surface_previews_under_its_own_embed_policy(tmp_path):
    """The anti-drift test. Pages resolve embeds and issues do not, so the two
    previews must differ in exactly that way — an issue preview showing a live
    embed would promise something saving the issue will not deliver."""
    with TestClient(create_app(tmp_path / "e.db")) as client:
        csrf = _login(client)
        issue = client.post("/issues", json={"title": "Target"}, headers=H1).json()
        directive = f"```athena\nkind: issue\nissue: {issue['id']}\n```"

        on_page = client.post(
            "/mentor/pages/preview", data={"body": directive, "csrf_token": csrf}
        ).text
        assert "embed-issue" in on_page
        assert "Embed not rendered here" not in on_page

        on_issue = client.post(
            "/aegis/issues/preview", data={"body": directive, "csrf_token": csrf}
        ).text
        assert "Embed not rendered here" in on_issue
        assert "embed-issue" not in on_issue


def test_the_preview_matches_the_saved_page_exactly(tmp_path):
    """Render the same text through the preview and through the page itself; the
    body markup must be identical. This is the property the whole design exists
    to hold, so it is asserted directly rather than inferred."""
    with TestClient(create_app(tmp_path / "m.db")) as client:
        csrf = _login(client)
        other = client.post("/issues", json={"title": "Linked"}, headers=H1).json()
        body = f"# Title\n\nSee [[issue:{other['id']}]] and some `code`."

        previewed = client.post(
            "/mentor/pages/preview", data={"body": body, "csrf_token": csrf}
        ).text.strip()
        page = _page(client, body=body)
        displayed = client.get(f"/mentor/pages/{page['id']}").text
        # Every fragment the preview produced appears in the rendered page.
        for fragment in (
            "<h1>Title</h1>",
            "<code>code</code>",
            f"/aegis/issues/{other['id']}",
        ):
            assert fragment in previewed, fragment
            assert fragment in displayed, fragment


def test_a_preview_never_renders_author_html_as_markup(tmp_path):
    with TestClient(create_app(tmp_path / "x.db")) as client:
        csrf = _login(client)
        for endpoint in ("/mentor/pages/preview", "/aegis/issues/preview"):
            rendered = client.post(
                endpoint,
                data={"body": "<script>alert(1)</script>", "csrf_token": csrf},
            ).text
            assert "<script>" not in rendered
            assert "&lt;script&gt;" in rendered


def test_a_preview_resolves_against_the_viewer_not_the_author(tmp_path):
    """Cross-links resolve per reader, so a preview must render as the person
    looking at it. A private issue is a dead link to someone who cannot see it,
    in the preview exactly as on the saved page."""
    app = create_app(tmp_path / "v.db")
    with TestClient(app) as owner:
        csrf = _login(owner)
        project = owner.post(
            "/projects", json={"key": "SEC", "name": "Secret"}, headers=H1
        ).json()
        owner.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        hidden = owner.post(
            "/issues",
            json={"title": "Classified", "project_id": project["id"]},
            headers=H1,
        ).json()
        body = f"[[issue:{hidden['id']}]]"

        mine = owner.post(
            "/mentor/pages/preview", data={"body": body, "csrf_token": csrf}
        ).text
        assert f"/aegis/issues/{hidden['id']}" in mine

        outsider = TestClient(app)
        outsider.post(
            "/users",
            json={"email": "v@e.com", "name": "Vic", "password": PASSWORD},
            headers=H1,
        )
        outsider.post("/login", data={"email": "v@e.com", "password": PASSWORD})
        their_csrf = outsider.cookies.get("athena_csrf", "")
        theirs = outsider.post(
            "/mentor/pages/preview",
            data={"body": body, "csrf_token": their_csrf},
            headers={"X-CSRF-Token": their_csrf},
        ).text
        assert f"/aegis/issues/{hidden['id']}" not in theirs
        assert "Classified" not in theirs


def test_preview_is_gated_bounded_and_csrf_protected(tmp_path):
    app = create_app(tmp_path / "g.db")
    with TestClient(app) as client:
        csrf = _login(client)
        for endpoint in ("/mentor/pages/preview", "/aegis/issues/preview"):
            anonymous = TestClient(app)
            assert anonymous.post(endpoint, data={"body": "x"}).status_code == 401
            assert (
                client.post(
                    endpoint,
                    data={"body": "x"},
                    headers={"X-CSRF-Token": "wrong"},
                ).status_code
                == 403
            )
            # A preview fires on every keystroke, so it carries the same bound
            # the saved path has rather than rendering an unbounded body.
            assert (
                client.post(
                    endpoint,
                    data={"body": "x" * 200_001, "csrf_token": csrf},
                ).status_code
                == 413
            )


def test_the_editor_still_works_without_javascript(tmp_path):
    """The preview is an enhancement, never a requirement: the form still posts
    and saves with no hx-* attribute ever firing."""
    with TestClient(create_app(tmp_path / "n.db")) as client:
        csrf = _login(client)
        page = _page(client, body="before")
        saved = client.post(
            f"/mentor/pages/{page['id']}/edit",
            data={"title": "Page", "body": "after", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert "after" in client.get(f"/mentor/pages/{page['id']}").text
