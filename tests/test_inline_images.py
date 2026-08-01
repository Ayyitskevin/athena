"""Stage R-4 — attachment images that actually display, and only when safe.

`![alt](/attachments/N)` already survived the Markdown pipeline before this
stage, but the contract rested entirely on a third-party sanitizer's default
allowlist with no test naming it: an `nh3` bump or an explicit `tags=` override
would have removed inline images silently. These tests pin the whole path.

The half that is genuinely new is the honesty of the served type. A browser is
told `nosniff`, so what Athena declares is what the bytes are treated as — which
means the declaration must come from the bytes and never from the uploader's
claim. Serving a caller-labelled file inline is how an upload becomes stored
XSS, so the rules under test are: real images display, mislabelled real images
still display, and anything merely *claiming* to be an image is an opaque
download.
"""

import base64

from fastapi.testclient import TestClient

from athena.core import attachments
from athena.main import create_app
from athena.web.render import render_body

H1 = {"X-Athena-Actor": "1"}
PASSWORD = "pw-long-enough"

# A real 4x4 PNG — the bytes matter here, so no placeholder will do.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+AAAAHElEQVQI12P8z8Dwn4"
    "EIwESMolGFowpHFQ4hhQBHbgQ2r6HxAgAAAABJRU5ErkJggg=="
)
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 12
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
SCRIPTED_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
)


def _app(tmp_path, monkeypatch, name="img.db"):
    attach = tmp_path / "attach"
    attach.mkdir()
    monkeypatch.setattr("athena.config.ATTACH_DIR", str(attach))
    return create_app(tmp_path / name)


def _admin(client):
    client.post(
        "/users", json={"email": "a@e.com", "name": "Ann", "password": PASSWORD}
    )


def _page(client):
    space = client.post("/spaces", json={"key": "DOC", "name": "Docs"}, headers=H1)
    return client.post(
        f"/spaces/{space.json()['id']}/pages",
        json={"title": "Page", "body": "body"},
        headers=H1,
    ).json()


def _upload(client, page_id, name, data, content_type):
    return client.post(
        f"/pages/{page_id}/attachments",
        files={"file": (name, data, content_type)},
        headers=H1,
    ).json()


# ---------------------------------------------------------------------------
# The stored type is decided by the bytes.
# ---------------------------------------------------------------------------


def test_sniffing_recognises_the_raster_formats_and_nothing_else():
    assert attachments.sniff_image_type(PNG) == "image/png"
    assert attachments.sniff_image_type(GIF) == "image/gif"
    assert attachments.sniff_image_type(WEBP) == "image/webp"
    assert attachments.sniff_image_type(JPEG) == "image/jpeg"
    assert attachments.sniff_image_type(SCRIPTED_SVG) is None
    assert attachments.sniff_image_type(b"<html></html>") is None
    assert attachments.sniff_image_type(b"") is None
    # RIFF alone is also audio and video; both halves of the header must match.
    assert attachments.sniff_image_type(b"RIFF" + b"\x00" * 4 + b"WAVE") is None


def test_a_claim_can_never_buy_inline_rendering():
    """The rule that keeps an upload from becoming stored XSS: bytes that merely
    CLAIM an inline-eligible type are recorded as an opaque download."""
    assert attachments.stored_content_type("image/png", PNG) == "image/png"
    # Unlabelled or mislabelled real images are still recognised as themselves.
    assert attachments.stored_content_type(None, PNG) == "image/png"
    assert (
        attachments.stored_content_type("application/octet-stream", PNG) == "image/png"
    )
    # A document wearing an image's name is not an image.
    assert (
        attachments.stored_content_type("image/png", b"<html><script>x</script></html>")
        == "application/octet-stream"
    )
    assert (
        attachments.stored_content_type("image/png; charset=utf-8", b"nope")
        == "application/octet-stream"
    )
    # Types that were never inline-eligible keep the uploader's label.
    assert attachments.stored_content_type("text/plain", b"hello") == "text/plain"
    assert (
        attachments.stored_content_type("image/svg+xml", SCRIPTED_SVG)
        == "image/svg+xml"
    )


def test_svg_is_never_inline_because_it_can_carry_script():
    assert "image/svg+xml" not in attachments.INLINE_CONTENT_TYPES
    assert attachments.INLINE_CONTENT_TYPES == {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }


# ---------------------------------------------------------------------------
# What the download route actually serves.
# ---------------------------------------------------------------------------


def test_images_are_served_inline_and_everything_else_is_a_download(
    tmp_path, monkeypatch
):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        _admin(client)
        page = _page(client)

        def disposition(name, data, content_type):
            stored = _upload(client, page["id"], name, data, content_type)
            response = client.get(f"/attachments/{stored['id']}", headers=H1)
            assert response.headers["x-content-type-options"] == "nosniff"
            return (
                response.headers["content-type"],
                response.headers["content-disposition"].split(";")[0],
            )

        assert disposition("a.png", PNG, "image/png") == ("image/png", "inline")
        # A real image the client mislabelled still displays — the case that
        # previously produced a broken image under nosniff.
        assert disposition("b.png", PNG, "application/octet-stream") == (
            "image/png",
            "inline",
        )
        # A document claiming to be an image is neither typed nor served as one.
        assert disposition("c.png", b"<html>x</html>", "image/png") == (
            "application/octet-stream",
            "attachment",
        )
        assert disposition("d.svg", SCRIPTED_SVG, "image/svg+xml") == (
            "image/svg+xml",
            "attachment",
        )
        assert disposition("e.txt", b"hello", "text/plain")[1] == "attachment"


def test_an_inline_image_is_still_gated_by_the_page_that_holds_it(
    tmp_path, monkeypatch
):
    """Inline changes how bytes are presented, never who may fetch them."""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as owner:
        _admin(owner)
        space = owner.post(
            "/spaces", json={"key": "SEC", "name": "Secret"}, headers=H1
        ).json()
        page = owner.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Private", "body": "b"},
            headers=H1,
        ).json()
        stored = _upload(owner, page["id"], "a.png", PNG, "image/png")
        owner.put(
            f"/spaces/{space['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        assert owner.get(f"/attachments/{stored['id']}", headers=H1).status_code == 200

        anonymous = TestClient(app)
        assert anonymous.get(f"/attachments/{stored['id']}").status_code == 404


# ---------------------------------------------------------------------------
# The renderer, and the affordance that makes the feature discoverable.
# ---------------------------------------------------------------------------


def test_markdown_image_survives_the_sanitizer(tmp_path, monkeypatch):
    """Pins the contract that currently rests on a third-party default: a bump
    or an explicit tags= override must not silently kill inline images."""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _admin(client)
        page = _page(client)
        stored = _upload(client, page["id"], "a.png", PNG, "image/png")
        body = f"![a red square](/attachments/{stored['id']})"
        client.patch(f"/pages/{page['id']}", json={"body": body}, headers=H1)
        client.post("/login", data={"email": "a@e.com", "password": PASSWORD})
        rendered = client.get(f"/mentor/pages/{page['id']}").text
        assert f'<img src="/attachments/{stored["id"]}"' in rendered
        assert 'alt="a red square"' in rendered


def test_the_renderer_still_refuses_dangerous_image_sources(tmp_path, monkeypatch):
    """Inline images must not become a hole. Author-written HTML stays escaped,
    an onerror handler never survives, and a javascript: source is stripped."""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _admin(client)
        _page(client)
    from athena.core import db

    conn = db.connect(tmp_path / "img.db")
    try:
        # Author-written HTML is escaped into text, so the handler never becomes
        # an attribute — it survives only as inert characters inside a <p>.
        raw_html = str(render_body(conn, '<img src="x" onerror="alert(1)">'))
        assert "<img" not in raw_html
        assert "&lt;img" in raw_html

        # A hostile source never becomes an image element at all — the directive
        # is left as inert text, which is the only outcome that matters.
        scripted = str(render_body(conn, "![x](javascript:alert(1))"))
        assert "<img" not in scripted

        data_uri = str(render_body(conn, "![x](data:text/html;base64,PHNjcmlwdD4=)"))
        assert "<img" not in data_uri

        # The legitimate case still works, so the guards above are not just
        # "everything is stripped".
        good = str(render_body(conn, "![ok](/attachments/7)"))
        assert '<img src="/attachments/7"' in good
    finally:
        conn.close()


def test_image_attachments_offer_a_thumbnail_and_the_markdown_to_embed_them(
    tmp_path, monkeypatch
):
    """A capability nothing mentions is invisible. The attachment list shows what
    an image is and hands over the exact directive that embeds it."""
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _admin(client)
        page = _page(client)
        image = _upload(client, page["id"], "shot.png", PNG, "image/png")
        document = _upload(client, page["id"], "notes.txt", b"hello", "text/plain")
        issue = client.post("/issues", json={"title": "I"}, headers=H1).json()
        client.post(
            f"/issues/{issue['id']}/attachments",
            files={"file": ("shot2.png", PNG, "image/png")},
            headers=H1,
        )
        client.post("/login", data={"email": "a@e.com", "password": PASSWORD})

        rendered = client.get(f"/mentor/pages/{page['id']}").text
        assert "attachment-thumb" in rendered
        assert f"![shot.png](/attachments/{image['id']})" in rendered
        # A non-image gets neither a thumbnail nor an embed snippet.
        assert f"![notes.txt](/attachments/{document['id']})" not in rendered

        on_issue = client.get(f"/aegis/issues/{issue['id']}").text
        assert "attachment-thumb" in on_issue
        assert "![shot2.png](/attachments/" in on_issue
