"""style-src carries no 'unsafe-inline', and the pages that needed it still render.

The exception existed because a handful of styles genuinely depend on data — a
label's stored hex, a rollup bar's percentage, a page's nesting depth, a generated
SVG's width. None of those can be a static class: the value is not known until the
row is read.

They are not all solved the same way, because they are not the same problem:

- Bounded numbers (bar percentages 0-100, tree depth) became STEPPED CLASSES. The
  exact count still rides in the segment's title; a one-percent step is visually
  identical.
- Genuinely arbitrary values (a user's #RRGGBB label colour, a computed SVG width)
  became tiny nonce-carrying <style> ELEMENTS. A CSP nonce does not license inline
  `style=` attributes — only <style> elements and scripts — so the attribute form had
  to go entirely, which is what these tests check.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from athena.main import content_security_policy, create_app

H = {"X-Athena-Actor": "1"}
BROWSER = {"X-Athena-Actor": "1", "Accept": "text/html"}


def _seed(client):
    client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})


def test_the_policy_never_contains_unsafe_inline():
    assert "unsafe-inline" not in content_security_policy()
    assert "unsafe-inline" not in content_security_policy("abc")
    assert "style-src 'self' 'nonce-abc'" in content_security_policy("abc")


def test_every_served_page_carries_a_nonce_and_no_unsafe_inline(tmp_path):
    app = create_app(tmp_path / "csp.db")
    with TestClient(app) as client:
        _seed(client)
        for path in ("/aegis", "/aegis/issues", "/aegis/boards", "/mentor", "/login"):
            response = client.get(path, headers=BROWSER)
            assert response.status_code == 200, path
            policy = response.headers["content-security-policy"]
            assert "unsafe-inline" not in policy, path
            assert "'nonce-" in policy, path


def test_the_nonce_is_fresh_per_response(tmp_path):
    """A predictable nonce is no nonce: injected markup could carry it."""
    app = create_app(tmp_path / "fresh.db")
    with TestClient(app) as client:
        _seed(client)
        seen = set()
        for _ in range(3):
            policy = client.get("/aegis", headers=BROWSER).headers[
                "content-security-policy"
            ]
            seen.add(policy.split("'nonce-")[1].split("'")[0])
        assert len(seen) == 3


def test_no_rendered_page_uses_an_inline_style_attribute(tmp_path):
    """The attribute form is what 'unsafe-inline' was licensing, so its absence is
    the actual acceptance criterion — a page that still emitted one would be broken
    by the policy, not protected by it."""
    app = create_app(tmp_path / "attrs.db")
    with TestClient(app) as client:
        _seed(client)
        client.post("/labels", json={"name": "bug", "color": "#ff0000"}, headers=H)
        issue = client.post("/issues", json={"title": "T", "body": "b"}, headers=H)
        issue_id = issue.json()["id"]
        client.post("/projects", json={"name": "P", "key": "P"}, headers=H)
        for path in (
            "/aegis",
            "/aegis/issues",
            "/aegis/boards",
            f"/aegis/issues/{issue_id}",
            "/aegis/labels",
            "/aegis/labels/bug",
            "/mentor",
            "/login",
        ):
            body = client.get(path, headers=BROWSER).text
            assert 'style="' not in body, f"{path} still renders a style attribute"


def test_a_label_colour_still_reaches_the_chip(tmp_path):
    """The arbitrary-value case. The stored hex has to survive the move from an
    attribute to a nonce'd rule, or every label silently renders grey."""
    app = create_app(tmp_path / "labels.db")
    with TestClient(app) as client:
        _seed(client)
        client.post("/labels", json={"name": "bug", "color": "#ff0000"}, headers=H)
        page = client.get("/aegis/labels", headers=BROWSER)
        nonce = (
            page.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
        )

        assert ".label-c-ff0000" in page.text  # the rule
        assert "--label-color: #ff0000" in page.text  # carrying the real colour
        assert 'class="label label-c-ff0000"' in page.text  # and the chip using it
        # The <style> element must carry THIS response's nonce, or the browser drops
        # it and the colour is lost.
        assert f'<style nonce="{nonce}">' in page.text


def test_the_rollup_bar_uses_a_stepped_width_class(tmp_path):
    """The bounded-number case, including the copy rendered outside any template
    (web/render.py builds embeds itself and so has no nonce to use)."""
    app = create_app(tmp_path / "rollup.db")
    with TestClient(app) as client:
        _seed(client)
        parent = client.post("/issues", json={"title": "P", "body": ""}, headers=H)
        pid = parent.json()["id"]
        child = client.post("/issues", json={"title": "C", "body": ""}, headers=H)
        client.patch(
            f"/issues/{child.json()['id']}", json={"parent_id": pid}, headers=H
        )
        body = client.get(f"/aegis/issues/{pid}", headers=BROWSER).text
        assert 'style="' not in body
        if "rollup-seg" in body:
            assert re.search(r"rollup-w-\d+", body), "bar lost its width class"


def test_the_stepped_classes_all_exist_in_the_stylesheet():
    """A class with no rule is a silently invisible bar or a flat tree."""
    from pathlib import Path

    css = (
        Path(__file__).resolve().parent.parent / "src/athena/static/styles.css"
    ).read_text()
    for percent in (0, 1, 33, 50, 99, 100):
        assert f".rollup-w-{percent} " in css
    for depth in range(10):
        assert f".doc-tree-depth-{depth} " in css
        assert f".tree-indent-{depth} " in css
