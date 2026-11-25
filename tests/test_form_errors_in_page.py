"""A refused form answers inside the application, and a comment survives a sign-in.

WHY (COCKPIT-13, U11): a refused plain form post answered with a fragment such
as ``<div class="error">Label name is required.</div>`` and nothing else. The
browser showed one unstyled line with no stylesheet and no navigation, and the
person had lost the page they were on. And a comment written while the session
expired was gone: the sign-in answer said "please sign in" and dropped the text.

Now a validation refusal of a form on a page with a context builder re-renders
that page with the error and what was typed (tier one); every other refusal is
the shared page inside the shell (tier two, ``web/form_errors.py``). htmx still
receives the fragment it swaps. Hidden and missing objects still answer alike.
A comment refused for want of a session is shown back, escaped, and the page
carries the markers ``static/drafts.js`` reads to keep the draft in the box.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from support import (
    ADMIN,
    add_user,
    bootstrap,
    declare_seat,
    login,
    make_app,
    onboard_agent,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "athena"
WEB = SRC / "web"

#: What a page inside the shell carries (templates/base.html).
STYLESHEET = '<link rel="stylesheet" href="/static/styles.css'
NAVIGATION = '<nav class="nav" aria-label="Primary">'
HTMX = {"HX-Request": "true"}


def _in_shell(text: str) -> bool:
    return (
        text.lstrip().startswith("<!DOCTYPE html>")
        and STYLESHEET in text
        and NAVIGATION in text
    )


@pytest.fixture
def world(tmp_path):
    """An admin and a member signed in to the browser, an anonymous browser, and
    one of everything a refused form is posted from: an issue with a visible
    project, a hidden issue the member cannot see, a space and a page (with a
    child), a hidden page, and a saved filter's page."""
    app, _db = make_app(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as member,
        TestClient(app) as anonymous,
    ):
        bootstrap(admin)
        add_user(admin, "m@e.com", name="Mel")
        login(admin, "a@e.com")
        login(member, "m@e.com")

        project = admin.post(
            "/projects", json={"key": "OPS", "name": "Ops"}, headers=ADMIN
        ).json()
        issue = admin.post(
            "/issues",
            json={"title": "Fix the boiler", "project_id": project["id"]},
            headers=ADMIN,
        ).json()
        secret = admin.post(
            "/projects", json={"key": "SEC", "name": "Secret"}, headers=ADMIN
        ).json()
        admin.put(
            f"/projects/{secret['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        hidden_issue = admin.post(
            "/issues",
            json={"title": "Classified", "project_id": secret["id"]},
            headers=ADMIN,
        ).json()

        space = admin.post(
            "/spaces", json={"key": "DOC", "name": "Handbook"}, headers=ADMIN
        ).json()
        page = admin.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Runbook", "body": "Steps."},
            headers=ADMIN,
        ).json()
        admin.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Child", "body": "", "parent_id": page["id"]},
            headers=ADMIN,
        )
        private_space = admin.post(
            "/spaces", json={"key": "PRV", "name": "Private"}, headers=ADMIN
        ).json()
        admin.put(
            f"/spaces/{private_space['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        hidden_page = admin.post(
            f"/spaces/{private_space['id']}/pages",
            json={"title": "Sealed", "body": "x"},
            headers=ADMIN,
        ).json()

        agent = onboard_agent(admin)["user"]
        declare_seat(admin, "taken", email=None, name="Taken seat")

        yield SimpleNamespace(
            app=app,
            admin=admin,
            member=member,
            anonymous=anonymous,
            issue=issue["id"],
            hidden_issue=hidden_issue["id"],
            space=space["id"],
            page=page["id"],
            hidden_page=hidden_page["id"],
            agent=agent["id"],
        )


@dataclass(frozen=True)
class Refused:
    """One refused post: where, with what, and what it answered before U11."""

    name: str
    path: Callable[[SimpleNamespace], str]
    data: dict[str, str]
    status: int
    wording: str  # the refusal's words, exactly as the old fragment said them
    heading: str | None = None  # tier one: the posting page's heading
    typed: str | None = None  # tier one: a value the person typed, kept
    who: str = "admin"
    # The error slot carries data-refused, which static/drafts.js reads. Every
    # page with a comment box marks it; an agent's own section does not.
    marked: bool = True


TIER_ONE = [
    Refused(
        "issue comment, empty",
        lambda w: f"/aegis/issues/{w.issue}/comments",
        {"body": "   "},
        400,
        "Comment cannot be empty.",
        heading="Fix the boiler",
    ),
    Refused(
        "issue label, no name",
        lambda w: f"/aegis/issues/{w.issue}/labels",
        {"name": "  "},
        400,
        "Label name is required.",
        heading="Fix the boiler",
    ),
    Refused(
        "issue link, unknown target",
        lambda w: f"/aegis/issues/{w.issue}/links",
        {"target_ref": "NOPE-404", "relation": "relates"},
        400,
        "no such target issue",
        heading="Fix the boiler",
        typed='value="NOPE-404"',
    ),
    Refused(
        "issue parent, unknown ref",
        lambda w: f"/aegis/issues/{w.issue}/parent",
        {"parent_ref": "NOPE-77"},
        400,
        "No such parent issue.",
        heading="Fix the boiler",
        typed='value="NOPE-77"',
    ),
    Refused(
        "issue contributor, none picked",
        lambda w: f"/aegis/issues/{w.issue}/contributors",
        {"user_id": ""},
        400,
        "Pick a user.",
        heading="Fix the boiler",
    ),
    Refused(
        "issue edit, blank title",
        lambda w: f"/aegis/issues/{w.issue}/edit",
        {"title": "   ", "body": "The typed description survives."},
        400,
        "Title cannot be empty.",
        heading="Edit OPS-1",
        typed="The typed description survives.",
    ),
    Refused(
        "page comment, empty",
        lambda w: f"/mentor/pages/{w.page}/comments",
        {"body": "   "},
        400,
        "Comment cannot be empty.",
        heading="Runbook",
    ),
    Refused(
        "page label, no name",
        lambda w: f"/mentor/pages/{w.page}/labels",
        {"name": " "},
        400,
        "Label name is required.",
        heading="Runbook",
    ),
    Refused(
        "page move, bad parent",
        lambda w: f"/mentor/pages/{w.page}/move",
        {"parent_id": "abc"},
        400,
        "Invalid parent page.",
        heading="Runbook",
    ),
    Refused(
        "page delete, has children",
        lambda w: f"/mentor/pages/{w.page}/delete",
        {},
        409,
        "Move or delete its child pages first.",
        heading="Runbook",
    ),
    Refused(
        "page edit, blank title",
        lambda w: f"/mentor/pages/{w.page}/edit",
        {"title": "  ", "body": "A long body the author typed."},
        400,
        "Title is required.",
        heading="Edit page",
        typed="A long body the author typed.",
    ),
    Refused(
        "space new page, blank title",
        lambda w: f"/mentor/spaces/{w.space}/pages",
        {"title": " ", "body": "Body typed into the new page.", "parent_id": ""},
        400,
        "Page title is required.",
        heading="Handbook",
        typed="Body typed into the new page.",
    ),
    Refused(
        "space new page, foreign parent",
        lambda w: f"/mentor/spaces/{w.space}/pages",
        {"title": "Typed title", "body": "", "parent_id": "999999"},
        400,
        "Parent must be a page in this space.",
        heading="Handbook",
        typed='value="Typed title"',
    ),
    Refused(
        "space edit, blank key",
        lambda w: f"/mentor/spaces/{w.space}/edit",
        {"key": " ", "name": "Typed space name", "description": ""},
        400,
        "Space key is required.",
        heading="Edit space",
        typed='value="Typed space name"',
    ),
    Refused(
        "filter, no name",
        lambda w: "/aegis/filters",
        {"name": " ", "search": "typed search words"},
        400,
        "Filter name is required.",
        heading="Saved filters",
        typed='value="typed search words"',
    ),
    Refused(
        "filter, bad priority",
        lambda w: "/aegis/filters",
        {"name": "My typed filter", "priority": "urgentest"},
        400,
        "no such priority.",
        heading="Saved filters",
        typed='value="My typed filter"',
    ),
]

TIER_ONE += [
    Refused(
        "fleet seat, slug taken",
        lambda w: "/admin/fleet/seats",
        {"slug": "taken", "name": "Typed seat name", "kind": "seat"},
        409,
        "seat taken is already declared",
        heading="Fleet roster",
        typed='value="Typed seat name"',
    ),
    Refused(
        "agent token, no scope",
        lambda w: f"/admin/agents/{w.agent}/tokens",
        {"token_name": "typed-token-label"},
        400,
        "scope",
        heading="Agents",
        typed='value="typed-token-label"',
        marked=False,
    ),
]

TIER_TWO = [
    Refused(
        "issue sprint, not a number",
        lambda w: f"/aegis/issues/{w.issue}/sprint",
        {"sprint_id": "abc"},
        400,
        '<div class="error">No such sprint.</div>',
    ),
    Refused(
        "issue comment, signed out",
        lambda w: f"/aegis/issues/{w.issue}/comments",
        {"body": "x"},
        401,
        '<div class="blocked">Please <a href="/login">sign in</a> to comment.</div>',
        who="anonymous",
    ),
    Refused(
        "page comment, signed out",
        lambda w: f"/mentor/pages/{w.page}/comments",
        {"body": "x"},
        401,
        '<div class="blocked">Please <a href="/login">sign in</a> to comment.</div>',
        who="anonymous",
    ),
    Refused(
        "admin tool, member",
        lambda w: "/admin/webhooks",
        {"url": "https://example.com/hook"},
        403,
        '<div class="blocked">Admin role required.</div>',
        who="member",
    ),
    Refused(
        "user agent flag, no such user",
        lambda w: "/admin/users/999999/agent",
        {"is_agent": "1"},
        404,
        '<div class="error">No such user.</div>',
    ),
    Refused(
        "automation rule, no such rule",
        lambda w: "/admin/automation/999999/delete",
        {},
        404,
        '<div class="error">No such rule.</div>',
    ),
    Refused(
        "filter delete, no such filter",
        lambda w: "/aegis/filters/999999/delete",
        {},
        404,
        '<div class="error">No such filter.</div>',
    ),
    Refused(
        "issue create, bad project",
        lambda w: "/aegis/issues",
        {"title": "t", "project_id": "abc"},
        400,
        '<div class="error">No such project.</div>',
    ),
    Refused(
        "page comment, hidden page",
        lambda w: f"/mentor/pages/{w.hidden_page}/comments",
        {"body": "x"},
        404,
        '<div class="error">Page not found.</div>',
        who="member",
    ),
    Refused(
        "issue label, hidden issue, no name",
        lambda w: f"/aegis/issues/{w.hidden_issue}/labels",
        {"name": " "},
        400,
        '<div class="error">Label name is required.</div>',
        who="member",
    ),
]


def _toggles_come_back_to_a_page(client: TestClient, text: str) -> None:
    """The rail and theme toggles in the shell send the browser back to a page
    after the toggle. Back to the refused POST address, a GET would be a 405."""
    for target in re.findall(r'name="next" value="([^"]*)"', text):
        assert client.get(target, follow_redirects=False).status_code != 405, target


def _post(world, row: Refused, headers: dict[str, str] | None = None):
    client = getattr(world, row.who)
    return client.post(
        row.path(world), data=row.data, headers=headers or {}, follow_redirects=False
    )


@pytest.mark.parametrize("row", TIER_ONE, ids=lambda row: row.name)
def test_a_validation_refusal_renders_in_the_page_that_posted_it(world, row):
    answer = _post(world, row)
    assert answer.status_code == row.status
    assert _in_shell(answer.text), "a refused form must render inside the shell"
    assert row.wording in answer.text
    assert row.heading is not None and row.heading in answer.text, (
        "the posting page is rebuilt, not replaced by a generic one"
    )
    if row.marked:
        assert "data-refused" in answer.text
    _toggles_come_back_to_a_page(getattr(world, row.who), answer.text)
    if row.typed is not None:
        assert row.typed in answer.text, "what the person typed is still there"


@pytest.mark.parametrize("row", TIER_TWO, ids=lambda row: row.name)
def test_any_other_refusal_is_the_shared_page_inside_the_shell(world, row):
    answer = _post(world, row)
    assert answer.status_code == row.status
    assert _in_shell(answer.text)
    assert row.wording in answer.text, "the refusal's words are unchanged"
    assert 'data-refused="' in answer.text
    assert 'class="button secondary">Back to' in answer.text
    _toggles_come_back_to_a_page(getattr(world, row.who), answer.text)


def test_a_get_refusal_is_inside_the_shell_too(world):
    answer = world.admin.get("/mentor/pages/999999")
    assert answer.status_code == 404
    assert _in_shell(answer.text)
    assert '<div class="error">Page not found.</div>' in answer.text


@pytest.mark.parametrize(
    "row", [*TIER_ONE[:3], TIER_ONE[6], *TIER_TWO], ids=lambda row: row.name
)
def test_htmx_still_receives_the_fragment_it_swaps(world, row):
    """A request htmx sent swaps its answer into the page it is on: wrapping it
    in a whole document would nest a page inside a page."""
    answer = _post(world, row, headers=HTMX)
    assert answer.status_code == row.status
    assert "<html" not in answer.text.lower()
    # Byte for byte the fragment the route answered before U11.
    fragment = (
        row.wording
        if row.wording.startswith("<")
        else f'<div class="error">{row.wording}</div>'
    )
    assert answer.text == fragment


@pytest.mark.parametrize(
    "path",
    ["/aegis/issues/preview?source_id={id}", "/mentor/pages/preview?source_id={id}"],
)
def test_the_fragment_routes_answer_htmx_with_a_fragment(world, path):
    """The editors' preview panes are filled by htmx: a refusal there stays a
    fragment, and is identical for a hidden and a missing source."""
    hidden_id = world.hidden_issue if "aegis" in path else world.hidden_page
    hidden = world.member.post(
        path.format(id=hidden_id), data={"body": "x"}, headers=HTMX
    )
    missing = world.member.post(
        path.format(id=999999), data={"body": "x"}, headers=HTMX
    )
    assert hidden.status_code == missing.status_code == 404
    assert hidden.text == missing.text
    assert hidden.text.startswith("<div class=")


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "/mentor/pages/{page}", None),
        ("get", "/aegis/issues/{issue}/work-context", None),
        ("post", "/aegis/issues/{issue}/comments", {"body": "hello"}),
        ("post", "/aegis/issues/{issue}/labels", {"name": " "}),
        ("post", "/aegis/issues/{issue}/parent", {"parent_ref": "NOPE-1"}),
        ("post", "/aegis/issues/{issue}/contributors", {"user_id": ""}),
        ("post", "/mentor/pages/{page}/comments", {"body": " "}),
        ("post", "/mentor/pages/{page}/labels", {"name": " "}),
        ("post", "/mentor/spaces/{space}/pages", {"title": " ", "body": "b"}),
        ("post", "/mentor/spaces/{space}/edit", {"key": "", "name": "n"}),
    ],
)
def test_a_hidden_object_and_a_missing_one_still_answer_alike(
    world, method, path, data
):
    """Visibility is frozen: hidden reads as missing. The refusal page reads the
    request, never the object, so the two answers are the same bytes."""
    private_space = world.admin.get(f"/pages/{world.hidden_page}", headers=ADMIN)
    hidden_ids = {
        "page": world.hidden_page,
        "issue": world.hidden_issue,
        "space": private_space.json()["space_id"],
    }
    missing_ids = dict.fromkeys(hidden_ids, 999999)
    call = getattr(world.member, method)
    kwargs = {"follow_redirects": False}
    if data is not None:
        kwargs["data"] = data
    hidden_path = path.format(**hidden_ids)
    missing_path = path.format(**missing_ids)
    hidden = call(hidden_path, **kwargs)
    missing = call(missing_path, **kwargs)
    assert hidden.status_code == missing.status_code
    assert hidden.status_code >= 400
    # The shell echoes the request's own path (the rail and theme toggles come
    # back to it); that is the asker's URL, not a fact about the object.
    assert hidden.text.replace(hidden_path, "{path}") == missing.text.replace(
        missing_path, "{path}"
    )


@pytest.mark.parametrize(
    "path", ["/aegis/issues/{issue}/comments", "/mentor/pages/{page}/comments"]
)
def test_a_comment_refused_for_want_of_a_session_is_shown_back_escaped(world, path):
    """The session expired while the comment was being written. The sign-in
    answer shows the text that was not posted, escaped, so it can be copied
    even without the draft script, and marks the post as not accepted."""
    written = 'Ship it <script>alert("x")</script> & "quote" before Friday'
    answer = world.anonymous.post(
        path.format(issue=world.issue, page=world.page),
        data={"body": written},
        follow_redirects=False,
    )
    assert answer.status_code == 401
    assert _in_shell(answer.text)
    assert "<script>alert" not in answer.text
    assert (
        "Ship it &lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; &amp; "
        "&#34;quote&#34; before Friday"
    ) in answer.text
    assert 'data-refused="401"' in answer.text
    back = path.format(issue=world.issue, page=world.page).rsplit("/", 1)[0]
    assert f'href="{back}"' in answer.text
    assert 'href="/login" class="button"' in answer.text


def test_the_comment_boxes_and_the_shell_carry_the_draft_wiring(world):
    """static/drafts.js has no runner in this suite; it is driven in Chromium
    (see the U11 report). What the suite can hold is the wiring it relies on:
    the shell loads it, and both new-comment boxes ask for it."""
    issue_page = world.admin.get(f"/aegis/issues/{world.issue}").text
    page_page = world.admin.get(f"/mentor/pages/{world.page}").text
    for text in (issue_page, page_page):
        assert '<script src="/static/drafts.js?v=' in text
        box = re.search(r'<textarea id="comment-body"[^>]*>', text)
        assert box is not None and "data-draft" in box.group(0)
    # The markers, as the code that reads them, not as a mention in a comment.
    script = (SRC / "static" / "drafts.js").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("//")
    )
    for reads in (
        'document.querySelectorAll("textarea[data-draft]")',
        'document.querySelector("[data-refused]")',
        "window.sessionStorage",
    ):
        assert reads in code
    refused = world.anonymous.post(
        f"/aegis/issues/{world.issue}/comments", data={"body": "x"}
    )
    assert "data-refused" in refused.text


def test_the_back_link_trusts_only_a_same_origin_referer(world):
    path = f"/aegis/issues/{world.issue}/sprint"
    same = world.admin.post(
        path,
        data={"sprint_id": "abc"},
        headers={"Referer": f"http://testserver/aegis/issues/{world.issue}?tab=x"},
    )
    assert f'href="/aegis/issues/{world.issue}?tab=x"' in same.text
    for referer in (
        "https://evil.example/aegis",
        "http://testserver.evil.example/x",
        "javascript:alert(1)",
    ):
        foreign = world.admin.post(
            path, data={"sprint_id": "abc"}, headers={"Referer": referer}
        )
        assert "evil" not in foreign.text and "javascript:" not in foreign.text
        assert 'href="/aegis" class="button secondary"' in foreign.text


# --- no bare refusal left in the browser routes ---------------------------------

#: ``HTMLResponse`` calls that answer >= 400 with a body and are not a
#: ``BrowserRefusal``: none. The two board answers with an EMPTY body are not
#: refusals a person reads: they are sent only to htmx (inside an
#: ``if request.headers.get("HX-Request")`` branch) and carry ``HX-Redirect``,
#: which sends the browser to the board with the conflict notice.
EMPTY_BODY_FOR_HTMX_REDIRECT = {("boards.py", 412), ("boards.py", 409)}


def _bare_refusals(source: str, name: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HTMLResponse"
        ):
            continue
        status = None
        for keyword in node.keywords:
            if keyword.arg == "status_code":
                status = keyword.value
        if status is None and len(node.args) > 1:
            status = node.args[1]
        if status is None:
            continue  # 200: a page or fragment that is not a refusal
        if isinstance(status, ast.Constant) and status.value < 400:
            continue
        body = node.args[0] if node.args else None
        code = status.value if isinstance(status, ast.Constant) else None
        if (
            isinstance(body, ast.Constant)
            and body.value == ""
            and (name, code) in EMPTY_BODY_FOR_HTMX_REDIRECT
        ):
            continue
        found.append(f"{name}:{node.lineno}")
    return found


def test_no_browser_route_returns_a_bare_refusal():
    """Every browser refusal goes through ``BrowserRefusal``, which gives htmx
    its fragment and everyone else the page. A new ``HTMLResponse(..., 4xx)``
    with a body fails here."""
    bare = [
        hit
        for path in sorted(WEB.glob("*.py"))
        for hit in _bare_refusals(path.read_text(), path.name)
    ]
    assert bare == []


def test_the_scan_finds_a_bare_refusal():
    assert _bare_refusals(
        "HTMLResponse('<div class=\"error\">No.</div>', status_code=400)", "x.py"
    ) == ["x.py:1"]
    assert _bare_refusals("HTMLResponse('<p>x</p>', status)", "x.py") == ["x.py:1"]
    assert _bare_refusals("HTMLResponse('<p>ok</p>')", "x.py") == []
    assert _bare_refusals("HTMLResponse('', status_code=412)", "boards.py") == []
    assert _bare_refusals("HTMLResponse('', status_code=400)", "boards.py") == [
        "boards.py:1"
    ]
