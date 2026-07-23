"""Production contract tests for visibility-safe fleet throughput metrics."""

from __future__ import annotations

import asyncio
from datetime import date
from html import escape
import json
import shutil
import sqlite3

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from athena.aegis import fleet_metrics, issue_commands, issues, statuses
from athena.aegis.fleet_metrics_api import FlowOut
from athena.core import activity, db, users
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena.web import fleet_metrics as web_fleet_metrics


H_ADMIN = {"X-Athena-Actor": "1"}


def _headers(actor_id: int) -> dict[str, str]:
    return {"X-Athena-Actor": str(actor_id)}


def _bootstrap(client: TestClient) -> dict[str, dict]:
    admin_response = client.post(
        "/users",
        json={
            "email": "admin@metrics.test",
            "name": "Admin",
            "password": "pw",
        },
    )
    assert admin_response.status_code == 201
    actors = {"admin": admin_response.json()}
    for key, payload in {
        "human": {
            "email": "human@metrics.test",
            "name": "Human",
            "role": "member",
        },
        "agent": {
            "email": "agent@metrics.test",
            "name": "Agent",
            "role": "member",
            "is_agent": True,
        },
        "outsider": {
            "email": "outsider@metrics.test",
            "name": "Outsider",
            "role": "viewer",
        },
    }.items():
        response = client.post("/users", json=payload, headers=H_ADMIN)
        assert response.status_code == 201, response.text
        actors[key] = response.json()
    return actors


def _project(
    client: TestClient,
    *,
    name: str,
    key: str,
    actor_id: int = 1,
) -> dict:
    response = client.post(
        "/projects",
        json={"name": name, "key": key},
        headers=_headers(actor_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _issue(
    client: TestClient,
    *,
    title: str,
    actor_id: int = 1,
    project_id: int | None = None,
    status: str | None = None,
) -> dict:
    payload: dict[str, object] = {"title": title, "project_id": project_id}
    if status is not None:
        payload["status"] = status
    response = client.post("/issues", json=payload, headers=_headers(actor_id))
    assert response.status_code == 201, response.text
    return response.json()


def _set_status(
    client: TestClient, issue_id: int, status: str, *, actor_id: int
) -> None:
    response = client.patch(
        f"/issues/{issue_id}",
        json={"status": status},
        headers=_headers(actor_id),
    )
    assert response.status_code == 200, response.text


def _stamp_latest(
    conn,
    issue_id: int,
    verb: str,
    created_at: str,
) -> int:
    row = conn.execute(
        "SELECT id FROM activity WHERE target_kind = 'issue' "
        "AND target_id = ? AND verb = ? ORDER BY id DESC LIMIT 1",
        (issue_id, verb),
    ).fetchone()
    assert row is not None
    conn.execute(
        "UPDATE activity SET created_at = ? WHERE id = ?",
        (created_at, row["id"]),
    )
    conn.commit()
    return row["id"]


def _metrics(
    client: TestClient,
    *,
    start: str,
    end: str,
    headers: dict[str, str] | None = None,
    extra: str = "",
) -> dict:
    response = client.get(
        f"/fleet/metrics?start={start}&end={end}{extra}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _vary(response) -> set[str]:
    return {
        value.strip().lower()
        for value in response.headers.get("vary", "").split(",")
        if value.strip()
    }


def test_query_normalization_is_strict_and_utc_bounded():
    default = fleet_metrics.parse_query(today=date(2026, 1, 31))
    assert default.start.isoformat() == "2026-01-02"
    assert default.end.isoformat() == "2026-02-01"
    assert (default.end - default.start).days == 30

    exact = fleet_metrics.parse_query(
        start="2026-01-01",
        end="2026-04-01",
        project_id="0007",
        actor_id=8,
        actor_limit="100",
    )
    assert exact.project_id == 7
    assert exact.actor_id == 8
    assert exact.actor_limit == 100
    assert (exact.end - exact.start).days == 90

    invalid = [
        {"start": "2026-01-01"},
        {"start": "2026-01-02", "end": "2026-01-02"},
        {"start": "2026-01-02", "end": "2026-04-03"},
        {"start": "2026-1-02", "end": "2026-01-03"},
        {"start": "2026-02-30", "end": "2026-03-01"},
        {"start": "2026-01-02T00:00:00Z", "end": "2026-01-03"},
        {"project_id": "+1"},
        {"project_id": "1.0"},
        {"project_id": "１２"},
        {"project_id": True},
        {"project_id": str(fleet_metrics.MAX_SQLITE_INTEGER + 1)},
        {"actor_id": "0"},
        {"actor_limit": "101"},
    ]
    for kwargs in invalid:
        with pytest.raises(fleet_metrics.FleetMetricsError):
            fleet_metrics.parse_query(**kwargs)

    with pytest.raises(fleet_metrics.FleetMetricsError):
        fleet_metrics.parse_query_pairs([("timezone", "UTC")])
    with pytest.raises(fleet_metrics.FleetMetricsError):
        fleet_metrics.parse_query_pairs(
            [("start", "2026-01-01"), ("start", "2026-01-02")]
        )


def test_response_models_reject_scalar_coercion():
    with pytest.raises(ValidationError):
        FlowOut.model_validate({"created": "1", "completed": 0, "net": 1})


def test_preset_links_share_one_utc_end_boundary():
    assert web_fleet_metrics._preset_links(date(2026, 6, 10)) == [
        {
            "days": 7,
            "href": ("/aegis/fleet-metrics?start=2026-06-03&end=2026-06-10"),
        },
        {
            "days": 30,
            "href": ("/aegis/fleet-metrics?start=2026-05-11&end=2026-06-10"),
        },
        {
            "days": 90,
            "href": ("/aegis/fleet-metrics?start=2026-03-12&end=2026-06-10"),
        },
    ]


def test_completion_semantics_cycle_reopen_attribution_and_mutable_state(
    tmp_path,
):
    db_file = tmp_path / "semantics.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        human_id = actors["human"]["id"]
        agent_id = actors["agent"]["id"]
        project = _project(client, name="Delivery", key="DEL", actor_id=human_id)
        conn = db.connect(db_file)
        assert statuses.add_status(conn, project["id"], "shipped", "done") is None

        issue = _issue(
            client,
            title="Cross the finish line",
            actor_id=human_id,
            project_id=project["id"],
        )
        _stamp_latest(conn, issue["id"], "created", "2026-01-01 00:00:00")

        _set_status(client, issue["id"], "shipped", actor_id=human_id)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-05 00:00:00")
        _set_status(client, issue["id"], "in_progress", actor_id=human_id)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-06 00:00:00")

        assigned = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": agent_id},
            headers=H_ADMIN,
        )
        assert assigned.status_code == 200
        _set_status(client, issue["id"], "shipped", actor_id=agent_id)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-07 00:00:00")

        # Lease/run completion is coordination, not issue lifecycle completion.
        activity.record(
            conn,
            actor_id=agent_id,
            verb="claim_completed",
            target_kind="issue",
            target_id=issue["id"],
        )
        _stamp_latest(conn, issue["id"], "claim_completed", "2026-01-07 12:00:00")

        archived = client.post(
            f"/issues/{issue['id']}/archive", headers=_headers(human_id)
        )
        assert archived.status_code == 200

        # Mutable configuration and current actor classification must not rewrite
        # the typed event-time facts.
        conn.execute(
            "UPDATE project_statuses SET category = 'doing' "
            "WHERE project_id = ? AND name = 'shipped'",
            (project["id"],),
        )
        users.set_agent(conn, agent_id, False)
        conn.commit()

        result = _metrics(
            client,
            start="2026-01-02",
            end="2026-01-08",
            headers=H_ADMIN,
        )
        assert result["flow"] == {"created": 0, "completed": 2, "net": -2}
        assert result["cycle_time"] == {
            "visibility_complete": True,
            "median_seconds": 216000.0,
            "sample_count": 2,
            "excluded_completions": 0,
        }
        assert result["completion_by_actor_type"] == {
            "human": 1,
            "agent": 1,
            "unknown": 0,
        }
        assert [
            (row["actor_name"], row["actor_type"], row["completions"])
            for row in result["actors"]
        ] == [("Human", "human", 1), ("Agent", "agent", 1)]
        assert result["coverage"]["complete"] is True

        bounded = _metrics(
            client,
            start="2026-01-02",
            end="2026-01-08",
            headers=H_ADMIN,
            extra="&actor_limit=1",
        )
        assert bounded["completion_by_actor_type"] == {
            "human": 1,
            "agent": 1,
            "unknown": 0,
        }
        assert len(bounded["actors"]) == 1
        assert bounded["limits"]["actor_rows_available"] == 2
        assert bounded["limits"]["actor_rows_truncated"] is True
        assert bounded["actors"][0]["actor_id"] == human_id

        conn.execute("UPDATE users SET name = 'Zulu human' WHERE id = ?", (human_id,))
        conn.execute(
            "UPDATE users SET name = 'Aardvark agent' WHERE id = ?", (agent_id,)
        )
        conn.commit()
        renamed = _metrics(
            client,
            start="2026-01-02",
            end="2026-01-08",
            headers=H_ADMIN,
            extra="&actor_limit=1",
        )
        assert renamed["actors"][0]["actor_id"] == human_id

        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM users WHERE id = ?", (human_id,))
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        deleted = _metrics(
            client,
            start="2026-01-02",
            end="2026-01-08",
            headers=H_ADMIN,
            extra="&actor_limit=1",
        )
        assert deleted["actors"][0]["actor_id"] == human_id
        assert deleted["actors"][0]["actor_name"] == "Unknown actor"
        conn.close()


def test_malformed_reopen_timestamp_excludes_cycle_instead_of_skipping_boundary(
    tmp_path,
):
    db_file = tmp_path / "malformed-reopen.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        issue = _issue(client, title="Malformed reopen")
        conn = db.connect(db_file)
        _stamp_latest(conn, issue["id"], "created", "2026-01-01 00:00:00")
        _set_status(client, issue["id"], "done", actor_id=1)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-02 00:00:00")
        _set_status(client, issue["id"], "open", actor_id=1)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-03Tbad")
        _set_status(client, issue["id"], "done", actor_id=1)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-04 00:00:00")

        result = _metrics(
            client,
            start="2026-01-04",
            end="2026-01-05",
            headers=H_ADMIN,
        )
        assert result["flow"] == {"created": 0, "completed": 1, "net": -1}
        assert result["cycle_time"] == {
            "visibility_complete": True,
            "median_seconds": None,
            "sample_count": 0,
            "excluded_completions": 1,
        }
        assert result["coverage"]["complete"] is False
        conn.close()


def test_done_to_done_is_not_a_completion_and_assignee_is_not_attribution(
    tmp_path,
):
    db_file = tmp_path / "done-to-done.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        human_id = actors["human"]["id"]
        agent_id = actors["agent"]["id"]
        project = _project(client, name="Release", key="REL", actor_id=human_id)
        conn = db.connect(db_file)
        assert statuses.add_status(conn, project["id"], "shipped", "done") is None
        assert statuses.add_status(conn, project["id"], "verified", "done") is None

        issue = _issue(
            client,
            title="Attribution follows the event performer",
            actor_id=human_id,
            project_id=project["id"],
        )
        _stamp_latest(conn, issue["id"], "created", "2026-01-01 00:00:00")
        assigned = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": agent_id},
            headers=H_ADMIN,
        )
        assert assigned.status_code == 200

        _set_status(client, issue["id"], "shipped", actor_id=human_id)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-02 00:00:00")
        _set_status(client, issue["id"], "verified", actor_id=agent_id)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-01-03 00:00:00")

        result = _metrics(
            client,
            start="2026-01-01",
            end="2026-01-04",
            headers=H_ADMIN,
        )
        assert result["flow"] == {"created": 1, "completed": 1, "net": 0}
        assert result["completion_by_actor_type"] == {
            "human": 1,
            "agent": 0,
            "unknown": 0,
        }
        assert [
            (row["actor_name"], row["actor_type"], row["completions"])
            for row in result["actors"]
        ] == [("Human", "human", 1)]
        conn.close()


def test_same_name_cross_project_category_moves_complete_and_reopen(tmp_path):
    db_file = tmp_path / "same-name-category.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        source = _project(client, name="Source", key="SRC")
        destination = _project(client, name="Destination", key="DST")
        conn = db.connect(db_file)
        assert statuses.remove_status(conn, destination["id"], "open") is None
        assert statuses.add_status(conn, destination["id"], "open", "done") is None

        issue = _issue(
            client,
            title="Same text, different lifecycle meaning",
            project_id=source["id"],
            status="open",
        )
        _stamp_latest(conn, issue["id"], "created", "2026-01-01 00:00:00")

        facts_before_noop = conn.execute(
            "SELECT COUNT(*) FROM issue_lifecycle_facts WHERE issue_id = ?",
            (issue["id"],),
        ).fetchone()[0]
        noop = client.patch(
            f"/issues/{issue['id']}",
            json={"status": "open"},
            headers=H_ADMIN,
        )
        assert noop.status_code == 200
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM issue_lifecycle_facts WHERE issue_id = ?",
                (issue["id"],),
            ).fetchone()[0]
            == facts_before_noop
        )

        for project_id, timestamp in (
            (destination["id"], "2026-01-02 00:00:00"),
            (source["id"], "2026-01-03 00:00:00"),
            (destination["id"], "2026-01-04 00:00:00"),
        ):
            moved = client.patch(
                f"/issues/{issue['id']}",
                json={"project_id": project_id},
                headers=H_ADMIN,
            )
            assert moved.status_code == 200, moved.text
            _stamp_latest(conn, issue["id"], "changed_status", timestamp)

        result = _metrics(
            client,
            start="2026-01-01",
            end="2026-01-05",
            headers=H_ADMIN,
        )
        assert result["flow"] == {"created": 1, "completed": 2, "net": -1}
        assert result["cycle_time"] == {
            "visibility_complete": True,
            "median_seconds": 86400.0,
            "sample_count": 2,
            "excluded_completions": 0,
        }
        transitions = conn.execute(
            "SELECT before_category, after_category "
            "FROM issue_lifecycle_facts WHERE issue_id = ? "
            "AND event_kind = 'status_transition' ORDER BY event_id",
            (issue["id"],),
        ).fetchall()
        assert [tuple(row) for row in transitions] == [
            ("todo", "done"),
            ("done", "todo"),
            ("todo", "done"),
        ]
        conn.close()


def test_half_open_boundary_direct_terminal_archive_and_no_data(tmp_path):
    db_file = tmp_path / "boundaries.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        included = _issue(
            client,
            title="Included direct completion",
            status="done",
        )
        excluded = _issue(
            client,
            title="Excluded at end",
            status="done",
        )
        conn = db.connect(db_file)
        _stamp_latest(conn, included["id"], "created", "2026-02-01 00:00:00")
        _stamp_latest(conn, excluded["id"], "created", "2026-02-02 00:00:00")
        assert (
            client.post(
                f"/issues/{included['id']}/archive", headers=H_ADMIN
            ).status_code
            == 200
        )

        result = _metrics(
            client,
            start="2026-02-01",
            end="2026-02-02",
            headers=H_ADMIN,
        )
        assert result["flow"] == {"created": 1, "completed": 1, "net": 0}
        assert result["cycle_time"]["median_seconds"] == 0.0
        assert result["cycle_time"]["sample_count"] == 1

        empty = _metrics(
            client,
            start="2026-02-03",
            end="2026-02-04",
            headers=H_ADMIN,
        )
        assert empty["flow"] == {"created": 0, "completed": 0, "net": 0}
        assert empty["cycle_time"] == {
            "visibility_complete": True,
            "median_seconds": None,
            "sample_count": 0,
            "excluded_completions": 0,
        }
        assert empty["actors"] == []
        conn.close()


def test_hidden_rows_do_not_change_any_payload_or_consume_caps(tmp_path, monkeypatch):
    db_file = tmp_path / "privacy.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        outsider = _headers(actors["outsider"]["id"])
        visible = _issue(client, title="Visible", status="done")
        conn = db.connect(db_file)
        _stamp_latest(conn, visible["id"], "created", "2026-03-02 00:00:00")
        before = _metrics(
            client,
            start="2026-03-01",
            end="2026-03-10",
            headers=outsider,
        )

        private = _project(client, name="Private", key="PVT")
        hidden = _issue(
            client,
            title="Classified actor and project",
            project_id=private["id"],
            status="done",
        )
        _stamp_latest(conn, hidden["id"], "created", "2026-03-03 00:00:00")
        assert (
            client.put(
                f"/projects/{private['id']}/visibility",
                json={"visibility": "private"},
                headers=H_ADMIN,
            ).status_code
            == 200
        )

        after = _metrics(
            client,
            start="2026-03-01",
            end="2026-03-10",
            headers=outsider,
        )
        assert after == before

        # Caps are applied only after the shared SQL visibility predicate.
        monkeypatch.setattr(fleet_metrics, "EVIDENCE_EVENT_LIMIT", 1)
        still_exact = _metrics(
            client,
            start="2026-03-01",
            end="2026-03-10",
            headers=outsider,
        )
        assert still_exact["flow"] == before["flow"]

        admin_over_cap = client.get(
            "/fleet/metrics?start=2026-03-01&end=2026-03-10",
            headers=H_ADMIN,
        )
        assert admin_over_cap.status_code == 422
        assert "narrow" in admin_over_cap.json()["detail"]
        conn.close()


def test_visible_legacy_imported_and_orphan_evidence_is_reported_not_guessed(
    tmp_path,
):
    db_file = tmp_path / "coverage.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        outsider = _headers(actors["outsider"]["id"])
        visible = _issue(client, title="Typed visible", status="done")
        orphan = _issue(client, title="Soon orphaned", status="done")
        conn = db.connect(db_file)
        _stamp_latest(conn, visible["id"], "created", "2026-04-02 00:00:00")
        _stamp_latest(conn, orphan["id"], "created", "2026-04-03 00:00:00")

        conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, created_at, "
            "visibility_restricted) "
            "VALUES (?, 'changed_status', 'issue', ?, 'open → done', ?, 0)",
            (1, visible["id"], "2026-04-04 00:00:00"),
        )
        conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, created_at, "
            "imported_at, visibility_restricted) "
            "VALUES (?, 'created', 'issue', ?, '', ?, ?, 0)",
            (
                1,
                visible["id"],
                "2026-04-05 00:00:00",
                "2026-04-06 00:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, created_at, "
            "visibility_restricted) "
            "VALUES (?, 'created', 'issue', 9999, '', ?, 0)",
            (1, "2026-04-06 12:00:00"),
        )
        conn.commit()

        outsider_before_orphan = _metrics(
            client,
            start="2026-04-01",
            end="2026-04-10",
            headers=outsider,
        )
        assert outsider_before_orphan["flow"] == {
            "created": 2,
            "completed": 2,
            "net": 0,
        }
        assert outsider_before_orphan["coverage"]["excluded_legacy_events"] == 1
        assert outsider_before_orphan["coverage"]["excluded_imported_events"] == 1

        conn.execute("DELETE FROM issues WHERE id = ?", (orphan["id"],))
        conn.commit()
        outsider_after_orphan = _metrics(
            client,
            start="2026-04-01",
            end="2026-04-10",
            headers=outsider,
        )
        assert outsider_after_orphan["flow"] == {
            "created": 1,
            "completed": 1,
            "net": 0,
        }
        assert outsider_after_orphan["cycle_time"] == {
            "visibility_complete": False,
            "median_seconds": None,
            "sample_count": 0,
            "excluded_completions": 1,
        }
        assert outsider_after_orphan["completion_by_actor_type"] == {
            "human": 1,
            "agent": 0,
            "unknown": 0,
        }
        assert outsider_after_orphan["actors"][0]["completions"] == 1
        assert outsider_after_orphan["coverage"] == {
            **outsider_before_orphan["coverage"],
            "visible_candidate_events": 3,
            "included_typed_events": 1,
            "cycle_samples_excluded": 1,
        }
        assert outsider_after_orphan["coverage"]["excluded_orphan_events"] == 0

        admin = _metrics(
            client,
            start="2026-04-01",
            end="2026-04-10",
            headers=H_ADMIN,
        )
        assert admin["flow"] == {"created": 2, "completed": 2, "net": 0}
        assert admin["cycle_time"] == {
            "visibility_complete": True,
            "median_seconds": 0.0,
            "sample_count": 2,
            "excluded_completions": 0,
        }
        assert admin["actors"][0]["completions"] == 2
        assert admin["coverage"]["included_typed_events"] == 2
        assert admin["coverage"]["excluded_orphan_events"] == 1
        assert admin["coverage"]["complete"] is False
        conn.close()


def test_project_filter_reports_visible_untyped_activity_scope(tmp_path):
    db_file = tmp_path / "project-legacy-coverage.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        project = _project(client, name="Legacy scope", key="LEG")
        issue = _issue(
            client,
            title="Typed creation plus untyped transition",
            project_id=project["id"],
        )
        conn = db.connect(db_file)
        _stamp_latest(conn, issue["id"], "created", "2026-04-01 00:00:00")
        activity.record(
            conn,
            actor_id=1,
            verb="changed_status",
            target_kind="issue",
            target_id=issue["id"],
            issue_project_ids={project["id"]},
        )
        _stamp_latest(conn, issue["id"], "changed_status", "2026-04-02 00:00:00")

        result = _metrics(
            client,
            start="2026-04-02",
            end="2026-04-03",
            headers=H_ADMIN,
            extra=f"&project_id={project['id']}",
        )
        assert result["flow"] == {"created": 0, "completed": 0, "net": 0}
        assert result["coverage"]["visible_candidate_events"] == 1
        assert result["coverage"]["excluded_legacy_events"] == 1
        assert result["coverage"]["complete"] is False
        conn.close()


def test_deleted_highest_issue_id_is_never_rebound_to_new_generation(
    tmp_path,
):
    db_file = tmp_path / "issue-id-generation.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        deleted = _issue(client, title="Deleted generation", status="done")
        conn = db.connect(db_file)
        _stamp_latest(conn, deleted["id"], "created", "2026-04-04 00:00:00")
        conn.execute("DELETE FROM issues WHERE id = ?", (deleted["id"],))
        conn.commit()

        replacement = _issue(client, title="Fresh generation", status="done")
        assert replacement["id"] > deleted["id"]
        _stamp_latest(conn, replacement["id"], "created", "2026-04-05 00:00:00")

        result = _metrics(
            client,
            start="2026-04-04",
            end="2026-04-06",
            headers=H_ADMIN,
        )
        assert result["flow"] == {"created": 2, "completed": 2, "net": 0}
        assert result["cycle_time"]["sample_count"] == 2
        assert result["coverage"]["excluded_orphan_events"] == 0
        assert {
            row["issue_id"]
            for row in conn.execute(
                "SELECT issue_id FROM issue_lifecycle_facts "
                "WHERE event_kind = 'created'"
            )
        } == {deleted["id"], replacement["id"]}
        conn.close()


def test_hidden_lifecycle_predecessor_excludes_cycle_without_hiding_completion(
    tmp_path,
):
    db_file = tmp_path / "hidden-chain.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        outsider = _headers(actors["outsider"]["id"])
        private = _project(client, name="Secret chain", key="SCH")
        issue = _issue(client, title="Moves through secret")
        conn = db.connect(db_file)
        _stamp_latest(conn, issue["id"], "created", "2026-05-01 00:00:00")
        assert (
            client.put(
                f"/projects/{private['id']}/visibility",
                json={"visibility": "private"},
                headers=H_ADMIN,
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"/issues/{issue['id']}",
                json={"project_id": private["id"]},
                headers=H_ADMIN,
            ).status_code
            == 200
        )
        _set_status(client, issue["id"], "in_progress", actor_id=1)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-05-02 00:00:00")
        assert (
            client.patch(
                f"/issues/{issue['id']}",
                json={"project_id": None},
                headers=H_ADMIN,
            ).status_code
            == 200
        )
        _set_status(client, issue["id"], "done", actor_id=1)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-05-03 00:00:00")

        result = _metrics(
            client,
            start="2026-05-03",
            end="2026-05-04",
            headers=outsider,
        )
        assert result["flow"] == {"created": 0, "completed": 1, "net": -1}
        assert result["cycle_time"] == {
            "visibility_complete": False,
            "median_seconds": None,
            "sample_count": 0,
            "excluded_completions": 1,
        }
        assert result["coverage"]["cycle_samples_excluded"] == 1
        assert "Secret chain" not in str(result)
        conn.close()


def test_hidden_same_category_detour_cannot_change_partial_visibility_payload(
    tmp_path,
):
    def projection(db_file, *, with_detour: bool) -> dict:
        with TestClient(create_app(db_file)) as client:
            actors = _bootstrap(client)
            outsider = _headers(actors["outsider"]["id"])
            private = _project(client, name="Private detour", key="PDT")
            assert (
                client.put(
                    f"/projects/{private['id']}/visibility",
                    json={"visibility": "private"},
                    headers=H_ADMIN,
                ).status_code
                == 200
            )
            issue = _issue(client, title="Same visible lifecycle")
            conn = db.connect(db_file)
            _stamp_latest(conn, issue["id"], "created", "2026-05-01 00:00:00")

            if with_detour:
                for project_id in (private["id"], None):
                    moved = client.patch(
                        f"/issues/{issue['id']}",
                        json={"project_id": project_id},
                        headers=H_ADMIN,
                    )
                    assert moved.status_code == 200

            _set_status(client, issue["id"], "done", actor_id=1)
            _stamp_latest(conn, issue["id"], "changed_status", "2026-05-03 00:00:00")
            result = _metrics(
                client,
                start="2026-05-01",
                end="2026-05-04",
                headers=outsider,
            )
            conn.close()
            return result

    baseline = projection(tmp_path / "public-only.db", with_detour=False)
    hidden_detour = projection(tmp_path / "private-detour.db", with_detour=True)
    assert hidden_detour == baseline
    assert baseline["flow"] == {"created": 1, "completed": 1, "net": 0}
    assert baseline["cycle_time"] == {
        "visibility_complete": False,
        "median_seconds": None,
        "sample_count": 0,
        "excluded_completions": 1,
    }


@pytest.mark.parametrize(
    "query",
    [
        "unknown=1",
        "start=2026-01-01",
        "start=2026-01-01&start=2026-01-02&end=2026-01-03",
        "start=2026-01-02&end=2026-01-02",
        "start=2026-01-01&end=2026-04-02",
        "start=2026-01-01T00:00:00Z&end=2026-01-02",
        "project_id=%2B1",
        "project_id=1.0",
        "project_id=9223372036854775808",
        "actor_id=-1",
        "actor_limit=0",
        "actor_limit=101",
    ],
)
def test_rest_rejects_malformed_or_oversized_criteria(tmp_path, query):
    with TestClient(create_app(tmp_path / "invalid.db")) as client:
        _bootstrap(client)
        response = client.get(f"/fleet/metrics?{query}", headers=H_ADMIN)
        assert response.status_code == 422
        assert response.headers["cache-control"] == "private, no-store"


def test_very_large_decimal_ids_are_bounded_before_conversion(tmp_path):
    huge_id = "9" * 5_000
    with TestClient(create_app(tmp_path / "huge-filter.db")) as client:
        _bootstrap(client)
        rest = client.get(
            f"/fleet/metrics?start=2026-01-01&end=2026-01-02&project_id={huge_id}",
            headers=H_ADMIN,
        )
        assert rest.status_code == 422
        assert rest.headers["cache-control"] == "private, no-store"

        web = client.get(
            f"/aegis/fleet-metrics?start=2026-01-01&end=2026-01-02&project_id={huge_id}"
        )
        assert web.status_code == 400
        assert web.headers["cache-control"] == "private, no-store"


def test_dependency_rate_limit_errors_keep_metrics_private_headers(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "rate-limit.db",
            anon_rate_limit_per_minute=1,
        )
    ) as client:
        path = "/fleet/metrics?start=2026-01-01&end=2026-01-02"
        assert client.get(path).status_code == 200
        limited = client.get(path)
        assert limited.status_code == 429
        assert limited.headers["cache-control"] == "private, no-store"
        assert {"authorization", "x-athena-actor"} <= _vary(limited)


def test_rest_web_tokens_mcp_client_and_private_headers_share_contract(tmp_path):
    db_file = tmp_path / "surfaces.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        issue = _issue(client, title="<script>visible</script>", status="done")
        conn = db.connect(db_file)
        _stamp_latest(conn, issue["id"], "created", "2026-06-02 00:00:00")
        hostile_actor = "<script>actor()</script>"
        conn.execute("UPDATE users SET name = ? WHERE id = 1", (hostile_actor,))
        conn.commit()

        path = "/fleet/metrics?start=2026-06-01&end=2026-06-10"
        rest_response = client.get(path, headers=H_ADMIN)
        assert rest_response.status_code == 200
        rest = rest_response.json()
        assert rest_response.headers["cache-control"] == "private, no-store"
        assert {"authorization", "x-athena-actor"} <= _vary(rest_response)
        assert set(rest) == {
            "schema",
            "scope",
            "period",
            "filters",
            "flow",
            "cycle_time",
            "completion_by_actor_type",
            "actors",
            "coverage",
            "limits",
            "definitions",
        }
        assert "activity_scope_key" not in str(rest)
        assert "visibility_restricted" not in str(rest)
        assert "@metrics.test" not in str(rest)

        web = client.get("/aegis/fleet-metrics?start=2026-06-01&end=2026-06-10")
        assert web.status_code == 200
        assert web.headers["cache-control"] == "private, no-store"
        assert "cookie" in _vary(web)
        assert "<script>visible</script>" not in web.text
        assert hostile_actor not in web.text
        assert escape(hostile_actor) in web.text
        assert "Fleet throughput" in web.text
        assert "Completion events by actor" in web.text
        assert "Cycle samples excluded" in web.text
        assert "View JSON" not in web.text

        tokens_by_scope = {}
        for scope in ("read", "issue:write", "docs:write"):
            response = client.post(
                "/tokens",
                json={"name": f"metrics-{scope}", "scopes": [scope]},
                headers=H_ADMIN,
            )
            assert response.status_code == 201
            tokens_by_scope[scope] = response.json()["token"]

        for scope in ("read", "issue:write"):
            response = client.get(
                path,
                headers={"Authorization": f"Bearer {tokens_by_scope[scope]}"},
            )
            assert response.status_code == 200
            assert response.json() == rest
        docs_only = client.get(
            path,
            headers={"Authorization": f"Bearer {tokens_by_scope['docs:write']}"},
        )
        assert docs_only.status_code == 403
        assert docs_only.headers["cache-control"] == "private, no-store"
        scope_denial = conn.execute(
            "SELECT actor_id, detail FROM activity "
            "WHERE verb = 'scope_denied' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert dict(scope_denial) == {
            "actor_id": 1,
            "detail": "read or issue:write on GET /fleet/metrics",
        }

        anonymous_response = client.get(path)
        assert anonymous_response.status_code == 200
        anonymous = anonymous_response.json()
        assert anonymous["flow"] == rest["flow"]
        assert anonymous["cycle_time"]["visibility_complete"] is False
        assert rest["cycle_time"]["visibility_complete"] is True

        for actor_key in ("human", "outsider"):
            partial = client.get(path, headers=_headers(actors[actor_key]["id"]))
            assert partial.status_code == 200
            assert partial.json()["flow"] == rest["flow"]
            assert partial.json()["cycle_time"]["visibility_complete"] is False
        invalid_bearer = client.get(path, headers={"Authorization": "Bearer invalid"})
        assert invalid_bearer.status_code == 401
        assert invalid_bearer.headers["cache-control"] == "private, no-store"
        assert invalid_bearer.json()["detail"]

        client.headers.update({"Authorization": f"Bearer {tokens_by_scope['read']}"})
        through_client = AthenaClient(client=client).get_fleet_metrics(
            start="2026-06-01",
            end="2026-06-10",
        )
        assert through_client == rest

        pytest.importorskip("mcp")
        from athena.mcp.server import build_server

        server = build_server(AthenaClient(client=client))
        through_mcp = asyncio.run(
            server.call_tool(
                "get_fleet_metrics",
                {"start": "2026-06-01", "end": "2026-06-10"},
            )
        )
        assert len(through_mcp) == 1
        assert json.loads(through_mcp[0].text) == rest
        paused_token = client.post(
            "/tokens",
            json={"name": "paused-metrics", "scopes": ["read"]},
            headers={
                **_headers(actors["human"]["id"]),
                "Authorization": "",
            },
        )
        assert paused_token.status_code == 201
        users.set_paused(conn, actors["human"]["id"], True)
        paused_response = client.get(
            path,
            headers={"Authorization": f"Bearer {paused_token.json()['token']}"},
        )
        assert paused_response.status_code == 403
        assert paused_response.headers["cache-control"] == "private, no-store"

        conn.close()


def test_browser_session_matches_member_private_project_visibility(tmp_path):
    db_file = tmp_path / "browser-visibility.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        public = _project(client, name="Public metrics", key="PMT")
        private = _project(client, name="Private metrics", key="SMT")
        assert (
            client.put(
                f"/projects/{private['id']}/visibility",
                json={"visibility": "private"},
                headers=H_ADMIN,
            ).status_code
            == 200
        )

        public_issue = _issue(
            client,
            title="Public throughput",
            actor_id=actors["human"]["id"],
            project_id=public["id"],
        )
        _set_status(
            client,
            public_issue["id"],
            "done",
            actor_id=actors["human"]["id"],
        )
        private_issue = _issue(
            client,
            title="Secret throughput",
            project_id=private["id"],
            status="done",
        )

        conn = db.connect(db_file)
        _stamp_latest(conn, public_issue["id"], "created", "2026-06-02 00:00:00")
        _stamp_latest(
            conn,
            public_issue["id"],
            "changed_status",
            "2026-06-03 00:00:00",
        )
        _stamp_latest(conn, private_issue["id"], "created", "2026-06-02 00:00:00")
        users.set_password(conn, actors["outsider"]["id"], "pw")
        conn.close()

        expected = _metrics(
            client,
            start="2026-06-01",
            end="2026-06-10",
            headers=_headers(actors["outsider"]["id"]),
        )
        assert expected["flow"] == {
            "created": 1,
            "completed": 1,
            "net": 0,
        }
        assert [row["actor_name"] for row in expected["actors"]] == ["Human"]

        login = client.post(
            "/login",
            data={"email": "outsider@metrics.test", "password": "pw"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        web = client.get("/aegis/fleet-metrics?start=2026-06-01&end=2026-06-10")
        assert web.status_code == 200
        compact = " ".join(web.text.split())
        assert (
            '<span class="stat-number">1</span> '
            '<span class="stat-label">Issues created</span>'
        ) in compact
        assert (
            '<span class="stat-number">1</span> '
            '<span class="stat-label">Completion events</span>'
        ) in compact
        assert "Human" in web.text
        assert "Admin" not in web.text
        assert "Cycle timing is withheld" in web.text


def test_hidden_and_missing_project_filters_are_identical(tmp_path):
    db_file = tmp_path / "project-filter.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        outsider = _headers(actors["outsider"]["id"])
        private = _project(client, name="Hidden filter", key="HIF")
        assert (
            client.put(
                f"/projects/{private['id']}/visibility",
                json={"visibility": "private"},
                headers=H_ADMIN,
            ).status_code
            == 200
        )
        base = "/fleet/metrics?start=2026-01-01&end=2026-01-02&project_id="
        hidden = client.get(base + str(private["id"]), headers=outsider)
        missing = client.get(base + "999999", headers=outsider)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json() == {"detail": "no such project"}
        assert hidden.headers["cache-control"] == "private, no-store"


def test_actual_metrics_query_uses_bounded_partial_time_index(tmp_path):
    conn = db.connect(tmp_path / "plan.db")
    db.migrate(conn)
    actor = users.create_user(
        conn,
        email="planner@metrics.test",
        name="Planner",
        role="admin",
    )
    issue = issue_commands.create_issue(conn, actor=actor, title="History plan target")
    issue_commands.update_issue(conn, actor=actor, issue_id=issue["id"], status="done")
    conn.executemany(
        "INSERT INTO activity "
        "(actor_id, verb, target_kind, target_id, detail, created_at, "
        "visibility_restricted) VALUES (1, ?, 'issue', ?, '', ?, 0)",
        [("commented", offset + 1, "2025-01-01 00:00:00") for offset in range(5_000)]
        + [
            ("created", 10_000 + offset, "2026-01-02 00:00:00") for offset in range(200)
        ],
    )
    conn.commit()
    conn.execute("ANALYZE")

    query = fleet_metrics.parse_query(start="2026-01-01", end="2026-01-08")
    sql, params = fleet_metrics._evidence_statement(
        conn,
        query,
        actor=None,
        project_scope_key=None,
    )
    plan = " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )
    assert "idx_activity_issue_lifecycle_window" in plan
    assert "SCAN a" not in plan
    assert "USE TEMP B-TREE" not in plan

    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    history = fleet_metrics._load_visible_lifecycle(
        conn,
        {issue["id"]},
        end_sql="9999-12-31 00:00:00",
        actor=actor,
    )
    conn.set_trace_callback(None)
    assert len(history) == 2
    history_sql = next(
        statement
        for statement in traced
        if "INDEXED BY idx_issue_lifecycle_issue_event" in statement
    )
    history_plan = " ".join(
        row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + history_sql)
    )
    assert "idx_issue_lifecycle_issue_event" in history_plan
    assert "USE TEMP B-TREE" not in history_plan
    conn.close()


def test_project_filter_uses_event_time_destination_scope(tmp_path):
    db_file = tmp_path / "project-attribution.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        first = _project(client, name="First", key="FST")
        second = _project(client, name="Second", key="SND")
        issue = _issue(
            client,
            title="Moves once",
            project_id=first["id"],
        )
        conn = db.connect(db_file)
        _stamp_latest(conn, issue["id"], "created", "2026-07-01 00:00:00")
        moved = client.patch(
            f"/issues/{issue['id']}",
            json={"project_id": second["id"]},
            headers=H_ADMIN,
        )
        assert moved.status_code == 200, moved.text
        _set_status(client, issue["id"], "done", actor_id=1)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-07-02 00:00:00")

        first_metrics = _metrics(
            client,
            start="2026-07-01",
            end="2026-07-03",
            headers=H_ADMIN,
            extra=f"&project_id={first['id']}",
        )
        second_metrics = _metrics(
            client,
            start="2026-07-01",
            end="2026-07-03",
            headers=H_ADMIN,
            extra=f"&project_id={second['id']}",
        )
        assert first_metrics["flow"] == {
            "created": 1,
            "completed": 0,
            "net": 1,
        }
        assert second_metrics["flow"] == {
            "created": 0,
            "completed": 1,
            "net": -1,
        }
        assert second_metrics["cycle_time"]["sample_count"] == 1
        assert second_metrics["cycle_time"]["median_seconds"] == 86400.0
        conn.close()


def test_deleted_actor_keeps_snapshot_type_with_unknown_safe_label(tmp_path):
    db_file = tmp_path / "deleted-actor.db"
    with TestClient(create_app(db_file)) as client:
        actors = _bootstrap(client)
        human_id = actors["human"]["id"]
        issue = _issue(
            client,
            title="Actor later removed",
            actor_id=human_id,
            status="done",
        )
        conn = db.connect(db_file)
        _stamp_latest(conn, issue["id"], "created", "2026-07-05 00:00:00")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM users WHERE id = ?", (human_id,))
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        result = _metrics(
            client,
            start="2026-07-05",
            end="2026-07-06",
            headers=H_ADMIN,
        )
        assert result["completion_by_actor_type"] == {
            "human": 1,
            "agent": 0,
            "unknown": 0,
        }
        assert result["actors"] == [
            {
                "actor_id": human_id,
                "actor_name": "Unknown actor",
                "actor_type": "human",
                "completions": 1,
                "human_completions": 1,
                "agent_completions": 0,
                "unknown_completions": 0,
            }
        ]
        filtered = _metrics(
            client,
            start="2026-07-05",
            end="2026-07-06",
            headers=H_ADMIN,
            extra=f"&actor_id={human_id}",
        )
        assert filtered["flow"] == {
            "created": 1,
            "completed": 1,
            "net": 0,
        }
        conn.close()


def test_service_reads_are_side_effect_free_and_one_wal_snapshot(tmp_path, monkeypatch):
    db_file = tmp_path / "snapshot.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        issue = _issue(client, title="Snapshot target", status="done")
        writer = db.connect(db_file)
        _stamp_latest(writer, issue["id"], "created", "2026-07-08 00:00:00")

        reader = db.connect(db_file)
        actor = users.get_user(reader, 1)
        assert actor is not None
        query = fleet_metrics.parse_query(start="2026-07-08", end="2026-07-09")
        before_counts = tuple(
            reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("activity", "issue_lifecycle_facts", "issues")
        )
        first = fleet_metrics.build_fleet_metrics(reader, query, actor=actor)
        second = fleet_metrics.build_fleet_metrics(reader, query, actor=actor)
        assert second == first
        after_counts = tuple(
            reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("activity", "issue_lifecycle_facts", "issues")
        )
        assert after_counts == before_counts

        original = fleet_metrics._load_visible_lifecycle
        interleaved = False

        def delete_between_component_queries(*args, **kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                writer.execute("DELETE FROM issues WHERE id = ?", (issue["id"],))
                writer.commit()
            return original(*args, **kwargs)

        monkeypatch.setattr(
            fleet_metrics,
            "_load_visible_lifecycle",
            delete_between_component_queries,
        )
        coherent = fleet_metrics.build_fleet_metrics(reader, query, actor=actor)
        assert interleaved is True
        assert coherent["flow"] == {
            "created": 1,
            "completed": 1,
            "net": 0,
        }
        assert coherent["cycle_time"]["sample_count"] == 1
        assert (
            writer.execute(
                "SELECT 1 FROM issues WHERE id = ?", (issue["id"],)
            ).fetchone()
            is None
        )
        reader.close()
        writer.close()


def test_history_cap_rejects_instead_of_returning_partial_cycle_time(
    tmp_path, monkeypatch
):
    db_file = tmp_path / "history-cap.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        issue = _issue(client, title="Long enough history")
        conn = db.connect(db_file)
        _stamp_latest(conn, issue["id"], "created", "2026-07-10 00:00:00")
        _set_status(client, issue["id"], "done", actor_id=1)
        _stamp_latest(conn, issue["id"], "changed_status", "2026-07-11 00:00:00")
        monkeypatch.setattr(fleet_metrics, "HISTORY_EVENT_LIMIT", 1)
        response = client.get(
            "/fleet/metrics?start=2026-07-11&end=2026-07-12",
            headers=H_ADMIN,
        )
        assert response.status_code == 422
        assert "lifecycle history" in response.json()["detail"]
        conn.close()


def test_mcp_metrics_rejects_non_strict_numbers_before_dispatch():
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    from athena.mcp.server import build_server

    class RecordingClient:
        def __init__(self):
            self.calls = []

        def get_fleet_metrics(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True}

    recording = RecordingClient()
    server = build_server(recording)
    for arguments in (
        {"project_id": True},
        {"project_id": 1.5},
        {"actor_limit": 2.5},
    ):
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool("get_fleet_metrics", arguments))
    assert recording.calls == []

    valid = asyncio.run(
        server.call_tool(
            "get_fleet_metrics",
            {
                "start": "2026-01-01",
                "end": "2026-01-02",
                "project_id": 7,
                "actor_limit": 3,
            },
        )
    )
    assert json.loads(valid[0].text) == {"ok": True}
    assert recording.calls == [
        {
            "start": "2026-01-01",
            "end": "2026-01-02",
            "project_id": 7,
            "actor_id": None,
            "actor_limit": 3,
        }
    ]


def test_upgrade_from_0054_preserves_legacy_and_types_only_new_commands(
    tmp_path, monkeypatch
):
    source_migrations = db.MIGRATIONS_DIR
    staged_migrations = tmp_path / "migrations"
    staged_migrations.mkdir()
    for migration in sorted(source_migrations.glob("*.sql")):
        if migration.name >= "0055_issue_lifecycle_facts.sql":
            continue
        shutil.copy(migration, staged_migrations / migration.name)

    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged_migrations)
    conn = db.connect(tmp_path / "upgrade.db")
    applied = db.migrate(conn)
    assert applied[-1] == "0054_issue_leases.sql"
    admin = users.create_user(
        conn,
        email="upgrade@metrics.test",
        name="Upgrade admin",
        role="admin",
    )
    legacy_issue_id = conn.execute(
        "INSERT INTO issues (title, created_by) VALUES ('Legacy issue', ?)",
        (admin["id"],),
    ).lastrowid
    activity.record(
        conn,
        actor_id=admin["id"],
        verb="created",
        target_kind="issue",
        target_id=legacy_issue_id,
    )

    shutil.copy(
        source_migrations / "0055_issue_lifecycle_facts.sql",
        staged_migrations / "0055_issue_lifecycle_facts.sql",
    )
    assert db.migrate(conn) == ["0055_issue_lifecycle_facts.sql"]
    assert conn.execute("SELECT COUNT(*) FROM issue_lifecycle_facts").fetchone()[0] == 0

    native = issue_commands.create_issue(
        conn,
        actor=admin,
        title="Typed after upgrade",
    )
    assert native["id"] > legacy_issue_id
    fact = conn.execute(
        "SELECT issue_id, event_kind FROM issue_lifecycle_facts"
    ).fetchone()
    assert dict(fact) == {
        "issue_id": native["id"],
        "event_kind": "created",
    }
    conn.close()


def test_lifecycle_fact_constraints_and_predecessor_chain_are_append_only(
    tmp_path,
):
    db_file = tmp_path / "fact-constraints.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        issue = _issue(client, title="Constrained fact")
        _set_status(client, issue["id"], "in_progress", actor_id=1)
        _set_status(client, issue["id"], "done", actor_id=1)
        conn = db.connect(db_file)
        facts = conn.execute(
            "SELECT event_id, previous_event_id, event_kind "
            "FROM issue_lifecycle_facts WHERE issue_id = ? ORDER BY event_id",
            (issue["id"],),
        ).fetchall()
        assert [row["event_kind"] for row in facts] == [
            "created",
            "status_transition",
            "status_transition",
        ]
        assert facts[0]["previous_event_id"] is None
        assert facts[1]["previous_event_id"] == facts[0]["event_id"]
        assert facts[2]["previous_event_id"] == facts[1]["event_id"]

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE issue_lifecycle_facts SET actor_kind = 'agent' "
                "WHERE event_id = ?",
                (facts[0]["event_id"],),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM issue_lifecycle_facts WHERE event_id = ?",
                (facts[0]["event_id"],),
            )
        conn.rollback()

        duplicate_root_event = activity.record(
            conn,
            actor_id=1,
            verb="changed_status",
            target_kind="issue",
            target_id=issue["id"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            conn.execute(
                "INSERT INTO issue_lifecycle_facts "
                "(event_id, issue_id, event_kind, before_status, "
                "before_category, after_status, after_category, actor_kind) "
                "VALUES (?, ?, 'status_transition', 'done', 'done', "
                "'in_progress', 'doing', 'human')",
                (duplicate_root_event["id"], issue["id"]),
            )
        conn.rollback()

        duplicate_successor_event = activity.record(
            conn,
            actor_id=1,
            verb="changed_status",
            target_kind="issue",
            target_id=issue["id"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            conn.execute(
                "INSERT INTO issue_lifecycle_facts "
                "(event_id, issue_id, previous_event_id, event_kind, "
                "before_status, before_category, after_status, after_category, "
                "actor_kind) VALUES (?, ?, ?, 'status_transition', "
                "'open', 'todo', 'in_progress', 'doing', 'human')",
                (
                    duplicate_successor_event["id"],
                    issue["id"],
                    facts[0]["event_id"],
                ),
            )
        conn.rollback()

        imported = conn.execute(
            "INSERT INTO activity "
            "(actor_id, verb, target_kind, target_id, detail, imported_at, "
            "visibility_restricted) VALUES (1, 'created', 'issue', ?, '', "
            "datetime('now'), 0)",
            (issue["id"],),
        ).lastrowid
        with pytest.raises(
            sqlite3.IntegrityError,
            match="matching native issue activity event required",
        ):
            conn.execute(
                "INSERT INTO issue_lifecycle_facts "
                "(event_id, issue_id, event_kind, after_status, "
                "after_category, actor_kind) "
                "VALUES (?, ?, 'created', 'open', 'todo', 'human')",
                (imported, issue["id"]),
            )
        conn.rollback()
        conn.close()


def test_native_completion_after_untracked_source_import_has_no_guessed_cycle(
    tmp_path,
):
    db_file = tmp_path / "source-import.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        conn = db.connect(db_file)
        imported_issue = issues.create_issue(
            conn,
            title="Imported without native creation history",
            body="",
            created_by=1,
        )
        _set_status(client, imported_issue["id"], "done", actor_id=1)
        _stamp_latest(
            conn,
            imported_issue["id"],
            "changed_status",
            "2026-07-15 00:00:00",
        )

        result = _metrics(
            client,
            start="2026-07-15",
            end="2026-07-16",
            headers=H_ADMIN,
        )
        assert result["flow"] == {
            "created": 0,
            "completed": 1,
            "net": -1,
        }
        assert result["cycle_time"] == {
            "median_seconds": None,
            "visibility_complete": True,
            "sample_count": 0,
            "excluded_completions": 1,
        }
        assert result["coverage"]["complete"] is False
        conn.close()
