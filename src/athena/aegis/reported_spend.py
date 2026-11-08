"""Read models over agent-REPORTED usage: per run, per agent per day, per issue.

Everything here is something an agent said it used (see ``core.reported_usage``). Athena did not
measure it, nothing enforces against it, and it is never a budget. The figures are sums of the
reports the viewer may see; they are not a statement of what was actually spent.

Reports come from two native events: ``usage_reported`` (a check-in; targets the agent, so only an
admin can read it) and ``claim_completed`` carrying ``usage`` in its report (targets the issue, so
the shared event visibility gate applies and a private project's reports are simply absent).
``viewer=None`` means no gate at all, for internal callers and tests; every user-facing caller
passes the real viewer. Only the newest ``SCAN_LIMIT`` events of each kind are read, so totals
cover a bounded recent window, not all of history.

A report is the usage since that agent's previous report, so totals are plain sums. An exact retry
of a check-in is a second report; this is stated, not hidden. Costs are summed per currency and
never converted.
"""

from __future__ import annotations

from decimal import Decimal
import sqlite3

from athena.aegis import lease_commands
from athena.core import activity, reported_usage

SCAN_LIMIT = 5000
LABEL = "reported by the agent, not metered by Athena"
_COUNTS = ("input_tokens", "output_tokens", "cached_tokens")


class _Totals:
    def __init__(self) -> None:
        self.reports = 0
        self.counts = dict.fromkeys(_COUNTS, 0)
        self.cost: dict[str, Decimal] = {}

    def add(self, usage: dict) -> None:
        self.reports += 1
        for name in _COUNTS:
            self.counts[name] += usage.get(name, 0)
        if "estimated_cost" in usage:
            currency = usage["currency"]
            self.cost[currency] = self.cost.get(currency, Decimal(0)) + Decimal(
                str(usage["estimated_cost"])
            )

    def public(self) -> dict:
        return {
            "reports": self.reports,
            **self.counts,
            "cost": {
                currency: format(amount.normalize(), "f")
                for currency, amount in sorted(self.cost.items())
            },
        }


class Scan:
    """One read of the visible reports, shared by every read model on a page.

    ``clipped`` is true when either event kind filled the scan window, so older reports are
    not in the totals and the page must say so.
    """

    def __init__(self, reports: list[dict], clipped: bool) -> None:
        self.reports = reports
        self.clipped = clipped


def scan(conn: sqlite3.Connection, viewer: dict | None) -> Scan:
    """Visible reports as ``{actor_id, day, run_id, issue_id, usage}``, newest first."""
    gate: dict = {} if viewer is None else {"actor": viewer}
    out: list[dict] = []
    clipped = False
    checkins = activity.list_activity(
        conn, verb=reported_usage.VERB_USAGE_REPORTED, limit=SCAN_LIMIT, **gate
    )
    clipped = len(checkins) >= SCAN_LIMIT
    for event in checkins:
        parsed = reported_usage.from_event(event)
        if parsed is not None:
            out.append(
                {
                    "actor_id": event["actor_id"],
                    "day": event["created_at"][:10],
                    "run_id": parsed["run_id"],
                    "issue_id": None,
                    "usage": parsed["usage"],
                }
            )
    completions = activity.list_activity(
        conn, verb="claim_completed", target_kind="issue", limit=SCAN_LIMIT, **gate
    )
    clipped = clipped or len(completions) >= SCAN_LIMIT
    for event in completions:
        report = lease_commands.reported_completion(event)
        if report is not None and "usage" in report:
            out.append(
                {
                    "actor_id": event["actor_id"],
                    "day": event["created_at"][:10],
                    "run_id": event["run_id"],
                    "issue_id": event["target_id"],
                    "usage": report["usage"],
                }
            )
    return Scan(out, clipped)


def by_run(
    conn: sqlite3.Connection,
    *,
    viewer: dict | None,
    agent_id: int | None = None,
    reads: Scan | None = None,
) -> list[dict]:
    """Totals per ``(agent, run)``; a completion with no run id belongs to no run."""
    groups: dict[tuple[int, str], _Totals] = {}
    for report in (reads or scan(conn, viewer)).reports:
        if report["run_id"] is None or agent_id not in (None, report["actor_id"]):
            continue
        groups.setdefault((report["actor_id"], report["run_id"]), _Totals()).add(
            report["usage"]
        )
    return [
        {"agent_id": agent, "run_id": run, "totals": totals.public()}
        for (agent, run), totals in groups.items()
    ]


def by_agent_day(
    conn: sqlite3.Connection,
    *,
    viewer: dict | None,
    agent_id: int | None = None,
    reads: Scan | None = None,
) -> list[dict]:
    """Totals per agent per server-clock day, newest day first (``reported_at`` is not used)."""
    groups: dict[tuple[int, str], _Totals] = {}
    for report in (reads or scan(conn, viewer)).reports:
        if agent_id not in (None, report["actor_id"]):
            continue
        groups.setdefault((report["actor_id"], report["day"]), _Totals()).add(
            report["usage"]
        )
    ordered = sorted(
        groups.items(), key=lambda item: (item[0][1], item[0][0]), reverse=True
    )
    return [
        {"agent_id": agent, "day": day, "totals": totals.public()}
        for (agent, day), totals in ordered
    ]


def by_issue(
    conn: sqlite3.Connection,
    *,
    viewer: dict | None,
    agent_id: int | None = None,
    reads: Scan | None = None,
) -> list[dict]:
    """Totals per issue, from completion reports only (a check-in names no issue)."""
    groups: dict[int, _Totals] = {}
    for report in (reads or scan(conn, viewer)).reports:
        if report["issue_id"] is None or agent_id not in (None, report["actor_id"]):
            continue
        groups.setdefault(report["issue_id"], _Totals()).add(report["usage"])
    return [
        {"issue_id": issue, "totals": totals.public()}
        for issue, totals in groups.items()
    ]
