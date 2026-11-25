"""The cockpit obeys its own design system (U15, COCKPIT-17 to 20).

WHY: five defects measured in Chromium on 2026-11-25, each against
``docs/DESIGN_SYSTEM.md``:

- **No skip link.** A keyboard user crossed the app bar's rail toggle, brand,
  five navigation groups, the search box and the user menu before reaching the
  page. Every page now opens with a "Skip to content" link to ``#main``.
- **The default button wore the accent.** The design system spends phosphor
  lime on exactly one meaning, *an agent is waiting on the human*; a lime
  "Create Project" said the same thing as the Intervene badge. The default
  button is ink now (``--text`` fill, ``--surface`` label); the accent stays on
  the badge, the waiting-on-you chips and the in-review status.
- **Developer notes shipped.** The navigation printed each link's route
  (``/admin/agents``), pages closed with "Data from the Aegis issues API.", the
  new-issue form promised that "in production it will hit the real REST API",
  and the dashboard told the operator to "use REST or MCP". A template scan
  fails the next one.
- **Empty pages were dead ends.** "No projects yet." now says what to do and
  links to where it is done, for a viewer who may do it and not for one who may
  not.
- **Long names widened pages.** A 120-character one-word name, or a long
  sentence, pushed tables, headers, selects and the agents page sideways, and
  the navigation alone made every page scroll sideways at 390 px. The server
  cannot measure layout, so the last tests pin the CSS contract the Chromium
  measurement relies on (scratch script ``measure.js``; see the U15 report).
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

from fastapi.testclient import TestClient

from athena import agent_openapi, demo
from athena.main import AGENT_TRANSPORT, create_app
from support import ADMIN, add_user, bootstrap, login, make_app

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "athena"
TEMPLATES = SRC / "templates"
STYLES = SRC / "static" / "styles.css"
DESIGN = REPO / "docs" / "DESIGN_SYSTEM.md"


# --- a stylesheet reader ------------------------------------------------------


def css_rules(css: str | None = None) -> list[tuple[list[str], dict[str, str]]]:
    """Every innermost rule of the stylesheet as (selectors, declarations).

    Comments are stripped first, so a rule kept only in a comment counts for
    nothing. A rule inside ``@media`` is read like any other.
    """
    css = STYLES.read_text(encoding="utf-8") if css is None else css
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        declarations = {}
        for part in body.split(";"):
            if ":" in part:
                name, value = part.split(":", 1)
                declarations[name.strip()] = value.strip()
        selectors = [s.strip() for s in selector.split(",") if s.strip()]
        rules.append((selectors, declarations))
    return rules


def declared(selector: str, prop: str, css: str | None = None) -> list[str]:
    """Every value ``prop`` takes in a rule naming exactly ``selector``."""
    return [
        decl[prop]
        for selectors, decl in css_rules(css)
        if selector in selectors and prop in decl
    ]


def test_the_stylesheet_reader_reads_rules_and_ignores_comments():
    css = "/* .x { color: red } */ .a, .b { color: var(--text); gap: 2px }"
    css += " @media (max-width: 9px) { .a { gap: 0 } }"
    assert declared(".a", "color", css) == ["var(--text)"]
    assert declared(".b", "color", css) == ["var(--text)"]
    assert declared(".a", "gap", css) == ["2px", "0"]
    assert declared(".x", "color", css) == []


# --- the default button is not the accent --------------------------------------

#: The accent tokens. Each means "an agent is waiting on the human".
ACCENT = re.compile(r"var\(--accent(?:-text|-ink|-soft|-line)?\)")


def test_the_default_button_is_ink_not_the_accent():
    """A primary action is the operator's own act, and "human actors get no
    colour": the default button carries no hue at all."""
    for selector in (".button", ".button:hover"):
        for selectors, decl in css_rules():
            if selector in selectors:
                for prop, value in decl.items():
                    assert not ACCENT.search(value), (selector, prop, value)
    assert declared(".button", "background") == ["var(--text)"]
    assert declared(".button", "color") == ["var(--surface)"]
    assert declared(".button:hover", "color") == ["var(--surface)"]


def test_the_accent_still_marks_what_waits_on_the_human():
    """Moving the button off the accent must not move the accent off its three
    semantic places: the Intervene badge, the waiting-on-you chip (which the
    in-review status and the attention links use) and the board's accent lane."""
    assert declared(".nav-count", "background") == ["var(--accent)"]
    assert declared(".nav-count", "color") == ["var(--accent-ink)"]
    assert declared('.chip[data-tone="accent"]', "--tone-soft") == [
        "var(--accent-soft)"
    ]
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'class="chip" data-tone="accent"' in base


def test_the_design_system_names_the_primary_button_colour():
    text = DESIGN.read_text(encoding="utf-8")
    section = text.split("## The default button", 1)
    assert len(section) == 2, "DESIGN_SYSTEM.md has no 'The default button' section"
    body = section[1].split("\n## ", 1)[0]
    for token in ("--text", "--surface", "--accent"):
        assert f"`{token}`" in body, token


# --- the skip link -------------------------------------------------------------

#: Elements a keyboard reaches with Tab, as a browser decides it by default.
_FOCUSABLE = {"button", "select", "textarea", "summary", "iframe"}


class _FirstFocusable(HTMLParser):
    """Finds the first element in <body> that Tab reaches, and every id."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.first: tuple[str, dict[str, str | None]] | None = None
        self.ids: dict[str, str] = {}
        self._capture = False
        self.first_text = ""

    def handle_starttag(self, tag, attrs):
        names = dict(attrs)
        if names.get("id"):
            self.ids[names["id"] or ""] = tag
        if tag == "body":
            self.in_body = True
            return
        if not self.in_body or self.first is not None:
            return
        tabindex = names.get("tabindex")
        focusable = (
            (tag == "a" and names.get("href") is not None)
            or (tag == "input" and names.get("type") != "hidden")
            or tag in _FOCUSABLE
            or (tabindex is not None and tabindex != "-1")
        )
        if focusable:
            self.first = (tag, names)
            self._capture = True

    def handle_endtag(self, tag):
        if self._capture and tag == (self.first or ("",))[0]:
            self._capture = False

    def handle_data(self, data):
        if self._capture:
            self.first_text += data


def skip_link_problems(html: str) -> list[str]:
    parser = _FirstFocusable()
    parser.feed(html)
    if parser.first is None:
        return ["nothing on the page is focusable"]
    tag, attrs = parser.first
    problems = []
    if tag != "a" or "skip-link" not in (attrs.get("class") or "").split():
        problems.append(f"the first focusable element is <{tag} {attrs}>")
        return problems
    if parser.first_text.strip() != "Skip to content":
        problems.append(f"the skip link reads {parser.first_text.strip()!r}")
    target = (attrs.get("href") or "").removeprefix("#")
    if not (attrs.get("href") or "").startswith("#") or not target:
        problems.append(f"the skip link points at {attrs.get('href')!r}")
    elif parser.ids.get(target) != "main":
        problems.append(f"#{target} is {parser.ids.get(target)!r}, not <main>")
    return problems


def test_the_skip_link_check_names_each_failure():
    good = (
        '<body><a class="skip-link" href="#main">Skip to content</a>'
        '<button>x</button><main id="main" tabindex="-1"></main></body>'
    )
    assert skip_link_problems(good) == []
    assert skip_link_problems(
        '<body><button>x</button><a class="skip-link" href="#main">Skip to content'
        '</a><main id="main"></main></body>'
    ) == ["the first focusable element is <button {}>"]
    # The brand link was the first thing Tab reached before U15: a link, but
    # not the skip link.
    assert skip_link_problems(
        '<body><a href="/">Athena</a><main id="main"></main></body>'
    ) == ["the first focusable element is <a {'href': '/'}>"]
    assert skip_link_problems(
        '<body><a class="skip-link" href="#nope">Skip to content</a></body>'
    ) == ["#nope is None, not <main>"]
    assert skip_link_problems(
        '<body><input type="hidden"><a class="skip-link" href="#m">Skip to content'
        '</a><div id="m"></div></body>'
    ) == ["#m is 'div', not <main>"]


#: Browser reads that are not pages (data downloads, the sign-on redirect and
#: the framework's own documentation pages, which Athena did not write).
_NOT_PAGES = ("/static/", "/logout", "/attachments/", "/healthz", "/readyz")
_NOT_PAGES += ("/version", "/docs", "/redoc", "/login/sso", "/auth/callback")
_LINK = re.compile(r"""(?:href|action)=["'](/[^"'#]*)""")


def _route_patterns(app) -> list[tuple[re.Pattern[str], str]]:
    """Every GET route the browser (not the API) is served, as a path pattern."""
    found = []
    for route in agent_openapi.route_contexts(app):
        if "GET" not in (getattr(route, "methods", None) or ()):
            continue
        kind = agent_openapi.classify(
            route, openapi_url=app.openapi_url, transport=AGENT_TRANSPORT
        )
        if kind != agent_openapi.BROWSER:
            continue
        pattern = re.sub(r"\\\{[a-z_]+:path\\\}", ".+", re.escape(route.path))
        pattern = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", pattern)
        found.append((re.compile(pattern), route.path))
    return found


def test_every_page_opens_with_a_skip_link_to_its_main_region(tmp_path):
    """Crawled like ``test_no_inline_script_or_handler``: every page a signed-in
    admin reaches, two per route, plus the signed-out sign-in page and the
    shared refusal page."""
    seeded = demo.seed_demo(tmp_path / "demo.db", attach_dir=tmp_path / "attachments")
    app = create_app(tmp_path / "demo.db")
    patterns = _route_patterns(app)
    problems: dict[str, list[str]] = {}
    with TestClient(app) as client:
        signed_out = client.get("/login")
        problems["/login (signed out)"] = skip_link_problems(signed_out.text)
        signed_in = client.post(
            "/login",
            data={"email": seeded["email"], "password": seeded["password"]},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303, signed_in.text
        refused = client.get("/aegis/issues/999999", headers={"Accept": "text/html"})
        assert refused.status_code == 404
        problems["refused page"] = skip_link_problems(refused.text)

        queue = [path for _, path in patterns if "{" not in path] + ["/"]
        seen: set[str] = set()
        per_route: dict[str, int] = {}
        pages = 0
        while queue:
            path = queue.pop()
            if path in seen or path.startswith(_NOT_PAGES):
                continue
            seen.add(path)
            route = next(
                (r for p, r in patterns if p.fullmatch(path.split("?")[0])), None
            )
            if route is None or per_route.get(route, 0) >= 2:
                continue
            response = client.get(path, headers={"Accept": "text/html"})
            if response.status_code != 200 or not response.headers.get(
                "content-type", ""
            ).startswith("text/html"):
                continue
            if response.headers.get("content-disposition", "").startswith("attachment"):
                continue
            per_route[route] = per_route.get(route, 0) + 1
            pages += 1
            problems[path] = skip_link_problems(response.text)
            queue += [
                link.replace("&amp;", "&") for link in _LINK.findall(response.text)
            ]
    assert {path: found for path, found in problems.items() if found} == {}
    assert pages > 60, pages


def test_the_skip_link_is_hidden_until_focused_and_main_takes_focus():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '<main class="work" id="main" tabindex="-1">' in base
    # Off-screen until focused, on-screen when focused: never display:none,
    # which would take it out of the tab order too.
    assert declared(".skip-link", "position") == ["absolute"]
    assert declared(".skip-link:not(:focus)", "clip-path") == ["inset(50%)"]
    assert declared(".skip-link", "display") == []
    assert declared(".skip-link:not(:focus)", "display") == []


# --- developer notes do not ship -------------------------------------------------

#: Text written for developers, as it appeared in the interface before U15.
#: Each pattern names the kind of note; a new one of the kind fails.
DEVELOPER_NOTES = {
    "a route path": re.compile(
        r"(?<![\w.:/-])/(?:aegis|admin|mentor|settings|inbox|find|users|issues|"
        r"projects|spaces|pages|tokens|labels|filters|webhooks|activity|runs|"
        r"agents|fleet)\b"
    ),
    "a raw HTTP call": re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE) /"),
    "where the data came from": re.compile(
        r"\bfrom the (?:\w+ ){0,3}(?:API|projection)\b"
    ),
    "an implementation name": re.compile(r"\b(?:REST|HTMX|htmx|Jinja)\b"),
    "a promise about production": re.compile(r"\bin production\b", re.I),
    "a requirement id": re.compile(
        r"\b(?:COCKPIT|FLEET|PERF|OPS|MWS)-\d+\b|\b[UCD]\d{1,3}\b"
    ),
    "a work note": re.compile(r"\b(?:TODO|FIXME|XXX)\b"),
}

#: Attributes whose value a person reads (or hears).
_READ_ATTRIBUTES = ("title", "placeholder", "aria-label", "alt")


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[tuple[int, str]] = []
        self._hidden = 0
        self.classes: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._hidden += 1
        for name, value in attrs:
            if name == "class" and value:
                self.classes.update(value.split())
            if name in _READ_ATTRIBUTES and value:
                self.text.append((self.getpos()[0], value))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden and data.strip():
            self.text.append((self.getpos()[0], data.strip()))


def visible_text(source: str) -> _VisibleText:
    """What a template shows: Jinja comments are not shipped, and a Jinja
    expression is data, not copy, so each is blanked (keeping line numbers)."""

    def keep_lines(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    source = re.sub(r"\{#.*?#\}", keep_lines, source, flags=re.S)
    source = re.sub(r"\{%.*?%\}", keep_lines, source, flags=re.S)
    source = re.sub(r"\{\{.*?\}\}", " ", source, flags=re.S)
    parser = _VisibleText()
    parser.feed(source)
    return parser


def developer_notes(source: str) -> list[str]:
    found = []
    for line, text in visible_text(source).text:
        for kind, pattern in DEVELOPER_NOTES.items():
            if pattern.search(text):
                found.append(f"line {line}: {kind}: {text[:80]!r}")
    return found


def test_the_note_scan_finds_each_kind_and_passes_real_content():
    assert developer_notes('<a href="/aegis">Issues <span>/aegis</span></a>') == [
        "line 1: a route path: '/aegis'"
    ]
    assert developer_notes("<p>Data from the Aegis issues API.</p>") == [
        "line 1: where the data came from: 'Data from the Aegis issues API.'"
    ]
    assert developer_notes("<p>walk GET /activity/chain/verify</p>")[0].startswith(
        "line 1: a route path"
    )
    assert developer_notes('<input placeholder="ref (MWS-18, 42)">') == [
        "line 1: a requirement id: 'ref (MWS-18, 42)'"
    ]
    # Real content is not a note: an issue key, a run id, a command the
    # operator runs, a Jinja comment, an expression's value, an href.
    for fine in (
        "<p>Short prefix for issue keys, e.g. <code>WEB-1</code>.</p>",
        "<p>run <code>athena-doctor</code> for the full claim</p>",
        "{# Restore via POST /users/{id}/restore (U5) #}<p>Restore</p>",
        '<a href="/admin/agents">{{ "/admin/agents" }}Agents</a>',
        "<p>demo-sol-run-001 · API tokens for you</p>",
    ):
        assert developer_notes(fine) == [], fine


def test_no_template_shows_a_developer_note():
    templates = sorted(TEMPLATES.rglob("*.html"))
    assert len(templates) > 40
    found = {
        str(path.relative_to(TEMPLATES)): notes
        for path in templates
        if (notes := developer_notes(path.read_text(encoding="utf-8")))
    }
    assert found == {}


def test_the_navigation_prints_no_routes():
    """The nav once printed each link's path beside its name. The class that
    styled it is gone, so a template that brings it back has no rule either."""
    for path in TEMPLATES.rglob("*.html"):
        assert "route" not in visible_text(path.read_text("utf-8")).classes, path
    assert not any(
        ".route" in selector for selectors, _ in css_rules() for selector in selectors
    )


# --- an empty page says what to do next -------------------------------------------

_EMPTY = re.compile(
    r'<(p|div) class="empty-state"[^>]*>(.*?)</\1>', flags=re.S | re.IGNORECASE
)


def empty_state(html: str) -> str:
    found = _EMPTY.findall(html)
    assert len(found) == 1, (found, re.findall(r"<title>(.*?)</title>", html))
    return found[0][1]


def links(fragment: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', fragment)


def _cast(client: TestClient) -> None:
    """An admin (1), a member (2) and a viewer (3), and nothing else."""
    bootstrap(client, email="a@e.com", name="Ann")
    add_user(client, "m@e.com", name="Max Member")
    add_user(client, "v@e.com", name="Vi Viewer", role="viewer")


def _next_step(client, path: str) -> list[str]:
    """The links in the page's one empty state (``client`` is signed in)."""
    response = client.get(path)
    assert response.status_code == 200, (path, response.text[:300])
    hrefs = links(empty_state(response.text))
    for href in hrefs:
        if href.startswith("#"):
            # The link lands on something: the form it names is on the page.
            assert f'id="{href[1:]}"' in response.text, (path, href)
    return hrefs


#: (page, the next step a writer is offered). A viewer is offered none of them.
WRITER_NEXT_STEPS = {
    "/aegis/issues": "/aegis/issues/new",
    "/aegis/boards": "/aegis/issues/new",
    "/aegis/projects": "#new-project",
    "/mentor": "#new-space",
    "/aegis/labels": "/aegis/issues",
    "/settings/tokens": "#new-token",
}


def test_an_empty_page_offers_its_next_step_to_whoever_may_take_it(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _cast(client)
        login(client, "m@e.com")
        for path, step in WRITER_NEXT_STEPS.items():
            assert step in _next_step(client, path), path
        login(client, "v@e.com")
        for path in WRITER_NEXT_STEPS:
            assert _next_step(client, path) == [], path
        # A space with no pages yet, now that the empty Mentor page was read.
        space = client.post(
            "/spaces", json={"key": "ENG", "name": "Engineering"}, headers=ADMIN
        )
        assert space.status_code == 201, space.text
        path = f"/mentor/spaces/{space.json()['id']}"
        assert _next_step(client, path) == []
        login(client, "m@e.com")
        assert _next_step(client, path) == ["#new-page"]


def test_a_next_step_anyone_may_take_is_offered_to_everyone(tmp_path):
    """Saving a filter, watching an issue and clearing a filter are reads."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _cast(client)
        for email in ("m@e.com", "v@e.com"):
            login(client, email)
            assert "#new-filter" in _next_step(client, "/aegis/filters")
            assert "/aegis/issues" in _next_step(client, "/inbox")
            for path, clear in (
                ("/aegis/issues?search=nothing", "/aegis/issues"),
                ("/aegis/activity?verb=nothing", "/aegis/activity"),
                ("/aegis/boards?search=nothing", "/aegis/boards"),
            ):
                assert _next_step(client, path) == [clear], (email, path)


def test_the_admin_pages_point_at_their_own_forms(tmp_path):
    """Only an admin reaches these pages, so the page is the gate."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        _cast(client)
        project = client.post(
            "/projects", json={"name": "Web", "key": "WEB"}, headers=ADMIN
        )
        assert project.status_code == 201, project.text
        login(client, "a@e.com")
        for path, step in (
            ("/admin/agents", "#onboard"),
            ("/admin/webhooks", "#new-webhook"),
            ("/admin/automation", "#new-rule"),
            (f"/aegis/projects/{project.json()['id']}/sprints", "#new-sprint"),
        ):
            assert step in _next_step(client, path), path


# --- a long name does not widen the page -----------------------------------------
#
# The proof is in Chromium: every page a signed-in admin reaches, with a
# 120-character one-word name and a long sentence seeded as an issue title,
# project, space, page, agent, user, label and filter, at 390 px and 1280 px,
# measured document.documentElement.scrollWidth against window.innerWidth
# (109 of 109 pages scrolled sideways at 390 px before, 37 at 1280 px; none
# after). These tests pin the rules that measurement relies on.


def test_every_table_scrolls_inside_its_own_scroller():
    """A wide table may scroll; the page may not. A table straight after its
    scroller is the contract, so a new bare table fails here."""
    bare = []
    tables = 0
    for path in sorted(TEMPLATES.rglob("*.html")):
        lines = [line.strip() for line in path.read_text("utf-8").splitlines()]
        for number, line in enumerate(lines):
            if line.startswith("<table"):
                tables += 1
                if number == 0 or lines[number - 1] != '<div class="table-scroll">':
                    bare.append(f"{path.relative_to(TEMPLATES)}:{number + 1}")
    assert tables > 25
    assert bare == []
    assert declared(".table-scroll", "overflow-x") == ["auto"]
    # A visually hidden label inside a scrolled table is clipped by the
    # scroller, not measured against the page.
    assert declared(".table-scroll", "position") == ["relative"]


def test_a_long_word_breaks_instead_of_widening_its_box():
    # The work region: names in headers, cards, rows, rails and definitions.
    assert declared(".work", "overflow-wrap") == ["anywhere"]
    assert declared(".work", "min-width") == ["0"]
    # Tables keep words whole and break only the name cell, above a floor.
    assert declared(".issues-table", "overflow-wrap") == ["normal"]
    for cell in (
        ".issues-table td:has(> a)",
        ".issues-table td:has(> strong)",
        ".issues-table .issue-title",
        ".issues-table .cell-wrap",
    ):
        assert declared(cell, "overflow-wrap") == ["anywhere"], cell
        assert declared(cell, "min-width") == ["8rem"], cell
    assert declared(".issue-id", "white-space") == ["nowrap"]


def test_controls_and_headers_shrink_to_the_page():
    # A select is as wide as its longest option, which can be a long name.
    assert declared("select", "max-width") == ["100%"]
    assert declared("select", "min-width") == ["0"]
    # Either side of a page header may shrink below its widest control.
    assert declared(".page-header > *", "min-width") == ["0"]
    # The navigation wraps instead of widening the bar (490 px at 390 px), and
    # on a narrow screen an open menu is a sheet under the bar.
    assert declared(".nav", "flex-wrap") == ["wrap"]
    assert "static" in declared(".nav-group", "position")
    assert declared(".nav-menu", "right") == ["var(--sp-2)"]
    # A hidden label at the right edge of a flex-end row stuck out by its
    # -1px margin and scrolled the board sideways by one pixel.
    assert declared(".visually-hidden", "margin") == ["0"]
