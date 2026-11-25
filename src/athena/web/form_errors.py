"""A refusal a browser sees: inside the application, not a bare line of text.

COCKPIT-13 (U11). A refused plain form post used to answer with a fragment such
as ``<div class="error">Label name is required.</div>`` and nothing else: no
``<html>``, no stylesheet, no navigation. The browser showed one unstyled line
and the person had lost the page they were on. Two answers replace it:

- A validation refusal of a form on a page that has a context builder (the
  issue page and its edit form, the Mentor page and its editor, the space page,
  the filters page, the admin pages) re-renders THAT page with the error and
  what was typed. Those live beside each page's own context builder.
- Every other refusal is a ``BrowserRefusal``: the same status code and the
  same fragment, which htmx still receives as it is (it swaps fragments into
  the page it is on), and which any other request receives inside the shell
  (``templates/refused.html``, extending ``base.html``) with a link back.

``BrowserRefusal`` decides between the two when it is SENT, from the request's
own scope, so the shared gates (``signin_required``, ``readonly_response``,
``issue_visible_or_404``...) keep their signatures and need no ``request``.
The link back is the Referer only when it is a same-origin local path, and
otherwise the index of the section the refused path belongs to, so a hidden
object and a missing one still answer identically: nothing here reads the
object, only the request.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from fastapi.responses import HTMLResponse
from markupsafe import Markup
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from athena.web import redirects

#: The indexes a refusal links back to when the request names no safe page of
#: its own: the routes of the five navigation groups (docs/DESIGN_SYSTEM.md),
#: plus the account settings. The longest one the refused path sits under wins.
SECTION_INDEXES = (
    "/aegis/dashboard",
    "/aegis/boards",
    "/aegis",
    "/aegis/projects",
    "/mentor",
    "/aegis/labels",
    "/aegis/filters",
    "/admin/agents",
    "/admin/fleet",
    "/settings/tokens",
    "/admin/automation",
    "/aegis/activity",
    "/admin/agents/runs",
    "/aegis/fleet-metrics",
    "/admin/webhooks",
    "/admin/run-controls",
    "/inbox",
    "/admin/security",
    "/admin/users",
    "/settings/password",
    "/settings/identities",
)

#: The page's heading, by status. The fragment below it still says exactly what
#: was refused, in the words it always used.
_HEADINGS = {
    401: "Sign in to continue",
    403: "Not allowed",
    404: "Not found",
    409: "Not done",
    412: "Not done",
    413: "Too large",
    429: "Too many attempts",
}


def is_htmx(request: Request) -> bool:
    """Whether htmx sent this request, and so swaps the answer into a page."""
    return request.headers.get("HX-Request", "").lower() == "true"


def section_index(path: str) -> str:
    """The index of the section ``path`` belongs to, or the home page."""
    best = "/"
    for index in SECTION_INDEXES:
        if (path == index or path.startswith(index + "/")) and len(index) > len(best):
            best = index
    return best


def back_link(request: Request) -> str:
    """Where a refusal sends the person back to.

    The Referer is trusted for one thing only: a same-origin page of this app,
    used as a local path. Anything else falls back to the section's index."""
    referer = request.headers.get("referer") or ""
    if referer:
        parts = urlsplit(referer)
        if (parts.scheme, parts.netloc) == (request.url.scheme, request.url.netloc):
            local = parts.path + (f"?{parts.query}" if parts.query else "")
            safe = redirects.safe_local_path(local)
            if safe is not None and parts.path != request.url.path:
                return safe
    return section_index(request.url.path)


class BrowserRefusal(HTMLResponse):
    """An ``HTMLResponse`` for a refusal: the fragment for htmx, a page otherwise.

    Built exactly like the ``HTMLResponse`` it replaces (content, status code,
    headers), so its status and wording cannot drift from what it said before.
    ``unposted`` is text the person wrote that was not saved (a comment refused
    for want of a session): the page shows it so it can be copied. ``back`` is
    the page to return to when the route knows it better than the request does.
    """

    def __init__(
        self,
        content: str = "",
        status_code: int = 400,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
        *,
        unposted: str | None = None,
        back: str | None = None,
    ) -> None:
        super().__init__(content, status_code, headers, media_type, background)
        self.fragment = content
        self.unposted = unposted or None
        self.back = back

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            if not is_htmx(request):
                page = await run_in_threadpool(self.render_page, request)
                self.body = self.render(page)
                self.headers["content-length"] = str(len(self.body))
        await super().__call__(scope, receive, send)

    def render_page(self, request: Request) -> str:
        """The refusal inside the shell (``templates/refused.html``)."""
        # Imported here: web.router answers refusals too, so a module-level
        # import would be circular.
        from athena.web.router import get_templates

        template = get_templates().get_template("refused.html")
        return template.render(
            {
                "request": request,
                "heading": _HEADINGS.get(self.status_code, "Not accepted"),
                "status_code": self.status_code,
                # Server-built markup: every fragment escapes what it echoes.
                "fragment": Markup(self.fragment),
                "unposted": self.unposted,
                "back": self.back or back_link(request),
            }
        )
