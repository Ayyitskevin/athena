"""Tests for the activity hash chain: the trail that can prove itself (0072).

These encode the contract, not the plumbing. Every write is chained in the same
transaction; the chain's linkage and the chained rows are DB-frozen; a forged
row, a missing link, or a side branch is *found* by verification with the exact
event id and reason; and the whole surface is honest about its edges — rows
below the anchor are "before the chain", a clean tail is not a clean trail, and
a break is a finding, never something Athena repairs.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from athena.core import activity, activity_chain, db
from athena.main import create_app
from athena.mcp.client import AthenaClient
from athena import ops


def _migrated_conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    return conn


def _seed_user(conn, email="k@e.com", name="Kevin", role="member"):
    cur = conn.execute(
        "INSERT INTO users (email, name, role) VALUES (?, ?, ?)",
        (email, name, role),
    )
    conn.commit()
    return cur.lastrowid


def _record_n(conn, n, *, actor_id=1, verb="created"):
    rows = []
    for i in range(n):
        rows.append(
            activity.record(
                conn, actor_id=actor_id, verb=verb, target_kind="issue", target_id=i + 1
            )
        )
    return rows


# --- Data layer: append + status ------------------------------------------


def test_every_recorded_row_is_chained_in_the_same_transaction(tmp_path):
    # WHY: the chain's whole claim is coverage — an event that exists unchained
    # is a hole verification would have to explain. record() appends the entry
    # itself, so no caller can forget it.
    conn = _migrated_conn(tmp_path / "chain.db")
    _seed_user(conn)
    _record_n(conn, 3)
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM activity) AS a, "
        "(SELECT COUNT(*) FROM activity_chain) AS c"
    ).fetchone()
    assert counts["a"] == counts["c"] == 3
    st = activity_chain.status(conn)
    assert st["anchor_event_id"] == 1
    assert st["chained_count"] == 3
    assert st["unchained_count"] == 0
    assert st["head"]["event_id"] == 3
    conn.close()


def test_chain_links_from_genesis_and_recipe_recomputes(tmp_path):
    # WHY: the recipe is the contract a THIRD PARTY could reimplement from
    # docs/TRAIL_INTEGRITY.md alone. The first entry links from the documented
    # genesis; every entry_hash re-derives from the stored row + prev_hash.
    conn = _migrated_conn(tmp_path / "recipe.db")
    _seed_user(conn)
    _record_n(conn, 2)
    entries = conn.execute(
        "SELECT event_id, prev_hash, entry_hash FROM activity_chain ORDER BY event_id"
    ).fetchall()
    assert entries[0]["prev_hash"] == activity_chain.GENESIS
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
    for entry in entries:
        row = conn.execute(
            "SELECT id, actor_id, verb, target_kind, target_id, detail, "
            "created_at, run_id, parent_run_id, forked_from_event_id, "
            "visibility_restricted, reverses_event_id, imported_at "
            "FROM activity WHERE id = ?",
            (entry["event_id"],),
        ).fetchone()
        assert activity_chain.entry_hash(row, entry["prev_hash"]) == entry["entry_hash"]
    conn.close()


def test_rollback_leaves_no_dangling_chain_entry(tmp_path):
    # WHY: the entry lands in the caller's transaction — an aborted write must
    # take its attestation down with it, or the chain would attest a ghost.
    conn = _migrated_conn(tmp_path / "rollback.db")
    _seed_user(conn)
    _record_n(conn, 1)
    conn.execute("BEGIN IMMEDIATE")
    activity.record(
        conn,
        actor_id=1,
        verb="created",
        target_kind="issue",
        target_id=99,
        commit=False,
    )
    conn.rollback()
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM activity) AS a, "
        "(SELECT COUNT(*) FROM activity_chain) AS c"
    ).fetchone()
    assert counts["a"] == counts["c"] == 1
    assert activity_chain.verify_all(conn)["ok"] is True
    conn.close()


# --- DB-enforced immutability ----------------------------------------------


def test_chained_activity_rows_cannot_be_deleted_and_edits_are_detected(tmp_path):
    # WHY: the contract is deletion-refused, edit-DETECTED. A chained row cannot
    # vanish (the chain entry's foreign key holds it, and the entry itself is
    # undeletable), while an in-place edit is deliberately not trigger-blocked —
    # prevention would be theater against a writer holding the file — but it can
    # never pass verification again.
    conn = _migrated_conn(tmp_path / "frozen.db")
    _seed_user(conn)
    _record_n(conn, 2)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM activity WHERE id = 1")
    conn.execute("UPDATE activity SET detail = 'forged' WHERE id = 1")
    conn.commit()
    report = activity_chain.verify_all(conn)
    assert report["ok"] is False
    assert report["mismatch"]["event_id"] == 1
    assert report["mismatch"]["reason"] == "entry_hash_mismatch"
    conn.close()


def test_chain_entries_are_immutable_and_only_extend_the_head(tmp_path):
    # WHY: if raw SQL could edit an entry, or append one that names any
    # prev_hash it likes, "the chain verifies" would stop meaning anything.
    conn = _migrated_conn(tmp_path / "entries.db")
    _seed_user(conn)
    _record_n(conn, 2)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE activity_chain SET entry_hash = ? WHERE event_id = 1",
            ("a" * 64,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="durable"):
        conn.execute("DELETE FROM activity_chain WHERE event_id = 2")
    # A side branch: right id order, wrong prev_hash.
    with pytest.raises(sqlite3.IntegrityError, match="extend the head"):
        conn.execute(
            "INSERT INTO activity_chain (event_id, prev_hash, entry_hash) "
            "VALUES (99, ?, ?)",
            ("b" * 64, "c" * 64),
        )
    # Backfilling behind the head is refused even with the head's hash.
    head = activity_chain.head(conn)
    with pytest.raises(sqlite3.IntegrityError, match="extend the head"):
        conn.execute(
            "INSERT INTO activity_chain (event_id, prev_hash, entry_hash) "
            "VALUES (1, ?, ?)",
            (head["entry_hash"], "d" * 64),
        )
    conn.close()


# --- Verification finds every schema-expressible break ----------------------


def _drop_chain_locks(conn):
    """Simulate an attacker editing the FILE, where triggers do not protect."""
    conn.execute("DROP TRIGGER activity_chain_no_update")
    conn.execute("DROP TRIGGER activity_chain_no_delete")


def test_verify_pinpoints_a_forged_row(tmp_path):
    # WHY: the product claim is not "tampering is impossible" (it is not — see
    # TRAIL_INTEGRITY.md) but "tampering is EVIDENT, at an exact spot".
    conn = _migrated_conn(tmp_path / "forged.db")
    _seed_user(conn)
    _record_n(conn, 5)
    conn.execute("UPDATE activity SET detail = 'forged' WHERE id = 3")
    conn.commit()
    report = activity_chain.verify_all(conn)
    assert report["ok"] is False
    assert report["mismatch"]["event_id"] == 3
    assert report["mismatch"]["reason"] == "entry_hash_mismatch"
    # The walk verified exactly the rows before the break, then stopped: one
    # exact finding, not an echo per subsequent row.
    assert report["checked"] == 2
    conn.close()


def test_verify_pinpoints_a_vanished_row(tmp_path):
    # WHY: mid-stream deletion (of BOTH the row and its entry) must surface as
    # the next entry failing to link, not as a silently shorter trail.
    conn = _migrated_conn(tmp_path / "vanished.db")
    _seed_user(conn)
    _record_n(conn, 5)
    _drop_chain_locks(conn)
    conn.execute("DELETE FROM activity_chain WHERE event_id = 3")
    conn.execute("DELETE FROM activity WHERE id = 3")
    conn.commit()
    report = activity_chain.verify_all(conn)
    assert report["ok"] is False
    assert report["mismatch"]["event_id"] == 4
    assert report["mismatch"]["reason"] == "prev_hash_mismatch"
    conn.close()


def test_verify_flags_an_unchained_row_above_the_anchor(tmp_path):
    # WHY: an out-of-band INSERT into activity (bypassing record()) is exactly
    # the "forged native-looking row" the single-writer doctrine exists to
    # prevent — coverage checking makes it visible.
    conn = _migrated_conn(tmp_path / "unchained.db")
    _seed_user(conn)
    _record_n(conn, 2)
    conn.execute(
        "INSERT INTO activity (actor_id, verb, target_kind, target_id, detail) "
        "VALUES (1, 'forged', 'issue', 9, '')"
    )
    conn.commit()
    report = activity_chain.verify_all(conn)
    assert report["ok"] is False
    assert report["mismatch"]["reason"] == "missing_chain_row"
    assert report["mismatch"]["event_id"] == 3
    conn.close()


def test_windowed_verify_resumes_and_bad_cursor_refuses(tmp_path):
    # WHY: verification is bounded work per call, resumable by cursor — and a
    # cursor that is NOT a verified entry must refuse rather than silently
    # re-anchor the walk past unverified history.
    conn = _migrated_conn(tmp_path / "windows.db")
    _seed_user(conn)
    _record_n(conn, 10)
    first = activity_chain.verify(conn, limit=4)
    assert (first["ok"], first["checked"], first["has_more"]) == (True, 4, True)
    assert first["next_after"] == 4
    second = activity_chain.verify(conn, after_id=first["next_after"], limit=4)
    assert (second["ok"], second["checked"], second["has_more"]) == (True, 4, True)
    third = activity_chain.verify(conn, after_id=second["next_after"], limit=4)
    assert (third["ok"], third["checked"], third["has_more"]) == (True, 2, False)
    assert third["next_after"] is None
    bad = activity_chain.verify(conn, after_id=999, limit=4)
    assert bad["ok"] is False
    assert bad["mismatch"]["reason"] == "bad_cursor"
    conn.close()


def test_verify_tail_checks_only_the_newest_entries(tmp_path):
    # WHY: the security page re-verifies a tail per render and must SAY it is
    # only a tail. The helper's window is exactly the newest `count` entries.
    conn = _migrated_conn(tmp_path / "tail.db")
    _seed_user(conn)
    _record_n(conn, 10)
    report = activity_chain.verify_tail(conn, count=3)
    assert report["ok"] is True
    assert report["checked"] == 3
    assert report["through_event_id"] == 10
    # Shorter chains than the window fall back to the full (still bounded) walk.
    assert activity_chain.verify_tail(conn, count=50)["checked"] == 10
    conn.close()


def test_empty_chain_verifies_vacuously(tmp_path):
    # WHY: a fresh database has no claim yet — "ok" with zero checked, never an
    # error a bootstrapping doctor run would trip over.
    conn = _migrated_conn(tmp_path / "empty.db")
    report = activity_chain.verify_all(conn)
    assert (report["ok"], report["checked"]) == (True, 0)
    assert activity_chain.status(conn)["head"] is None
    conn.close()


# --- REST surface ------------------------------------------------------------


def test_chain_endpoints_are_admin_only(tmp_path):
    # WHY: chain state is operator intelligence about the audit trail itself —
    # the same bar as /security. Members and anonymous callers get refusals,
    # not integrity metadata.
    app = create_app(tmp_path / "rest-authz.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "Ann"})
        member = client.post(
            "/users",
            json={"email": "m@example.com", "name": "Member"},
            headers={"X-Athena-Actor": "1"},
        ).json()
        member_headers = {"X-Athena-Actor": str(member["id"])}
        for path in ("/activity/chain", "/activity/chain/verify"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=member_headers).status_code == 403


def test_chain_status_and_verify_over_rest(tmp_path):
    # WHY: the REST twin of the data layer — an operator's curl and the MCP
    # tools read the same projection, cursor and all.
    app = create_app(tmp_path / "rest.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "Ann"})
        admin = {"X-Athena-Actor": "1"}
        status = client.get("/activity/chain", headers=admin)
        assert status.status_code == 200
        body = status.json()
        assert body["schema"] == activity_chain.SCHEMA
        assert body["chained_count"] >= 1  # bootstrap itself is on the trail
        assert body["head"]["entry_hash"]
        first = client.get(
            "/activity/chain/verify", params={"limit": 1}, headers=admin
        ).json()
        assert first["ok"] is True and first["checked"] == 1
        if first["has_more"]:
            resumed = client.get(
                "/activity/chain/verify",
                params={"after_id": first["next_after"]},
                headers=admin,
            ).json()
            assert resumed["ok"] is True
        assert (
            client.get(
                "/activity/chain/verify", params={"limit": 0}, headers=admin
            ).status_code
            == 422
        )


def test_mcp_client_reads_chain_over_rest(tmp_path):
    # WHY: steering rule 1 — no capability without an MCP path. The client
    # methods hit the same REST surface, so parity is structural.
    app = create_app(tmp_path / "mcp.db")
    with TestClient(app) as tc:
        tc.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})
        raw = tc.post(
            "/tokens",
            json={"name": "mcp", "scopes": ["admin"]},
            headers={"X-Athena-Actor": "1"},
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        api = AthenaClient(client=tc)
        status = api.activity_chain_status()
        assert status["schema"] == activity_chain.SCHEMA
        report = api.verify_activity_chain(limit=5)
        assert report["ok"] is True


# --- Web: the security page card ---------------------------------------------


def test_security_page_shows_trail_integrity_card(tmp_path):
    # WHY: the operator's one page for trail intelligence carries the chain's
    # state, and the tail check says it is only a tail — never the full claim.
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        client.post(
            "/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"}
        )
        client.post(
            "/login",
            data={"email": "a@e.com", "password": "pw"},
            follow_redirects=False,
        )
        page = client.get("/admin/security")
        assert page.status_code == 200
        assert "Trail integrity" in page.text
        assert "Chain head" in page.text
        assert "entries re-verified on this render" in page.text


# --- Doctor -------------------------------------------------------------------


def test_doctor_walks_the_chain_and_fails_on_a_break(tmp_path, capsys):
    db_file = tmp_path / "doctor.db"
    conn = _migrated_conn(db_file)
    _seed_user(conn)
    _record_n(conn, 4)
    ok = ops._check_activity_chain(db_file)
    assert "activity chain: ok (4 entries verified" in ok
    assert "anchor event 1" in ok
    # The check is WIRED into the doctor CLI, not just available beside it.
    assert ops.doctor_main([str(db_file)]) == 0
    assert "activity chain: ok" in capsys.readouterr().out
    conn.execute("UPDATE activity SET detail = 'forged' WHERE id = 2")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="event 2: entry_hash_mismatch"):
        ops._check_activity_chain(db_file)
    assert ops.doctor_main([str(db_file)]) == 1
    assert "entry_hash_mismatch" in capsys.readouterr().err


def test_doctor_reports_pre_chain_databases_plainly(tmp_path):
    # WHY: a database that predates 0072 has nothing to verify; doctor says so
    # instead of failing a check the schema cannot satisfy yet.
    db_file = tmp_path / "old.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    msg = ops._check_activity_chain(db_file)
    assert "predates migration 0072" in msg
