"""No page carries a script, a handler or a style its own policy blocks (U14).

WHY (COCKPIT-16): Mission Control's "Copy command" did nothing. Its script was
inline, and the page's content security policy is ``script-src 'self'``. Three
filter selects submitted through an inline ``onchange``, blocked the same
way, and every page load logged one blocked inline style: the one htmx adds
for its request indicator.

The policy stays as strict as it was. What a page needs is asked for with a
data attribute and done by ``static/confirm.js``; htmx is told to add no
style and evaluate nothing. A style that depends on data is still a ``<style>``
element carrying the response's nonce, as ``tests/test_csp_no_unsafe_inline.py``
describes; that is the one inline form the policy allows.

The first test reads every template. The second renders every page of a
seeded workspace that a signed-in admin can reach and checks what was sent.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

from fastapi.testclient import TestClient

from athena import agent_openapi, demo
from athena.main import AGENT_TRANSPORT, create_app

SRC = Path(__file__).resolve().parent.parent / "src" / "athena"
TEMPLATES = SRC / "templates"
NONCE = 'nonce="{{ request.state.csp_nonce }}"'


class _Inline(HTMLParser):
    """Collects what a content security policy without 'unsafe-inline' blocks:
    a script with no ``src``, an ``on*`` handler attribute, a ``style``
    attribute, and a ``<style>`` element (reported with its nonce, or None)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.style_nonces: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        names = dict(attrs)
        if tag == "script" and not names.get("src"):
            self.problems.append("an inline <script>")
        if tag == "style":
            self.style_nonces.append(names.get("nonce"))
        for name in names:
            if name.startswith("on"):
                self.problems.append(f"an inline {name} handler on <{tag}>")
            if name == "style":
                self.problems.append(f"a style attribute on <{tag}>")

    handle_startendtag = handle_starttag


def inline_problems(html: str, *, nonce: str | None) -> list[str]:
    parser = _Inline()
    parser.feed(html)
    problems = list(parser.problems)
    problems += [
        "a <style> without this response's nonce"
        for found in parser.style_nonces
        if nonce is None or found != nonce
    ]
    return problems


# --- the check itself -------------------------------------------------------------


def test_the_check_names_each_inline_form_and_passes_the_allowed_one():
    assert inline_problems('<script src="/static/x.js"></script>', nonce="n") == []
    assert inline_problems('<style nonce="n">a{}</style>', nonce="n") == []
    assert inline_problems("<script>alert(1)</script>", nonce="n") == [
        "an inline <script>"
    ]
    assert inline_problems('<select onchange="go()"></select>', nonce="n") == [
        "an inline onchange handler on <select>"
    ]
    assert inline_problems('<br onclick="go()"/>', nonce="n") == [
        "an inline onclick handler on <br>"
    ]
    assert inline_problems('<p style="color:red">x</p>', nonce="n") == [
        "a style attribute on <p>"
    ]
    for blocked in ("<style>a{}</style>", '<style nonce="other">a{}</style>'):
        assert inline_problems(blocked, nonce="n") == [
            "a <style> without this response's nonce"
        ]
    # Text that only looks like markup is not markup.
    assert inline_problems("<p>&lt;script&gt; onclick= style=</p>", nonce="n") == []


# --- every template ---------------------------------------------------------------


def test_no_template_carries_an_inline_script_handler_or_style():
    templates = sorted(TEMPLATES.rglob("*.html"))
    assert len(templates) > 40
    found = {}
    for path in templates:
        source = path.read_text(encoding="utf-8")
        problems = []
        # A <style> in a template must carry the response's nonce, spelled as
        # the templates spell it.
        for opening in re.findall(r"<style\b[^>]*>", source):
            if NONCE not in opening:
                problems.append(f"{opening} has no nonce")
        without_styles = re.sub(r"<style\b[^>]*>", "", source)
        problems += [
            problem
            for problem in inline_problems(without_styles, nonce=None)
            if "<style>" not in problem
        ]
        if problems:
            found[str(path.relative_to(TEMPLATES))] = problems
    assert found == {}


def test_the_behaviours_templates_ask_for_are_the_ones_the_script_provides():
    script = (SRC / "static" / "confirm.js").read_text(encoding="utf-8")
    asked = set()
    for path in TEMPLATES.rglob("*.html"):
        asked |= set(
            re.findall(
                r"\bdata-(confirm|autosubmit|copy-command)\b", path.read_text("utf-8")
            )
        )
    assert asked == {"confirm", "autosubmit", "copy-command"}
    # Each as the code that reads it, not as a mention in a comment.
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("//")
    )
    for reads in (
        'form.getAttribute("data-confirm")',
        'control.hasAttribute("data-autosubmit")',
        'target.closest("[data-copy-command]")',
        'button.getAttribute("data-copy-command")',
        "control.form.requestSubmit()",
        "navigator.clipboard.writeText(text)",
    ):
        assert reads in code, reads
    # Delegated from the document, so swapped-in markup behaves the same.
    assert script.count("document.addEventListener(") == 3
    assert "querySelectorAll" not in script

    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    config = re.search(r"<meta name=\"htmx-config\" content='([^']*)'>", base)
    assert config is not None
    import json

    assert json.loads(config.group(1)) == {
        "includeIndicatorStyles": False,
        "allowEval": False,
    }
    # The setting is read when htmx loads: it must come first.
    assert base.index('name="htmx-config"') < base.index("htmx.min.js")


# --- every page -------------------------------------------------------------------

_LINK = re.compile(r"""(?:href|hx-get|action)=["'](/[^"'#]*)""")
# Reads that are not pages, or that end the session the crawl runs in.
_SKIP = ("/static/", "/logout", "/attachments/", "/healthz", "/readyz", "/version")


#: The framework's own API-documentation pages, which Athena serves and did
#: not write. Each loads its script and stylesheet from a CDN and starts from
#: an inline script, all of which the policy blocks: they render blank in a
#: browser (found by this test, U14, and left for the owner: vendor the assets
#: or remove the routes). Remove an entry the day its page renders.
FRAMEWORK_PAGES = {"/docs", "/docs/oauth2-redirect", "/redoc"}


#: Browser reads that are not pages: data downloads, and the two halves of a
#: single sign-on redirect. Everything else the browser can GET must be
#: rendered by the crawl below, so a new page is checked without being listed.
NOT_PAGES = {
    "/admin/agents/runs/{run_id}/replay.json",
    "/admin/fleet.json",
    "/aegis/activity.csv",
    "/aegis/palette/actions",
    "/aegis/projects/{project_id}/floor.json",
    "/auth/callback",
    "/login/sso",
    "/mentor/spaces/{space_id}/export.html",
}


def _is_page(response) -> bool:
    # A download is a file, not a page: no policy applies to it once saved.
    return response.headers.get("content-type", "").startswith(
        "text/html"
    ) and not response.headers.get("content-disposition", "").startswith("attachment")


def _route_templates(app) -> list[tuple[re.Pattern[str], str]]:
    """Every GET route of the app, as a pattern over paths and its template."""
    found = []
    for route in agent_openapi.route_contexts(app):
        if "GET" not in (getattr(route, "methods", None) or ()):
            continue
        pattern = re.sub(r"\\\{[a-z_]+:path\\\}", ".+", re.escape(route.path))
        pattern = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", pattern)
        found.append((re.compile(pattern), route.path))
    return found


def _browser_templates(app) -> set[str]:
    """The template of every GET route the browser, not the API, is served."""
    return {
        route.path
        for route in agent_openapi.route_contexts(app)
        if "GET" in (getattr(route, "methods", None) or ())
        and agent_openapi.classify(
            route, openapi_url=app.openapi_url, transport=AGENT_TRANSPORT
        )
        == agent_openapi.BROWSER
    }


def test_every_page_a_signed_in_admin_reaches_is_free_of_inline_forms(tmp_path):
    seeded = demo.seed_demo(tmp_path / "demo.db", attach_dir=tmp_path / "attachments")
    app = create_app(tmp_path / "demo.db")
    with TestClient(app) as client:
        signed_in = client.post(
            "/login",
            data={"email": seeded["email"], "password": seeded["password"]},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303, signed_in.text
        # The seed has no label and no saved filter; their pages need one each.
        as_operator = {"X-Athena-Actor": str(seeded["ids"]["operator"])}
        for path, body in (
            ("/labels", {"name": "bug", "color": "#ff0000"}),
            ("/filters", {"name": "Open work"}),
        ):
            made = client.post(path, json=body, headers=as_operator)
            assert made.status_code == 201, made.text

        # Start from every route that takes no parameter, then follow links.
        queue = [
            path
            for _, path in _route_templates(app)
            if "{" not in path and not path.startswith(_SKIP)
        ]
        queue += ["/"]
        seen: set[str] = set()
        pages: dict[str, list[str]] = {}
        while queue and len(seen) < 1500:
            path = queue.pop()
            if path in seen or path.startswith(_SKIP):
                continue
            seen.add(path)
            response = client.get(path, headers={"Accept": "text/html"})
            if response.status_code != 200 or not _is_page(response):
                continue
            policy = response.headers["content-security-policy"]
            nonce = re.search(r"'nonce-([^']+)'", policy)
            assert nonce is not None, path
            pages[path] = inline_problems(response.text, nonce=nonce.group(1))
            for link in _LINK.findall(response.text):
                if link not in seen:
                    queue.append(link.replace("&amp;", "&"))

    blocked = {path: found for path, found in pages.items() if found}
    assert {
        path: found for path, found in blocked.items() if path not in FRAMEWORK_PAGES
    } == {}
    assert set(blocked) == FRAMEWORK_PAGES

    # The crawl is the test's reach: say what it rendered.
    rendered = {
        template
        for pattern, template in _route_templates(app)
        if any(pattern.fullmatch(path.split("?")[0]) for path in pages)
    }
    assert _browser_templates(app) - rendered == NOT_PAGES
    # The three pages that carried a handler, and the one that carried a script.
    for fixed in (
        "/admin/agents/runs",
        "/admin/run-controls",
        "/admin/security",
        "/aegis/activity/runs",
    ):
        assert fixed in rendered, fixed
