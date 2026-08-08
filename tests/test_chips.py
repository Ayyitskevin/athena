"""Chip tone mapping: closed domains map by value, unknowns never raise.

WHY: a chip's data-tone is presentation, but a WRONG tone misleads the
operator (a defied kill reading as success would be worse than no color).
These pin the semantic pairings the design system documents, and the
fallback behavior — a filter runs mid-render, so an unmapped value must
degrade to a neutral chip, never to a 500.
"""

import pytest

from athena.web import chips


@pytest.mark.parametrize(
    ("value", "tone"),
    [("open", "neutral"), ("in_progress", "info"), ("done", "success")],
)
def test_status_tone_maps_seed_statuses(value, tone):
    assert chips.status_tone(value) == tone


@pytest.mark.parametrize(
    ("value", "tone"),
    [
        ("low", "neutral"),
        ("medium", "info"),
        ("high", "warning"),
        ("urgent", "danger"),
    ],
)
def test_priority_tone_orders_by_urgency(value, tone):
    assert chips.priority_tone(value) == tone


@pytest.mark.parametrize(
    ("value", "tone"),
    [
        # Silence is the primary signal a supervisor reads: recently
        # reporting is healthy, stale is a warning, expired is danger.
        ("reporting_recently", "success"),
        ("active", "success"),
        ("stale", "warning"),
        ("expired", "danger"),
        ("not_reported", "neutral"),
        ("untagged", "neutral"),
    ],
)
def test_checkin_tone_reads_liveness(value, tone):
    assert chips.checkin_tone(value) == tone


@pytest.mark.parametrize(
    ("value", "tone"),
    [
        ("replay_ready", "success"),
        ("mixed_runs", "info"),
        ("partial_window", "warning"),
        ("untagged_only", "neutral"),
        ("quiet", "neutral"),
    ],
)
def test_health_tone(value, tone):
    assert chips.health_tone(value) == tone


@pytest.mark.parametrize(
    ("value", "tone"),
    [("planned", "neutral"), ("active", "info"), ("completed", "success")],
)
def test_sprint_tone_follows_lifecycle(value, tone):
    assert chips.sprint_tone(value) == tone


@pytest.mark.parametrize(
    ("value", "tone"),
    [("critical", "danger"), ("danger", "danger"), ("warning", "warning")],
)
def test_token_tone(value, tone):
    assert chips.token_tone(value) == tone


def test_unknown_values_degrade_to_neutral_never_raise():
    for fn in (
        chips.status_tone,
        chips.priority_tone,
        chips.checkin_tone,
        chips.health_tone,
        chips.sprint_tone,
    ):
        assert fn("no-such-value") == "neutral"
        assert fn(None) == "neutral"
        assert fn("") == "neutral"


def test_unknown_token_severity_stays_loud():
    # A token-posture warning of unknown severity is still a warning the
    # operator should see — the fallback is warning, not neutral.
    assert chips.token_tone("??") == "warning"
    assert chips.token_tone(None) == "warning"
