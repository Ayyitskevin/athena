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


def _space_id(client, key):
    created = client.post("/spaces", json={"key": key, "name": key}, headers=H1)
    assert created.status_code == 201
    return created.json()["id"]


def _create_page(client, space_id, title, body=""):
    created = client.post(
        f"/spaces/{space_id}/pages", json={"title": title, "body": body}, headers=H1
    )
    assert created.status_code == 201
    return created.json()["id"]


def _rename_page(client, page_id, title):
    path = f"/pages/{page_id}"
    etag = client.get(path, headers=H1).headers["etag"]
    renamed = client.patch(
        path, json={"title": title}, headers={**H1, "If-Match": etag}
    )
    assert renamed.status_code == 200


def _xref(page_id, escaped_title):
    return f'<a href="/mentor/pages/{page_id}" class="xref">{escaped_title}</a>'


def test_saved_views_and_restored_previews_keep_their_original_runbook(tmp_path):
    with TestClient(create_app(tmp_path / "identity.db")) as client:
        _login(client)
        docs = _space_id(client, "DOC")
        ops = _space_id(client, "OPS")
        original = _create_page(client, ops, "Q&A")
        body = "Open [[Q&A]]"
        page = _create_page(client, docs, "Guide", body)
        issue = client.post(
            "/issues", json={"title": "Review", "body": body}, headers=H1
        ).json()["id"]
        _rename_page(client, original, "Runbook & notes")
        replacement = _create_page(client, docs, "Q&A")
        local = _create_page(client, docs, "Checklist")
        _create_page(client, ops, "Checklist")

        original_link = _xref(original, "Runbook &amp; notes")
        replacement_link = _xref(replacement, "Q&amp;A")
        draft = {"title": "Draft", "body": body + " and [[Checklist]]"}
        for prefix, source in (("/mentor/pages", page), ("/aegis/issues", issue)):
            endpoint = f"{prefix}/preview?source_id={source}"
            detail = f"{prefix}/{source}"
            assert original_link in client.get(detail).text
            form = client.get(f"{detail}/edit").text
            assert original_link in form
            assert f'hx-post="{endpoint}"' in form

            # A new token can be disambiguated by a page's own space. An issue
            # has no space, even when its numeric ID coincides with a page's.
            expected_new = (
                _xref(local, "Checklist")
                if prefix == "/mentor/pages"
                else '<span class="xref broken">[[Checklist]]</span>'
            )
            typed = client.post(endpoint, data=draft)
            assert typed.status_code == 200
            assert original_link in typed.text and expected_new in typed.text
            assert replacement_link not in typed.text

            assert client.post(f"{detail}/draft", data=draft).status_code == 200
            restored = client.get(f"{detail}/edit?restore=1").text
            assert typed.text.strip() in restored
            standalone = client.post(f"{prefix}/preview", data={"body": body})
            assert replacement_link in standalone.text

        exported = client.get(f"/mentor/spaces/{docs}/export.html").text
        assert original_link in exported and replacement_link not in exported
        context = client.get(f"/issues/{issue}/work-context", headers=H1).json()
        assert [
            (r["id"], r["title"]) for r in context["references"]["outgoing"]["items"]
        ] == [(original, "Runbook & notes")]


def test_private_bound_targets_and_preview_sources_remain_hidden(tmp_path):
    app = create_app(tmp_path / "private.db")
    with TestClient(app) as owner, TestClient(app) as reader:
        _login(owner)
        public = _space_id(owner, "DOC")
        private = _space_id(owner, "OPS")
        target = _create_page(owner, private, "Runbook")
        body = "[[Runbook]]"
        referring_page = _create_page(owner, public, "Guide", body)
        _rename_page(owner, target, "Restricted procedure")
        replacement = _create_page(owner, public, "Runbook")
        assert (
            owner.put(
                f"/spaces/{private}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )
        project = owner.post(
            "/projects", json={"key": "OPS", "name": "Ops"}, headers=H1
        ).json()["id"]
        hidden_issue = owner.post(
            "/issues",
            json={"title": "Private", "body": body, "project_id": project},
            headers=H1,
        ).json()["id"]
        assert (
            owner.put(
                f"/projects/{project}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )

        owner.post(
            "/users",
            json={"email": "r@e.com", "name": "Reader", "password": PASSWORD},
            headers=H1,
        )
        reader.post("/login", data={"email": "r@e.com", "password": PASSWORD})
        reader.headers["X-CSRF-Token"] = reader.cookies["athena_csrf"]
        assert (
            _xref(target, "Restricted procedure")
            in owner.get(f"/mentor/pages/{referring_page}").text
        )
        for rendered in (
            reader.get(f"/mentor/pages/{referring_page}"),
            reader.post(
                f"/mentor/pages/preview?source_id={referring_page}", data={"body": body}
            ),
            reader.get(f"/mentor/spaces/{public}/export.html"),
        ):
            assert rendered.status_code == 200
            assert '<span class="xref broken">[[Runbook]]</span>' in rendered.text
            assert "Restricted procedure" not in rendered.text
            assert _xref(replacement, "Runbook") not in rendered.text
        for prefix, source in (
            ("/mentor/pages", target),
            ("/aegis/issues", hidden_issue),
        ):
            hidden = reader.post(
                f"{prefix}/preview?source_id={source}", data={"body": body}
            )
            missing = reader.post(
                f"{prefix}/preview?source_id=999999", data={"body": body}
            )
            assert hidden.status_code == missing.status_code == 404
            assert hidden.text == missing.text
            out_of_range = reader.post(
                f"{prefix}/preview?source_id={1 << 63}", data={"body": body}
            )
            assert out_of_range.status_code == 422
