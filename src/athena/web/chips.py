"""Jinja filters mapping small closed-domain values to a chip's semantic tone.

Presentation only — no database access, no business logic. Priority, sprint
state, agent reporting/claim state, agent health state, and token-warning
severity are all closed `Literal` types (see aegis/api.py, core/workers.py,
core/agents.py, aegis/sprints.py), so mapping them by exact value is complete
and correct.

Issue status is the one exception: aegis/statuses.py lets a project rename or
add statuses, so this name-based map is only a best-effort approximation of
the default seed set (statuses.DEFAULT_STATUSES). The correct fix is mapping
by category (statuses.global_category), which needs a database connection
that isn't available to a template filter — tracked as a follow-up alongside
the rest of the Phosphor Ink migration's deferred items.

An unrecognized value falls back to "neutral" (or "warning" for a token
posture, since an unknown warning kind still deserves attention) rather than
raising — a filter runs mid-render, and an unmapped value is a display
nicety, not an error worth a 500.
"""

from __future__ import annotations

_STATUS_TONE = {"open": "neutral", "in_progress": "info", "done": "success"}
_PRIORITY_TONE = {
    "low": "neutral",
    "medium": "info",
    "high": "warning",
    "urgent": "danger",
}
# Covers both agent_runs.html's checkin.reporting_state ("reporting_recently" /
# "stale") and fleet_active_work.html's lease.claim_state ("active" / "expired") —
# the old stylesheet reused one CSS class for both domains; this filter does too.
_CHECKIN_TONE = {
    "reporting_recently": "success",
    "active": "success",
    "stale": "warning",
    "expired": "danger",
    "not_reported": "neutral",
    "untagged": "neutral",
}
_HEALTH_TONE = {
    "replay_ready": "success",
    "mixed_runs": "info",
    "partial_window": "warning",
    "untagged_only": "neutral",
    "quiet": "neutral",
}
_TOKEN_TONE = {"critical": "danger", "danger": "danger", "warning": "warning"}
_SPRINT_TONE = {"planned": "neutral", "active": "info", "completed": "success"}


_CATEGORY_TONE = {"todo": "neutral", "doing": "info", "done": "success"}


def status_tone(name: str | None) -> str:
    return _STATUS_TONE.get(name or "", "neutral")


def category_tone(category: str | None) -> str:
    """Tone from a status CATEGORY (todo/doing/done) — the correct mapping
    wherever the row carries ``status_category``, because a project can name
    its statuses anything. ``status_tone`` remains the name-based fallback
    for surfaces whose queries don't resolve the category (search snippets,
    imported snapshots)."""
    return _CATEGORY_TONE.get(category or "", "neutral")


def priority_tone(name: str | None) -> str:
    return _PRIORITY_TONE.get(name or "", "neutral")


def checkin_tone(state: str | None) -> str:
    return _CHECKIN_TONE.get(state or "", "neutral")


def health_tone(state: str | None) -> str:
    return _HEALTH_TONE.get(state or "", "neutral")


def token_tone(severity: str | None) -> str:
    return _TOKEN_TONE.get(severity or "", "warning")


def sprint_tone(state: str | None) -> str:
    return _SPRINT_TONE.get(state or "", "neutral")
