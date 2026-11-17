"""Stop instructions survive unrelated worker registrations and quiet periods."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from athena.aegis import desk, fleet_attention
from athena.core import answerability, db, tokens, users, workers
from athena.web.admin_agents import _agents_context


@pytest.fixture
def registry(tmp_path):
    conn = db.connect(tmp_path / "workers.db")
    db.migrate(conn)
    admin = users.create_user(conn, email="op@example.invalid", name="Op", role="admin")
    agents = [
        users.create_user(
            conn, email=f"a{i}@example.invalid", name=f"A{i}", is_agent=True
        )
        for i in range(2)
    ]
    creds = [
        tokens.create_token(
            conn, user_id=a["id"], name="synthetic", scopes=["read", "issue:write"]
        )["id"]
        for a in agents
    ]

    def add(
        key,
        *,
        owner=0,
        seen="2026-01-02 00:00:00",
        asked=False,
        heard=False,
        stopped=False,
    ):
        cur = conn.execute(
            "INSERT INTO agent_workers (agent_id,worker_key,first_seen_at,last_seen_at,last_token_id,"
            "kill_requested_at,kill_requested_by,kill_acknowledged_at,stopped_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                agents[owner]["id"],
                key,
                seen,
                seen,
                creds[owner],
                seen if asked else None,
                admin["id"] if asked else None,
                seen if heard else None,
                seen if stopped else None,
            ),
        )
        return cur.lastrowid

    yield conn, admin, agents, add
    conn.close()


def test_old_instructions_are_counted_before_desk_and_cockpit_page_bounds(registry):
    conn, admin, agents, add = registry
    first_ids = [add(f"stop-{i}", asked=True) for i in range(201)]
    other_id = add("other-owner", owner=1, asked=True)
    add("already-stopped", asked=True, stopped=True)
    for i in range(501):
        add(f"ordinary-{i}", seen="2026-01-03 00:00:00")
    conn.commit()
    before = conn.total_changes
    group = desk.build_desk(conn, actor=agents[0])["asks"]["worker_kill_requests"]
    assert [row["worker_id"] for row in group["items"]] == first_ids[:50]
    assert (group["total"], group["limit"], group["clipped"]) == (201, 50, True)
    other = desk.build_desk(conn, actor=agents[1])["asks"]["worker_kill_requests"]
    assert (
        [r["worker_id"] for r in other["items"]] == [other_id]
        and other["total"] == 1
        and not other["clipped"]
    )
    page = _agents_context(conn, admin)
    assert [w["id"] for w in page["unanswered_kills"]] == first_ids[:200]
    assert page["unanswered_kills_total"] == 202
    rollup = fleet_attention.build_attention(conn, actor=admin)
    assert (
        next(s["count"] for s in rollup["signals"] if s["key"] == "unanswered_kills")
        == 202
    )
    ledger = {r["agent_id"]: r["worker_kills"] for r in page["answerability"]}
    assert ledger == {
        agents[0]["id"]: {"told_to_stop": 201, "confirmed": 1},
        agents[1]["id"]: {"told_to_stop": 1, "confirmed": 0},
    }
    assert (
        answerability.build_answerability(conn, agent_id=agents[1]["id"])["agents"][0][
            "worker_kills"
        ]
        == ledger[agents[1]["id"]]
    )
    # The general directory intentionally keeps its most recent workers first.
    assert all(w["kill_state"] == "none" for w in workers.list_workers(conn, limit=500))
    assert conn.total_changes == before


@pytest.mark.parametrize(
    "offset,expected",
    [
        (-1, "acknowledged_but_reporting"),
        (0, "acknowledged_but_reporting"),
        (1, "acknowledged"),
    ],
)
def test_desk_and_aggregates_use_individual_worker_inclusive_cutoff(
    registry, offset, expected
):
    conn, admin, agents, add = registry
    at = datetime(2026, 1, 2, 0, 1, 30, tzinfo=UTC) + timedelta(microseconds=offset)
    # A caller may supply a non-UTC offset. SQLite stores UTC second-granularity timestamps.
    now = at.astimezone(timezone(timedelta(hours=-4)))
    edge = add("edge", asked=True, heard=True)
    future = add("future", seen="2027-01-01 00:00:00", asked=True, heard=True)
    stale = add("stale-never-heard", seen="2020-01-01 00:00:00", asked=True)
    add("stopped", asked=True, stopped=True)
    add("stopped-without-being-asked", stopped=True)
    conn.commit()
    assert workers.get_worker(conn, edge, now=now)["kill_state"] == expected
    assert workers.get_worker(conn, future, now=now)["age_seconds"] == 0
    unconfirmed = [future, edge, stale] if offset <= 0 else [future, stale]
    lane = desk.build_desk(conn, actor=agents[0], now=now)["asks"][
        "worker_kill_requests"
    ]
    assert [w["worker_id"] for w in lane["items"]] == unconfirmed
    counts = answerability.build_answerability(conn, now=now)["agents"][0][
        "worker_kills"
    ]
    assert counts == {
        "told_to_stop": len(unconfirmed),
        "confirmed": 4 - len(unconfirmed),
    }
    signal = next(
        s
        for s in fleet_attention.build_attention(conn, actor=admin, now=now)["signals"]
        if s["key"] == "unanswered_kills"
    )
    assert signal["count"] == len(unconfirmed)


def test_page_counts_and_rows_use_one_snapshot_when_an_owner_stops(
    registry, monkeypatch
):
    conn, _, agents, add = registry
    first = add("one", asked=True)
    add("two", asked=True)
    conn.commit()
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    original = workers.count_unanswered
    observed = datetime.now(UTC)

    def stop_after_count(connection, **kwargs):
        total = original(connection, **kwargs)
        with db.connect(db_path) as writer:
            workers.set_stopped(writer, worker_id=first)
        writer.close()
        return total

    monkeypatch.setattr(workers, "count_unanswered", stop_after_count)
    rows, total = workers.unanswered_page(
        conn, limit=1, now=observed, agent_id=agents[0]["id"]
    )
    assert total == 2 and [w["id"] for w in rows] == [first]
    monkeypatch.undo()
    assert workers.count_unanswered(conn, now=observed) == 1


def test_count_aggregation_does_not_materialize_registry_rows(registry, monkeypatch):
    conn, _, agents, add = registry
    for i in range(8):
        add(f"stop-{i}", asked=True)
    conn.commit()
    hydrated = []
    source = workers._projection

    def track(row, **kwargs):
        hydrated.append(row["id"])
        return source(row, **kwargs)

    monkeypatch.setattr(workers, "_projection", track)
    now = datetime.now(UTC)
    assert workers.count_unanswered(conn, now=now) == 8
    assert (
        workers.kill_counts_by_agent(conn, agent_id=agents[0]["id"], now=now)[
            agents[0]["id"]
        ]["told_to_stop"]
        == 8
    )
    assert not hydrated
    rows, total = workers.unanswered_page(conn, limit=4, now=now)
    assert total == 8 and [r["id"] for r in rows] == hydrated and len(hydrated) == 4
