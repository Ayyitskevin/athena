"""The issue run narrative: one read-time story over existing history.

The contract under test is the projection's honesty discipline (MWS-15): the
narrative only RE-READS surfaces that already exist (activity, claim handoffs,
run controls, check-ins), every item cites its owning source, unknown signals
fail closed instead of being guessed into a class, each lane obeys its owning
surface's visibility, and one per-request clock drives every derived freshness
so comparisons inside a response are honest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from athena.aegis import (
    issue_commands,
    issue_etags,
    issue_narrative,
    issues,
    lease_commands,
)
from athena.core import (
    activity,
    agent_run_checkins,
    db,
    run_context,
    run_control_commands,
    run_controls,
    tokens,
)

RUN_ID = "run-narrative-alpha"
OTHER_RUN_ID = "run-narrative-other"


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_users(conn):
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) "
        "VALUES ('op@e.com', 'Operator', 'admin', 0)"
    )
    conn.execute(
        "INSERT INTO users (email, name, is_agent) VALUES ('a@e.com', 'Agent A', 1)"
    )
    conn.execute(
        "INSERT INTO users (email, name, role, is_agent) "
        "VALUES ('h@e.com', 'Human', 'member', 0)"
    )
    conn.commit()


def _actor(conn, user_id):
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def _issue_tag(conn, issue_id):
    issue = issues.get_issue(conn, issue_id)
    assert issue is not None
    return issue_etags.current_etag(conn, issue)


def _claim(conn, user_id, issue_id, *, generation=None, run_id=None):
    token = run_context.set_run_id(run_id)
    try:
        return lease_commands.claim_issue(
            conn,
            actor=_actor(conn, user_id),
            issue_id=issue_id,
            if_match=[_issue_tag(conn, issue_id)],
            generation=generation,
        )
    finally:
        run_context.reset_run_id(token)


def _handoff_payload(generation, **overrides):
    payload = {
        "generation": generation,
        "reason": "blocked",
        "note": "Waiting for an operator decision.",
        "attempted_work": "Reproduced the failure and isolated the boundary.",
        "evidence": ["focused test reproduces the failure", "pytest -q output"],
        "blocking_question": "Which recovery path should be used?",
        "resume_instructions": "Choose the recovery path, then rerun the test.",
    }
    payload.update(overrides)
    return payload


def _delegated_issue(conn, title="narrative work"):
    issue = issues.create_issue(conn, title=title, body="story", created_by=1)
    issue_commands.add_contributor(
        conn,
        actor=_actor(conn, 1),
        issue_id=issue["id"],
        user_id=2,
        require_agent=True,
    )
    return issue


def _checkin(conn, user_id=2, run_id=RUN_ID):
    cred = tokens.create_token(
        conn, user_id=user_id, name="narrative-test", scopes=["read"]
    )
    agent_run_checkins.upsert_checkin(
        conn, agent_id=user_id, run_id=run_id, token_id=cred["id"]
    )
    conn.commit()


def _control(conn, run_id=RUN_ID, *, now=None, ttl_seconds=60):
    return run_control_commands.create_control(
        conn,
        actor=_actor(conn, 1),
        run_id=run_id,
        kind=run_controls.KIND_STEER,
        payload="use approach B",
        ttl_seconds=ttl_seconds,
        now=now,
    )


def _signals(narrative):
    return [item["signal"] for item in narrative["items"]]


def test_full_lifecycle_orders_newest_first_and_cites_every_source(tmp_path):
    """A claim → steer → check-in → handoff → resume → complete story reads back
    as typed items in a deterministic newest-first order, each citing the surface
    that owns it — no item invents an id."""
    conn = _migrated_conn(tmp_path / "narrative.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"], run_id=RUN_ID)
    _checkin(conn)
    _control(conn)
    handoff = lease_commands.yield_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        **_handoff_payload(lease["generation"]),
    )
    lease = _claim(conn, 2, issue["id"], run_id=RUN_ID)
    lease_commands.resume_claim_handoff(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        handoff_token=handoff["handoff_token"],
        generation=lease["generation"],
    )
    lease_commands.complete_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        generation=lease["generation"],
    )

    narrative = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1)
    )
    assert narrative["schema"] == "athena.issue_narrative.v1"
    assert not narrative["clipped"]

    signals = _signals(narrative)
    for expected in (
        "claim",
        "checkin",
        "ask",
        "handoff",
        "evidence",
        "outcome",
    ):
        assert expected in signals, signals
    # Both evidence refs from the handoff arrive as their own items.
    assert signals.count("evidence") == 2

    # Deterministic newest-first order: identical across builds, sorted by the
    # documented (at, rank, source) key.
    again = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1)
    )
    assert [i["source"] for i in narrative["items"]] == [
        i["source"] for i in again["items"]
    ]
    stamps = [str(item["at"]) for item in narrative["items"]]
    assert stamps == sorted(stamps, reverse=True)

    # Every item cites an owning source that actually exists.
    event_ids = {
        row["id"] for row in conn.execute("SELECT id FROM activity").fetchall()
    }
    control_ids = {
        row["id"] for row in conn.execute("SELECT id FROM run_controls").fetchall()
    }
    for item in narrative["items"]:
        kind, source_id = item["source"]["kind"], item["source"]["id"]
        if kind == "activity":
            assert source_id in event_ids
        elif kind == "run_control":
            assert source_id in control_ids
        elif kind == "claim_handoff":
            assert source_id == handoff["handoff_token"]
        elif kind == "agent_run_checkin":
            assert source_id == RUN_ID
        else:  # a new source kind must be added here deliberately
            raise AssertionError(f"unknown source kind: {kind}")

    # The handoff item is the owning record's, not a re-typed activity event.
    handoff_item = next(i for i in narrative["items"] if i["signal"] == "handoff")
    assert handoff_item["state"] == "resumed"
    assert handoff_item["source"] == {
        "kind": "claim_handoff",
        "id": handoff["handoff_token"],
    }
    conn.close()


def test_unknown_verbs_fail_closed_into_the_unclassified_count(tmp_path):
    """A comment is real issue history but no run signal: it must never be
    guessed into a class — it is counted, honestly, as unclassified."""
    conn = _migrated_conn(tmp_path / "unknown.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"], run_id=RUN_ID)
    comment_event = activity.record(
        conn,
        actor_id=3,
        verb="commented",
        target_kind="issue",
        target_id=issue["id"],
        detail="looks good",
    )

    narrative = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 3)
    )
    cited_events = {
        item["source"]["id"]
        for item in narrative["items"]
        if item["source"]["kind"] == "activity"
    }
    assert comment_event["id"] not in cited_events
    # The window reports exactly the events it refused to type (delegated +
    # commented), so a clipped story can never pass as complete.
    assert narrative["window"]["unclassified_events"] == 2
    conn.close()


def test_clipped_window_says_so_instead_of_implying_completeness(tmp_path):
    """Truncation is always announced: an over-limit item list clips, and an
    event window that had to drop older history says so in `window`."""
    conn = _migrated_conn(tmp_path / "clip.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"], run_id=RUN_ID)
    _checkin(conn)
    _control(conn)

    narrative = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1), limit=2
    )
    assert narrative["clipped"]
    assert len(narrative["items"]) == 2

    monkey_window = 1
    original = issue_narrative.EVENT_WINDOW
    try:
        issue_narrative.EVENT_WINDOW = monkey_window
        narrow = issue_narrative.build_issue_narrative(
            conn, issue["id"], actor=_actor(conn, 1)
        )
    finally:
        issue_narrative.EVENT_WINDOW = original
    assert narrow["window"]["events_clipped"]
    assert narrow["window"]["event_limit"] == monkey_window
    conn.close()


def test_control_window_reports_clipping_only_when_items_were_omitted(
    tmp_path, monkeypatch
):
    """A full control window is still complete; only an overflowing lane is
    labelled clipped, and the response stays bounded to the advertised limit."""
    conn = _migrated_conn(tmp_path / "control-window.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"], run_id=RUN_ID)
    monkeypatch.setattr(issue_narrative, "CONTROLS_PER_RUN_LIMIT", 2)
    t0 = datetime(2020, 1, 1, tzinfo=UTC)
    _control(conn, now=t0)
    _control(conn, now=t0 + timedelta(seconds=120))

    exact = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1)
    )
    assert not exact["window"]["controls_clipped"]
    assert sum(item["source"]["kind"] == "run_control" for item in exact["items"]) == 2

    _control(conn, now=t0 + timedelta(seconds=240))
    overflow = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1)
    )
    assert overflow["window"]["controls_clipped"]
    assert (
        sum(item["source"]["kind"] == "run_control" for item in overflow["items"]) == 2
    )
    conn.close()


def test_missing_issue_and_foreign_runs_are_absent(tmp_path):
    """A missing issue is None, not an empty story. And a control on a run that
    never touched this issue stays with its own run — the narrative joins only
    run ids the issue's visible trail actually carries."""
    conn = _migrated_conn(tmp_path / "missing.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    other = _delegated_issue(conn, title="other work")
    _claim(conn, 2, issue["id"], run_id=RUN_ID)
    _claim(conn, 2, other["id"], run_id=OTHER_RUN_ID)
    _control(conn, run_id=OTHER_RUN_ID)

    assert issue_narrative.build_issue_narrative(conn, 99999, actor=None) is None

    narrative = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1)
    )
    assert narrative is not None
    assert all(item["run_id"] != OTHER_RUN_ID for item in narrative["items"]), (
        narrative["items"]
    )
    assert not [
        item for item in narrative["items"] if item["source"]["kind"] == "run_control"
    ]
    conn.close()


def test_one_clock_drives_every_derived_freshness(tmp_path):
    """Ask-vs-ended and fresh-vs-stale are the same clock's verdict: one
    injected `now` flips both lanes together, and the response stamps exactly
    the clock it used."""
    conn = _migrated_conn(tmp_path / "clock.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"], run_id=RUN_ID)
    _checkin(conn)
    t0 = datetime.now(UTC).replace(microsecond=0)
    _control(conn, now=t0, ttl_seconds=60)

    fresh = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1), now=t0 + timedelta(seconds=30)
    )
    assert fresh["observed_at"] == run_controls.stamp(t0 + timedelta(seconds=30))
    control_item = next(
        i for i in fresh["items"] if i["source"]["kind"] == "run_control"
    )
    checkin_item = next(
        i for i in fresh["items"] if i["source"]["kind"] == "agent_run_checkin"
    )
    assert control_item["signal"] == "ask"
    assert control_item["state"] == run_controls.STATE_REQUESTED
    assert checkin_item["state"] == agent_run_checkins.REPORTING_RECENTLY

    later = t0 + timedelta(seconds=3600)
    stale = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1), now=later
    )
    assert stale["observed_at"] == run_controls.stamp(later)
    control_item = next(
        i for i in stale["items"] if i["source"]["kind"] == "run_control"
    )
    checkin_item = next(
        i for i in stale["items"] if i["source"]["kind"] == "agent_run_checkin"
    )
    # Same stored facts, later clock: the open ask became an ended control and
    # the fresh check-in went stale — derived, never stored.
    assert control_item["signal"] == "run_control"
    assert control_item["state"] == run_controls.STATE_EXPIRED
    assert checkin_item["state"] == agent_run_checkins.STALE
    conn.close()


def test_default_call_captures_one_clock_for_the_whole_projection(
    tmp_path, monkeypatch
):
    """The normal, non-injected path captures server time once, then uses that
    same instant for the response stamp and every freshness calculation."""
    conn = _migrated_conn(tmp_path / "default-clock.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"], run_id=RUN_ID)
    _checkin(conn)
    t0 = datetime(2040, 1, 2, 3, 4, 5, tzinfo=UTC)
    conn.execute(
        "UPDATE agent_run_checkins SET last_seen_at = ? WHERE run_id = ?",
        (t0.strftime("%Y-%m-%d %H:%M:%S"), RUN_ID),
    )
    conn.commit()
    _control(conn, now=t0, ttl_seconds=60)
    observed = t0 + timedelta(seconds=30)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return observed

    monkeypatch.setattr(issue_narrative, "datetime", FrozenDateTime)
    narrative = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1)
    )

    assert narrative["observed_at"] == run_controls.stamp(observed)
    control_item = next(
        item for item in narrative["items"] if item["source"]["kind"] == "run_control"
    )
    checkin_item = next(
        item
        for item in narrative["items"]
        if item["source"]["kind"] == "agent_run_checkin"
    )
    assert control_item["state"] == run_controls.STATE_REQUESTED
    assert "30s before observed_at" in checkin_item["summary"]
    assert checkin_item["state"] == agent_run_checkins.REPORTING_RECENTLY
    conn.close()


def test_visibility_is_each_lane_owning_surfaces_rule(tmp_path):
    """The operator sees every lane; the run's agent sees its own controls but
    no check-in lane; a plain human member sees neither — and the response
    says which lanes were withheld rather than hiding the omission."""
    conn = _migrated_conn(tmp_path / "visibility.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    _claim(conn, 2, issue["id"], run_id=RUN_ID)
    _checkin(conn)
    _control(conn)

    admin_view = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 1)
    )
    assert admin_view["visibility"] == {"run_controls": True, "checkins": True}
    assert "checkin" in _signals(admin_view)
    assert "ask" in _signals(admin_view)

    agent_view = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 2)
    )
    assert agent_view["visibility"] == {"run_controls": True, "checkins": False}
    assert "ask" in _signals(agent_view)  # its own inbox
    assert "checkin" not in _signals(agent_view)
    # Claim history is the issue's own trail — visible to every viewer.
    assert "claim" in _signals(agent_view)

    human_view = issue_narrative.build_issue_narrative(
        conn, issue["id"], actor=_actor(conn, 3)
    )
    assert human_view["visibility"] == {"run_controls": False, "checkins": False}
    assert "checkin" not in _signals(human_view)
    assert "ask" not in _signals(human_view)
    assert "claim" in _signals(human_view)
    conn.close()


def test_handoff_outside_the_event_window_still_cites_its_own_record(tmp_path):
    """When the activity window clips away a handoff's yield event, the handoff
    lane still tells the story from its own table — the projection degrades to
    the owning record, never to a dangling event reference."""
    conn = _migrated_conn(tmp_path / "window.db")
    _seed_users(conn)
    issue = _delegated_issue(conn)
    lease = _claim(conn, 2, issue["id"], run_id=RUN_ID)
    handoff = lease_commands.yield_claim(
        conn,
        actor=_actor(conn, 2),
        issue_id=issue["id"],
        **_handoff_payload(lease["generation"]),
    )

    original = issue_narrative.EVENT_WINDOW
    try:
        issue_narrative.EVENT_WINDOW = 1  # only the newest event survives
        narrative = issue_narrative.build_issue_narrative(
            conn, issue["id"], actor=_actor(conn, 1)
        )
    finally:
        issue_narrative.EVENT_WINDOW = original

    assert narrative["window"]["events_clipped"]
    handoff_item = next(i for i in narrative["items"] if i["signal"] == "handoff")
    assert handoff_item["source"]["kind"] == "claim_handoff"
    assert handoff_item["source"]["id"] == handoff["handoff_token"]
    assert handoff_item["state"] == "awaiting_resume"
    assert {
        i["summary"] for i in narrative["items"] if i["signal"] == "evidence"
    } == set(_handoff_payload(None)["evidence"])
    conn.close()
