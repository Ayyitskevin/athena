"""What an instance lets a caller with no credential read (C11, D7, D23).

``ATHENA_ANONYMOUS_READS`` defaults on (D23 keeps that default), so an operator
must be told what that means on their own instance: how many GET routes the
credential gate lets a caller with no credential past, which projects and spaces
are public, and which reads no container scopes.

The route count is derived from the application's own routes, each asked the way
its own identity dependency decides:

- a browser route (one carrying the browser-session dependency ``main.py``
  attaches to every ``web/`` router: pages, their JSON and CSV helpers, the
  sign-in routes) is called with a signed-out GET request, and is counted when
  the dependency lets it through;
- any other route's identity is read from its dependency tree by
  ``agent_openapi.authentication`` (the classifier the agent OpenAPI document's
  ``security`` comes from): ``required`` (``current_actor``) is never counted,
  ``optional`` (``optional_actor``) is counted when
  ``identity._anonymous_reads_allowed()`` says so (the predicate that
  dependency consults), and a route with no identity dependency (``/healthz``,
  ``/readyz``, ``/version``, the two OpenAPI documents) always is.

This counts what the credential gate admits, not what then answers 2xx: past
the gate a page may still answer a signed-out caller with its own sign-in
prompt (the admin pages do), and a route may answer 404 for a hidden or absent
row, or for single sign-on that is not configured.

Counting walks every route of a built app, which costs ``athena-doctor`` the
import of ``athena.main`` (seconds). So the two results, with anonymous reads on
and off, are stored here as constants, and ``tests/test_anonymous_read_exposure.py``
recomputes them from the app and fails, printing the new values, whenever a
route changes them. Nothing here reads a credential, a row's content or an
environment value: the public containers are named by key only.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Any

#: How many container keys one line names before it says "and N more".
_SHOWN_KEYS = 10


@dataclass(frozen=True, slots=True)
class RouteExposure:
    """GET routes, and how many of each kind the credential gate lets a caller
    with no credential past."""

    rest: int
    rest_total: int
    browser: int
    browser_total: int
    documents: int

    @property
    def anonymous(self) -> int:
        return self.rest + self.browser + self.documents

    @property
    def total(self) -> int:
        return self.rest_total + self.browser_total + self.documents


#: ``route_exposure`` of the packaged app, with anonymous reads on and off
#: (pinned by ``tests/test_anonymous_read_exposure.py``).
ROUTE_COUNTS: dict[bool, RouteExposure] = {
    True: RouteExposure(
        rest=53, rest_total=105, browser=57, browser_total=57, documents=2
    ),
    False: RouteExposure(
        rest=3, rest_total=105, browser=3, browser_total=57, documents=2
    ),
}


@dataclass(frozen=True, slots=True)
class PublicContainers:
    """Public projects and spaces, by key, beside how many of each exist."""

    projects: tuple[str, ...]
    project_total: int
    spaces: tuple[str, ...]
    space_total: int
    issues_without_project: int


def _browser_gate_admits(gate: Any, path: str) -> bool:
    """Whether the browser-session dependency lets a signed-out GET through."""
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
    )
    try:
        gate(request)
    # The gate refuses by raising its own private exception, which this module
    # does not import: athena.middleware stays out of every mapped module, so a
    # middleware change keeps selecting the full suite (see agent_openapi).
    except Exception:  # noqa: BLE001 — any raise is the gate refusing
        return False
    return True


def route_exposure(app: Any, transport: Any) -> RouteExposure:
    """Count ``app``'s GET routes the credential gate lets a caller with no
    credential past (``transport`` is an ``agent_openapi.Transport``)."""
    from starlette.routing import Mount

    from athena import agent_openapi
    from athena.core import identity

    reader = agent_openapi.CodeReader()
    rest = rest_total = browser = browser_total = documents = 0
    for route in agent_openapi.route_contexts(app):
        if isinstance(getattr(route, "original_route", route), Mount):
            continue  # packaged static assets, not a read of workspace data
        if "GET" not in (getattr(route, "methods", None) or ()):
            continue
        kind = agent_openapi.classify(
            route, openapi_url=app.openapi_url, transport=transport
        )
        if kind == agent_openapi.BROWSER:
            browser_total += 1
            browser += _browser_gate_admits(transport.browser_session, route.path)
        elif kind == agent_openapi.DOCUMENT or not hasattr(route, "dependant"):
            documents += 1
        else:
            rest_total += 1
            needed = agent_openapi.authentication(route, reader)
            rest += needed is None or (
                needed == "optional" and identity._anonymous_reads_allowed()
            )
    return RouteExposure(rest, rest_total, browser, browser_total, documents)


#: A key is printed only when it has the shape a project key must have; any
#: other key (a space key may hold a newline, an escape sequence or 76
#: characters) is printed as ``#<id>``, so no account can write a line of the
#: operator's report.
_PRINTABLE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]{0,9}")

#: GET routes that answer a caller with no credential, while anonymous reads are
#: on, whatever any project's or space's visibility: they are not scoped to a
#: container. ``tests/test_anonymous_read_exposure.py`` asks each over HTTP with
#: every container private.
UNSCOPED_READS = (
    "/issues",
    "/labels",
    "/aegis/labels",
    "/aegis/activity",
    "/aegis/activity.csv",
    "/fleet/metrics",
    "/aegis/fleet-metrics",
)
_UNSCOPED_TEXT = "labels, the activity feed and its CSV export, and fleet metrics"


def _key(row: sqlite3.Row) -> str:
    key = row["key"]
    if isinstance(key, str) and _PRINTABLE_KEY.fullmatch(key):
        return key
    return f"#{int(row['id'])}"


def public_containers(conn: sqlite3.Connection) -> PublicContainers:
    """The public projects and spaces, and the issues no project holds."""
    projects = conn.execute(
        "SELECT id, key, visibility FROM projects ORDER BY id"
    ).fetchall()
    spaces = conn.execute(
        "SELECT id, key, visibility FROM spaces ORDER BY id"
    ).fetchall()
    loose = conn.execute("SELECT COUNT(*) FROM issues WHERE project_id IS NULL")
    return PublicContainers(
        projects=tuple(_key(row) for row in projects if row["visibility"] == "public"),
        project_total=len(projects),
        spaces=tuple(_key(row) for row in spaces if row["visibility"] == "public"),
        space_total=len(spaces),
        issues_without_project=int(loose.fetchone()[0]),
    )


def _named(keys: tuple[str, ...]) -> str:
    if not keys:
        return ""
    shown = ", ".join(keys[:_SHOWN_KEYS])
    if len(keys) > _SHOWN_KEYS:
        shown += f" and {len(keys) - _SHOWN_KEYS} more"
    return f" ({shown})"


def setting_text(*, anonymous_reads: bool, explicit: bool) -> str:
    """The setting as the operator configured it; never the raw value."""
    state = "on" if anonymous_reads else "off"
    if explicit:
        return (
            f"{state} (ATHENA_ANONYMOUS_READS={'true' if anonymous_reads else 'false'})"
        )
    return f"{state} (ATHENA_ANONYMOUS_READS is not set; the default is on)"


def routes_text(exposure: RouteExposure) -> str:
    """What the count is: routes the credential gate admits, not routes that
    answer 2xx."""
    return (
        f"{exposure.anonymous} of {exposure.total} GET routes let a caller with no "
        f"credential past the credential gate ({exposure.rest} of "
        f"{exposure.rest_total} REST routes, {exposure.browser} of "
        f"{exposure.browser_total} browser routes, {exposure.documents} OpenAPI "
        "documents); past the gate a route may still ask the viewer to sign in, "
        "or answer 404 for a hidden or absent row or for single sign-on that is "
        "not configured"
    )


def doctor_line(
    exposure: RouteExposure,
    containers: PublicContainers,
    *,
    anonymous_reads: bool,
    explicit: bool,
) -> str:
    """The line ``athena-doctor`` prints: the setting of the environment it runs
    in, the route count, the public containers and the reads no container
    scopes."""
    public = (
        f"public containers: {len(containers.projects)} of "
        f"{containers.project_total} projects{_named(containers.projects)}, "
        f"{len(containers.spaces)} of {containers.space_total} spaces"
        f"{_named(containers.spaces)}"
    )
    if anonymous_reads:
        unscoped = (
            "readable whatever a container's visibility: issues with no project: "
            f"{containers.issues_without_project}; {_UNSCOPED_TEXT}"
        )
        advice = (
            "set ATHENA_ANONYMOUS_READS=false to require a credential for every read"
        )
        return (
            "anonymous reads in this environment: "
            f"{setting_text(anonymous_reads=anonymous_reads, explicit=explicit)}; "
            f"{routes_text(exposure)}; {public}; {unscoped}; {advice}"
        )
    return (
        "anonymous reads in this environment: "
        f"{setting_text(anonymous_reads=anonymous_reads, explicit=explicit)}; "
        f"{routes_text(exposure)}; {public}; every container and every read "
        "beside them needs a credential"
    )


def first_start_line(
    exposure: RouteExposure, *, anonymous_reads: bool, explicit: bool
) -> str:
    """The one line the first start (``athena-serve --bootstrap``) prints."""
    setting = setting_text(anonymous_reads=anonymous_reads, explicit=explicit)
    if anonymous_reads:
        meaning = (
            "anyone who can reach this listener reads every public project and "
            "space without signing in, and new ones are public unless "
            "ATHENA_DEFAULT_VISIBILITY=private, and issues with no project, "
            f"{_UNSCOPED_TEXT} are readable whatever a container's visibility; "
            "set ATHENA_ANONYMOUS_READS=false to require a credential for every "
            "read"
        )
    else:
        meaning = "every read requires a credential, whatever a container's visibility"
    return (
        f"anonymous reads: {setting}: {meaning}; {routes_text(exposure)}; "
        "athena-doctor reports this state and the public containers"
    )
