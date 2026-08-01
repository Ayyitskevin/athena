"""Stage R-3 — the human-readable exit, and what it refuses to pretend.

JSON portability already lets a machine take the data. This is the half that
matters when Athena is gone: a person opening one file in a browser. So the
tests here are mostly about self-containment and honesty — no request to a
server, no live thing frozen into a file as though it were still live, and a
footer that names everything the export left out.

The sharp case is embeds. An embed resolves against a viewer and a moment;
written into a file it would be stale data wearing a live face. The export
renders it as a dead box that still shows the directive it came from, so a
reader sees both that nothing was resolved and exactly what the author wrote.
"""

import base64

from fastapi.testclient import TestClient

from athena.main import create_app
from athena.web import html_export

H1 = {"X-Athena-Actor": "1"}
PASSWORD = "pw-long-enough"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+AAAAHElEQVQI12P8z8Dwn4"
    "EIwESMolGFowpHFQ4hhQBHbgQ2r6HxAgAAAABJRU5ErkJggg=="
)


def _app(tmp_path, monkeypatch, name="x.db"):
    attach = tmp_path / "attach"
    attach.mkdir()
    monkeypatch.setattr("athena.config.ATTACH_DIR", str(attach))
    return create_app(tmp_path / name)


def _space(client, key="DOC", name="Handbook"):
    client.post(
        "/users", json={"email": "a@e.com", "name": "Ann", "password": PASSWORD}
    )
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _page(client, space_id, title, body, parent_id=None):
    payload = {"title": title, "body": body}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    return client.post(f"/spaces/{space_id}/pages", json=payload, headers=H1).json()


def test_the_export_is_one_self_contained_file(tmp_path, monkeypatch):
    """No stylesheet to fetch, no image that only resolves against a running
    Athena. If it needs the server, it is not an exit."""
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client)
        page = _page(client, space["id"], "Overview", "# Overview")
        stored = client.post(
            f"/pages/{page['id']}/attachments",
            files={"file": ("shot.png", PNG, "image/png")},
            headers=H1,
        ).json()
        client.patch(
            f"/pages/{page['id']}",
            json={"body": f"![shot](/attachments/{stored['id']})"},
            headers=H1,
        )

        document = client.get(
            f"/mentor/spaces/{space['id']}/export.html", headers=H1
        ).text
        assert "data:image/png;base64," in document
        # Nothing points back at the live server.
        assert '"/attachments/' not in document
        assert "<link" not in document and "<script" not in document
        assert document.startswith("<!doctype html>")


def test_an_exported_embed_is_visibly_dead_and_still_shows_its_directive(
    tmp_path, monkeypatch
):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client)
        _page(
            client,
            space["id"],
            "Dashboard",
            "```athena\nkind: count\nq: is:open\n```",
        )
        document = client.get(
            f"/mentor/spaces/{space['id']}/export.html", headers=H1
        ).text
        assert "cannot be resolved in an exported copy" in document
        # The directive survives, escaped, so nothing the author wrote is lost.
        assert "embed-directive" in document
        assert "kind: count" in document
        # And it is text, not a live directive that some other renderer might run.
        assert "```athena" not in document.split("embed-directive")[1][:200]


def test_every_page_appears_once_with_a_table_of_contents(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client)
        parent = _page(client, space["id"], "Parent", "top")
        _page(client, space["id"], "Child", "nested", parent_id=parent["id"])
        _page(client, space["id"], "Sibling", "beside")

        document = client.get(
            f"/mentor/spaces/{space['id']}/export.html", headers=H1
        ).text
        assert document.count('class="page"') == 3
        for title in ("Parent", "Child", "Sibling"):
            assert "#page-" in document
            assert title in document
        # Children are nested under their parent in the contents list.
        assert 'style="margin-left: 1.2rem"' in document


def test_the_export_says_it_is_a_snapshot_and_what_it_contains(tmp_path, monkeypatch):
    """A file that looks like the app but is months stale is a trap unless it
    admits what it is."""
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client)
        _page(client, space["id"], "Page", "body")
        document = client.get(
            f"/mentor/spaces/{space['id']}/export.html", headers=H1
        ).text
        assert "is a snapshot and is not live" in document
        assert "Only pages you could see" in document
        assert "Embeds are not resolved in an export" in document


def test_the_export_renders_as_the_reader_not_the_author(tmp_path, monkeypatch):
    """Bodies render through the same renderer the page uses, with the exporting
    viewer's visibility — a link to something they cannot read is dead here
    exactly as it is on the page."""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        space = _space(client)
        project = client.post(
            "/projects", json={"key": "SEC", "name": "Secret"}, headers=H1
        ).json()
        client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        hidden = client.post(
            "/issues",
            json={"title": "Classified", "project_id": project["id"]},
            headers=H1,
        ).json()
        _page(client, space["id"], "Refers", f"See [[issue:{hidden['id']}]].")

        # The web route reads the SESSION, not the REST actor header — so the
        # author has to actually be signed in for this to be their view.
        client.post("/login", data={"email": "a@e.com", "password": PASSWORD})
        mine = client.get(f"/mentor/spaces/{space['id']}/export.html").text
        assert f"/aegis/issues/{hidden['id']}" in mine

        anonymous = TestClient(app)
        theirs = anonymous.get(f"/mentor/spaces/{space['id']}/export.html").text
        assert f"/aegis/issues/{hidden['id']}" not in theirs
        assert "Classified" not in theirs


def test_a_space_the_viewer_cannot_see_is_the_same_404_as_a_missing_one(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        space = _space(client)
        _page(client, space["id"], "Secret page", "confidential body")
        client.put(
            f"/spaces/{space['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        anonymous = TestClient(app)
        hidden = anonymous.get(f"/mentor/spaces/{space['id']}/export.html")
        missing = anonymous.get("/mentor/spaces/99999/export.html")
        assert hidden.status_code == missing.status_code == 404
        assert "confidential body" not in hidden.text
        assert "Secret page" not in hidden.text


def test_the_download_names_the_file_after_the_space(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client, key="OPS", name="Operations")
        _page(client, space["id"], "Page", "body")
        response = client.get(f"/mentor/spaces/{space['id']}/export.html", headers=H1)
        assert response.headers["content-disposition"] == (
            'attachment; filename="athena-ops.html"'
        )
        assert response.headers["content-type"].startswith("text/html")


def test_an_image_too_large_to_inline_is_named_rather_than_dropped(
    tmp_path, monkeypatch
):
    """A silently missing image reads as a rendering bug. Saying which one was
    left out, and why, is the difference between a bound and a lie."""
    monkeypatch.setattr(html_export, "MAX_IMAGE_BYTES", 10)
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client)
        page = _page(client, space["id"], "Shots", "x")
        stored = client.post(
            f"/pages/{page['id']}/attachments",
            files={"file": ("huge.png", PNG, "image/png")},
            headers=H1,
        ).json()
        client.patch(
            f"/pages/{page['id']}",
            json={"body": f"![huge](/attachments/{stored['id']})"},
            headers=H1,
        )
        document = client.get(
            f"/mentor/spaces/{space['id']}/export.html", headers=H1
        ).text
        assert "Image not included" in document
        assert "huge.png" in document
        assert "data:image/png" not in document


def test_the_page_ceiling_reports_what_it_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(html_export, "MAX_PAGES", 2)
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client)
        for index in range(5):
            _page(client, space["id"], f"Page {index}", "body")
        document = client.get(
            f"/mentor/spaces/{space['id']}/export.html", headers=H1
        ).text
        assert document.count('class="page"') == 2
        assert "3 further pages were not included" in document
        assert "stops at 2 pages" in document


def test_an_empty_space_still_exports_a_readable_file(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        space = _space(client)
        document = client.get(
            f"/mentor/spaces/{space['id']}/export.html", headers=H1
        ).text
        assert "Handbook" in document
        assert "0 pages exported" in document
