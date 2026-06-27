"""The activity actor-type lens — filtering the audit trail to agents or humans.

The is_agent flag exists so an operator can ask "what did the agents do?"
distinctly from human activity. These tests pin that lens across the three
surfaces that share one data-layer query: the data layer itself, the JSON
/activity feed, and the browser feed + its CSV export. The lens is permissive on
bad input in the browser (unknown → no filter) but strict in the API (422), and
it is carried through paging and export so a filtered view stays filtered.
"""
import csv
from io import StringIO

from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_actors(conn):
    """A human (id 1) and an agent (id 2)."""
    conn.execute("INSERT INTO users (email, name, is_agent) VALUES ('h@e.com','Human',0)")
    conn.execute("INSERT INTO users (email, name, is_agent) VALUES ('b@e.com','Bot',1)")
    conn.commit()


def _two_actors_acting(client):
    """Bootstrap a human (id 1) and an agent (id 2), then have each create an
    issue so the trail has one human event (target #1) and one agent event (#2)."""
    client.post("/users", json={"email": "h@e.com", "name": "Human"})
    client.post(
        "/users", json={"email": "b@e.com", "name": "Bot", "is_agent": True}, headers=H1
    )
    client.post("/issues", json={"title": "by-human"}, headers={"X-Athena-Actor": "1"})
    client.post("/issues", json={"title": "by-agent"}, headers={"X-Athena-Actor": "2"})


# --- data layer -------------------------------------------------------------


def test_list_activity_actor_type_lens(tmp_path):
    conn = _migrated_conn(tmp_path / "lens.db")
    _seed_actors(conn)
    activity.record(conn, actor_id=1, verb="created", target_kind="issue", target_id=1)
    activity.record(conn, actor_id=2, verb="created", target_kind="issue", target_id=2)

    agents = activity.list_activity(conn, actor_is_agent=True)
    assert [r["actor_name"] for r in agents] == ["Bot"]

    humans = activity.list_activity(conn, actor_is_agent=False)
    assert [r["actor_name"] for r in humans] == ["Human"]

    # None means no lens — the whole trail, unchanged behaviour.
    assert len(activity.list_activity(conn)) == 2
    conn.close()


def test_actor_type_lens_composes_with_other_filters(tmp_path):
    # WHY: the lens is independent of the others — "agents, on issue #2" must still
    # narrow correctly, not override or get overridden.
    conn = _migrated_conn(tmp_path / "compose.db")
    _seed_actors(conn)
    activity.record(conn, actor_id=2, verb="created", target_kind="issue", target_id=2)
    activity.record(conn, actor_id=2, verb="commented", target_kind="issue", target_id=9)
    rows = activity.list_activity(conn, actor_is_agent=True, target_kind="issue", target_id=2)
    assert [r["verb"] for r in rows] == ["created"]
    conn.close()


# --- JSON API ---------------------------------------------------------------


def test_activity_api_actor_type_filter(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        _two_actors_acting(client)

        agent_feed = client.get("/activity?actor_type=agent", headers=H1).json()
        assert {e["actor_name"] for e in agent_feed} == {"Bot"}

        human_feed = client.get("/activity?actor_type=human", headers=H1).json()
        assert {e["actor_name"] for e in human_feed} == {"Human"}

        # No lens → both actors.
        assert {e["actor_name"] for e in client.get("/activity", headers=H1).json()} == {
            "Human",
            "Bot",
        }


def test_activity_api_rejects_unknown_actor_type(tmp_path):
    # WHY: machine clients get a strict contract — an out-of-range value is a 422,
    # not a silently-dropped filter that returns the wrong rows.
    with TestClient(create_app(tmp_path / "bad.db")) as client:
        client.post("/users", json={"email": "h@e.com", "name": "Human"})
        bad = client.get("/activity?actor_type=robot", headers=H1)
        assert bad.status_code == 422


# --- browser feed + CSV -----------------------------------------------------


def test_activity_web_feed_actor_type_filter(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        _two_actors_acting(client)

        # Agents lens: only the agent's event (on issue #2) renders; the human's
        # event (issue #1) is filtered out. The select reflects the active lens.
        page = client.get("/aegis/activity?actor_type=agent")
        assert page.status_code == 200
        assert "/aegis/issues/2" in page.text
        assert "/aegis/issues/1" not in page.text
        assert 'value="agent" selected' in page.text

        # Humans lens is the mirror image.
        human = client.get("/aegis/activity?actor_type=human")
        assert "/aegis/issues/1" in human.text
        assert "/aegis/issues/2" not in human.text


def test_activity_web_unknown_actor_type_is_ignored(tmp_path):
    # WHY: a hand-edited URL with a bad value shouldn't error the page — the browser
    # feed stays permissive (no lens), unlike the strict API.
    with TestClient(create_app(tmp_path / "weblax.db")) as client:
        _two_actors_acting(client)
        page = client.get("/aegis/activity?actor_type=robot")
        assert page.status_code == 200
        assert "/aegis/issues/1" in page.text and "/aegis/issues/2" in page.text


def test_activity_csv_export_respects_actor_type(tmp_path):
    with TestClient(create_app(tmp_path / "csv.db")) as client:
        _two_actors_acting(client)

        dump = client.get("/aegis/activity.csv?actor_type=agent")
        assert dump.status_code == 200
        rows = list(csv.DictReader(StringIO(dump.text)))
        assert {r["actor_name"] for r in rows} == {"Bot"}

        # The export link the page hands out carries the lens.
        page = client.get("/aegis/activity?actor_type=agent")
        assert "actor_type=agent" in page.text
