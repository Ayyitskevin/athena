"""Fixes from the run-replay subsystem audit.

Two verified defects:
  1. reconstruct_runs silently clipped the oldest run at the `limit` window and
     reported the fragment's count/start as if whole. It now flags that run
     "partial" so callers don't read a clipped total as complete.
  2. The X-Athena-Run / X-Athena-Parent-Run headers were latin-1 decoded while the
     ?run_id= / ?parent_run_id= query params Starlette hands us are UTF-8 — so a
     non-ASCII run id stored from a header wouldn't match its own replay filter.
     The middleware now decodes the headers as UTF-8 to match.
"""
import asyncio

from fastapi.testclient import TestClient

from athena.core import activity, db, run_context
from athena.main import RunContextMiddleware, create_app

H1 = {"X-Athena-Actor": "1"}


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _user(conn, name="Bot"):
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (f"{name}@e.com", name))
    conn.commit()


def _event(conn, *, ts, run_id=None, verb="created"):
    conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail, created_at, run_id) "
        "VALUES (1, ?, 'issue', 1, '', ?, ?)",
        (verb, ts, run_id),
    )
    conn.commit()


# --- Fix 1: the oldest run is flagged partial when the window clips it -------


def test_clipped_oldest_run_is_flagged_partial(tmp_path):
    conn = _migrated_conn(tmp_path / "clip.db")
    _user(conn)
    for i in range(5):
        _event(conn, ts=f"2026-06-27 09:00:0{i}", run_id="r1")  # one 5-event run

    # A full window of 3 loads only the tail — the run is clipped, and now SAYS so.
    clipped = activity.reconstruct_runs(conn, actor_id=1, gap_seconds=1800, limit=3)
    assert len(clipped) == 1
    assert clipped[0]["partial"] is True
    assert clipped[0]["event_count"] == 3  # still the fragment, but no longer silent

    # A generous window sees the whole run — complete, not partial.
    whole = activity.reconstruct_runs(conn, actor_id=1, limit=200)
    assert whole[0]["partial"] is False
    assert whole[0]["event_count"] == 5
    conn.close()


def test_only_the_oldest_run_is_marked_partial(tmp_path):
    # Two runs fill the window exactly; only the oldest can be clipped, so only it is
    # flagged (conservatively — a full window means "there may be more behind this").
    conn = _migrated_conn(tmp_path / "oldest.db")
    _user(conn)
    _event(conn, ts="2026-06-27 09:00:00", run_id="r1")
    _event(conn, ts="2026-06-27 09:00:01", run_id="r1")
    _event(conn, ts="2026-06-27 09:00:02", run_id="r2")

    runs = activity.reconstruct_runs(conn, actor_id=1, limit=3)  # window full (3 == 3)
    assert [r["run_id"] for r in runs] == ["r2", "r1"]  # newest first
    assert runs[0]["partial"] is False  # newest run is never clipped
    assert runs[1]["partial"] is True   # oldest might be
    conn.close()


def test_short_window_marks_nothing_partial(tmp_path):
    conn = _migrated_conn(tmp_path / "short.db")
    _user(conn)
    _event(conn, ts="2026-06-27 09:00:00", run_id="r1")
    _event(conn, ts="2026-06-27 09:00:01", run_id="r1")
    # Two events, big window — we've seen everything; nothing is partial.
    runs = activity.reconstruct_runs(conn, actor_id=1, limit=200)
    assert len(runs) == 1 and runs[0]["partial"] is False
    conn.close()


def test_runs_api_exposes_partial(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "H"})
        for t in ("a", "b", "c"):
            client.post("/issues", json={"title": t}, headers=H1)  # 3 created events
        # limit=2 fills the window, so the (single, gap-grouped) run is partial.
        runs = client.get("/activity/runs?actor_id=1&limit=2", headers=H1).json()
        assert runs[0]["partial"] is True


# --- Fix 2: run headers decode as UTF-8, matching the query-param filter -----


def test_run_headers_decoded_as_utf8():
    captured = {}

    async def app(scope, receive, send):
        captured["run"] = run_context.get_run_id()
        captured["parent"] = run_context.get_parent_run_id()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        pass

    # Real clients that need a non-ASCII run id send its UTF-8 bytes; latin-1 would
    # have turned these into mojibake that no UTF-8 ?run_id= filter could match.
    scope = {
        "type": "http",
        "headers": [
            (b"x-athena-run", "café-7".encode("utf-8")),
            (b"x-athena-parent-run", "naïve-1".encode("utf-8")),
        ],
    }
    asyncio.run(RunContextMiddleware(app)(scope, receive, send))

    assert captured["run"] == "café-7"
    assert captured["parent"] == "naïve-1"
    # And the context is reset once the request ends — no leak past the scope.
    assert run_context.get_run_id() is None
    assert run_context.get_parent_run_id() is None
