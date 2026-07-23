"""Deterministic runs — stamping events with an explicit run id and replaying by it.

reconstruct_runs guesses run boundaries from a time gap; this is the deterministic
counterpart. A client tags the requests of one run with an X-Athena-Run header; an
ASGI middleware stashes it request-scoped; activity.record() stamps every event with
it (no recorder threads it through); and the feeds can replay exactly that run's
events by id. These tests pin: the header normalization, that record() stamps the
ambient id (and only within its scope), that an HTTP request carries the id onto the
events it causes — including into the sync handler — and that /activity and /events
filter by run_id while untagged actions stay null and backward-compatible.
"""

from fastapi.testclient import TestClient

from athena.core import activity, db, run_context
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _user(conn, name="Human"):
    conn.execute(
        "INSERT INTO users (email, name) VALUES (?, ?)", (f"{name}@e.com", name)
    )
    conn.commit()


# --- normalization ----------------------------------------------------------


def test_normalize_run_id():
    assert run_context.normalize(None) is None
    assert run_context.normalize("   ") is None  # whitespace → untagged
    assert run_context.normalize("  job-7 ") == "job-7"  # trimmed
    assert run_context.normalize("u" * 500) == "u" * 200  # bounded, not rejected


# --- data layer: record() stamps the ambient id -----------------------------


def test_record_stamps_ambient_run_id_within_scope(tmp_path):
    conn = _migrated_conn(tmp_path / "stamp.db")
    _user(conn)
    token = run_context.set_run_id("run-abc")
    try:
        tagged = activity.record(
            conn, actor_id=1, verb="created", target_kind="issue", target_id=1
        )
    finally:
        run_context.reset_run_id(token)
    # Stamped while the run was in force...
    assert tagged["run_id"] == "run-abc"
    # ...and untagged once it's reset — the id never bleeds past its scope.
    untagged = activity.record(
        conn, actor_id=1, verb="commented", target_kind="issue", target_id=1
    )
    assert untagged["run_id"] is None
    conn.close()


def test_list_filters_by_run_id(tmp_path):
    conn = _migrated_conn(tmp_path / "filter.db")
    _user(conn)
    for verb, run in [("created", "r1"), ("changed_status", "r1"), ("commented", "r2")]:
        token = run_context.set_run_id(run)
        try:
            activity.record(
                conn, actor_id=1, verb=verb, target_kind="issue", target_id=1
            )
        finally:
            run_context.reset_run_id(token)

    r1 = activity.list_activity(conn, run_id="r1")
    assert {e["verb"] for e in r1} == {"created", "changed_status"}
    # The forward (events) cursor filters by the same id.
    r2 = activity.list_events(conn, run_id="r2")
    assert [e["verb"] for e in r2] == ["commented"]
    conn.close()


# --- HTTP: the header rides onto the events a request causes -----------------


def test_http_header_tags_events_and_replays(tmp_path):
    with TestClient(create_app(tmp_path / "http.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        # One issue created under a run id (proves the contextvar reaches the sync
        # handler, where record() runs), one created untagged.
        tagged = client.post(
            "/issues",
            json={"title": "tagged"},
            headers={**H1, "X-Athena-Run": "run-42"},
        ).json()
        client.post("/issues", json={"title": "untagged"}, headers=H1)

        # Replay run-42: exactly its events, all stamped, all on the tagged issue.
        replay = client.get("/activity?run_id=run-42", headers=H1).json()
        assert replay
        assert all(e["run_id"] == "run-42" for e in replay)
        assert all(e["target_id"] == tagged["id"] for e in replay)

        # The untagged action stays null and still shows in the unfiltered feed —
        # the column is additive, existing behaviour unchanged.
        everything = client.get("/activity", headers=H1).json()
        assert any(e["run_id"] is None for e in everything)


def test_events_replay_by_run_id(tmp_path):
    with TestClient(create_app(tmp_path / "events.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        client.post(
            "/issues", json={"title": "a"}, headers={**H1, "X-Athena-Run": "r1"}
        )
        client.post(
            "/issues", json={"title": "b"}, headers={**H1, "X-Athena-Run": "r2"}
        )

        r1 = client.get("/events?run_id=r1", headers=H1).json()["events"]
        assert len(r1) == 1
        assert r1[0]["run_id"] == "r1" and r1[0]["verb"] == "created"


def test_untagged_request_records_null_run_id(tmp_path):
    # WHY: a request with no header (every browser action) must leave run_id NULL,
    # and a later tagged request must not inherit a stale id — proves the middleware
    # resets between requests.
    with TestClient(create_app(tmp_path / "reset.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        client.post("/issues", json={"title": "plain"}, headers=H1)
        feed = client.get("/activity", headers=H1).json()
        assert feed and all(e["run_id"] is None for e in feed)
