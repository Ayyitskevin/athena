"""Self-refreshing panels for the cockpit surfaces that answer "right now".

VISION's Observe promise is that an operator can see what each agent is doing at
this moment. Three surfaces carry that weight — the dashboard's fleet-attention
card, Mission Control's active claimed work, and the open run-control requests —
and until now each of them only changed when a human pressed reload. A number that
is stale in a way the page does not admit is worse than no number, because it reads
as current.

The mechanism is deliberately the smallest one that works: htmx is already vendored,
so a panel asks its own page for its own markup every N seconds and swaps itself.
No SSE (which fights the one-process model), no websockets, no JS build chain
(VISION rule 4) — three attributes on an element that already existed.

Two properties matter more than the mechanism:

**The poll is the page.** A refresh re-enters the same route, with the same admin
gate and the same visibility filtering, and asks for the same filtered view the
operator is looking at (`refresh_url` preserves the query string). There is no
second code path that could drift from the first, and no partial-only endpoint that
could forget a gate the page remembers. A viewer who may not see a surface cannot
poll it either, because the polling markup lives inside the surface they were
refused. That includes paused accounts: the session middleware treats a paused user
as signed out, so the admin-gated panels never render for them at all.

Two more surfaces refresh the same way (U12). The issue page's supervision
block (who holds the work, under which run, when it last moved) answers as a
panel of the issue page itself. The navigation's "needs you" badge sits on
every page, so it cannot re-enter "its" page without rebuilding whichever page
that was; it is a panel of the dashboard, which answers a named panel before
it builds anything else, and its counts are gated by the session exactly as
the badge is (web/nav.py). Still no partial-only endpoint: a route that serves
a page serves its panels.

**Staleness is shown, not implied.** Every panel renders the instant it was built,
and says how often it will rebuild. A panel that has silently stopped refreshing —
polling disabled, a dead tab, a network the browser gave up on — is then visibly
stale rather than quietly wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import Request

from athena import config
from athena.core._util import utc_now

#: Query parameter naming which panel a request wants back on its own.
#: Explicit rather than keying off the `HX-Request` header: these pages will grow
#: other htmx interactions, and "any htmx request returns the attention card" is a
#: trap waiting for the next feature. A named panel can only be asked for on purpose.
PANEL_PARAM = "panel"

#: The panel names, one per surface. Closed set — an unknown value renders the
#: whole page, exactly as a request with no parameter at all does.
FLEET_ATTENTION = "fleet-attention"
ACTIVE_WORK = "active-work"
RUN_CONTROLS = "run-controls"
ISSUE_SUPERVISION = "issue-supervision"
#: The navigation badge: a panel of the dashboard route (see the module text).
NAV_COUNTS = "nav-counts"
NAV_COUNTS_HOME = "/aegis/dashboard"

_TS_FORMAT = "%H:%M:%S"


@dataclass(frozen=True)
class Live:
    """What a panel needs to refresh itself and to admit how fresh it is."""

    url: str
    seconds: int
    observed_at: str

    @property
    def enabled(self) -> bool:
        return self.seconds > 0


def wants_panel(request: Request, panel: str) -> bool:
    """Is this request asking for one panel's markup rather than the whole page?"""
    return request.query_params.get(PANEL_PARAM) == panel


def refresh_url(request: Request, panel: str) -> str:
    """This request's own URL, with `panel` forced on.

    Every other parameter survives, which is the point: an operator filtered to one
    agent must keep seeing that agent after a refresh, not silently widen to the
    whole fleet. Rebuilt from the parsed parameters rather than by appending to the
    raw query string, so a URL that already names a panel does not accumulate a
    second one.
    """
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != PANEL_PARAM
    ]
    params.append((PANEL_PARAM, panel))
    return f"{request.url.path}?{urlencode(params)}"


def build(request: Request, panel: str) -> Live:
    """The refresh contract for one panel of the page being rendered."""
    return Live(
        url=refresh_url(request, panel),
        seconds=config.LIVE_REFRESH_SECONDS,
        observed_at=utc_now().strftime(_TS_FORMAT),
    )


def issue_supervision(request: Request, issue_id: int) -> Live | None:
    """The issue page's supervision block's refresh contract, for a signed-in
    viewer only. Its URL is built from the issue's id, not from this request:
    the page is also rendered by POST routes (a refused form, the warning before
    a close), and a poll aimed at a POST address would be refused every time."""
    if getattr(request.state, "user", None) is None:
        return None
    return Live(
        url=f"/aegis/issues/{int(issue_id)}?"
        f"{urlencode([(PANEL_PARAM, ISSUE_SUPERVISION)])}",
        seconds=config.LIVE_REFRESH_SECONDS,
        observed_at=utc_now().strftime(_TS_FORMAT),
    )


def nav(request: Request) -> Live | None:
    """The badge's refresh contract, for a signed-in viewer only.

    Signed out (and paused, which the session middleware reads as signed out)
    there is nothing to count and no poll is armed; a poll answered for a
    viewer who has since signed out comes back without one, and stops."""
    if getattr(request.state, "user", None) is None:
        return None
    return Live(
        url=f"{NAV_COUNTS_HOME}?{urlencode([(PANEL_PARAM, NAV_COUNTS)])}",
        seconds=config.LIVE_REFRESH_SECONDS,
        observed_at=utc_now().strftime(_TS_FORMAT),
    )
