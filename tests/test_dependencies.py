"""Typed issue dependencies: blocks / blocked-by / relates-to.

These pin the CONTRACT of the feature, not just that a route returns 200:

  * the three user-facing relations collapse onto two stored kinds, and
    "blocked_by" is never its own row — it is a "blocks" row read from the other
    end (so adding "A blocked_by B" must show up as "B blocks A");
  * "relates" is symmetric and stored ONCE regardless of which side declared it;
  * adding the same edge twice is a silent no-op (a double-submit must not error
    or duplicate);
  * the integrity guards hold — no self-link, no direct A<->B block contradiction;
  * open_blockers is what drives the close-warning: it counts only blockers that
    are not yet done, and empties the moment the blocker closes;
  * deleting an issue CASCADEs its links away (no dangling rows);
  * the REST surface enforces the same creator-or-assignee write gate as every
    other issue mutation, and resolves a human ref (ATH-12) as the target;
  * the web close path WARNS instead of closing when a blocker is still open, and
    honours the confirm=1 override.
"""

from fastapi.testclient import TestClient

from athena.aegis import dependencies, issues
from athena.core import db
from athena.main import create_app

_H = {"X-Athena-Actor": "1"}


def _seed_user(db_file, email="k@e.com", name="K"):
    conn = db.connect(db_file)
    conn.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
    conn.commit()
    conn.close()


def _make_issue(client, title, body=""):
    return client.post(
        "/issues", json={"title": title, "body": body}, headers=_H
    ).json()


# --- data layer: normalization & integrity --------------------------------


def test_blocked_by_is_the_inverse_blocks_row(tmp_path):
    # WHY: blocked_by must NOT be a distinct stored kind. Declaring "A blocked_by
    # B" has to be readable as "B blocks A" from B's side — same single edge.
    db_file = tmp_path / "inv.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")["id"]
        b = _make_issue(client, "B")["id"]
        conn = db.connect(db_file)
        assert dependencies.add_link(
            conn, from_id=a, to_id=b, relation="blocked_by", created_by=1
        ) == (None, True)

        a_links = dependencies.list_links(conn, a)
        b_links = dependencies.list_links(conn, b)
        # A says it is blocked_by B...
        assert [x["id"] for x in a_links["blocked_by"]] == [b]
        assert a_links["blocks"] == []
        # ...and the very same edge reads as "B blocks A" from B's side.
        assert [x["id"] for x in b_links["blocks"]] == [a]
        assert b_links["blocked_by"] == []
        # Exactly one underlying row exists.
        rows = conn.execute("SELECT from_id, to_id, kind FROM issue_links").fetchall()
        assert [tuple(r) for r in rows] == [(b, a, "blocks")]
        conn.close()


def test_relates_is_symmetric_and_stored_once(tmp_path):
    # WHY: a symmetric relation declared from either side is one row, and shows up
    # on BOTH issues. Order of declaration must not matter.
    db_file = tmp_path / "rel.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")["id"]
        b = _make_issue(client, "B")["id"]
        conn = db.connect(db_file)
        # Declare from the HIGHER id to prove normalization (smaller id first).
        assert dependencies.add_link(
            conn, from_id=b, to_id=a, relation="relates", created_by=1
        ) == (None, True)
        assert [x["id"] for x in dependencies.list_links(conn, a)["relates"]] == [b]
        assert [x["id"] for x in dependencies.list_links(conn, b)["relates"]] == [a]
        count = conn.execute(
            "SELECT COUNT(*) FROM issue_links WHERE kind = 'relates'"
        ).fetchone()[0]
        assert count == 1
        conn.close()


def test_adding_same_edge_twice_is_a_noop(tmp_path):
    # WHY: a double-submit (or a relates declared from both sides) must not error
    # and must not create a second row.
    db_file = tmp_path / "idem.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")["id"]
        b = _make_issue(client, "B")["id"]
        conn = db.connect(db_file)
        for relation, frm, to in [
            ("blocks", a, b),
            ("relates", a, b),
            ("relates", b, a),
        ]:
            # reason is None for every add; only the first of each identical edge
            # reports inserted=True (the relates re-add from the other side is a no-op).
            assert (
                dependencies.add_link(
                    conn, from_id=frm, to_id=to, relation=relation, created_by=1
                )[0]
                is None
            )
        assert conn.execute("SELECT COUNT(*) FROM issue_links").fetchone()[0] == 2
        conn.close()


def test_self_link_rejected(tmp_path):
    db_file = tmp_path / "self.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")["id"]
        conn = db.connect(db_file)
        reason, _inserted = dependencies.add_link(
            conn, from_id=a, to_id=a, relation="blocks", created_by=1
        )
        assert reason is not None
        assert conn.execute("SELECT COUNT(*) FROM issue_links").fetchone()[0] == 0
        conn.close()


def test_direct_block_contradiction_rejected(tmp_path):
    # WHY: A blocks B and B blocks A is a nonsensical 2-cycle; the second add must
    # be refused so the data never describes a mutual deadlock.
    db_file = tmp_path / "cyc.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")["id"]
        b = _make_issue(client, "B")["id"]
        conn = db.connect(db_file)
        assert dependencies.add_link(
            conn, from_id=a, to_id=b, relation="blocks", created_by=1
        ) == (None, True)
        reason, _inserted = dependencies.add_link(
            conn, from_id=b, to_id=a, relation="blocks", created_by=1
        )
        assert reason is not None and "block each other" in reason
        conn.close()


def test_unknown_relation_rejected(tmp_path):
    db_file = tmp_path / "unk.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")["id"]
        b = _make_issue(client, "B")["id"]
        conn = db.connect(db_file)
        assert (
            dependencies.add_link(
                conn, from_id=a, to_id=b, relation="nonsense", created_by=1
            )[0]
            is not None
        )
        conn.close()


# --- open_blockers: the engine behind the close warning --------------------


def test_open_blockers_counts_only_undone_and_empties_on_close(tmp_path):
    # WHY: the close-warning fires off open_blockers. A blocker that is already
    # done must not warn; closing the blocker must clear the warning.
    db_file = tmp_path / "blk.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        target = _make_issue(client, "target")["id"]
        b1 = _make_issue(client, "b1")["id"]
        b2 = _make_issue(client, "b2")["id"]
        conn = db.connect(db_file)
        # b1 and b2 both block target.
        for b in (b1, b2):
            dependencies.add_link(
                conn, from_id=b, to_id=target, relation="blocks", created_by=1
            )
        assert {x["id"] for x in dependencies.open_blockers(conn, target)} == {b1, b2}
        # Close b1 -> only b2 still blocks.
        issues.update_status(conn, b1, "done")
        assert [x["id"] for x in dependencies.open_blockers(conn, target)] == [b2]
        # Close b2 -> nothing blocks; the warning would no longer fire.
        issues.update_status(conn, b2, "done")
        assert dependencies.open_blockers(conn, target) == []
        conn.close()


def test_deleting_issue_cascades_its_links(tmp_path):
    # WHY: links carry real FKs with ON DELETE CASCADE; deleting an endpoint must
    # leave no dangling relationship row.
    db_file = tmp_path / "casc.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")["id"]
        b = _make_issue(client, "B")["id"]
        conn = db.connect(db_file)
        dependencies.add_link(conn, from_id=a, to_id=b, relation="blocks", created_by=1)
        conn.execute("DELETE FROM issues WHERE id = ?", (a,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM issue_links").fetchone()[0] == 0
        conn.close()


# --- REST API: gate, ref resolution, status codes --------------------------


def test_api_add_list_remove_roundtrip(tmp_path):
    db_file = tmp_path / "api.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")
        b = _make_issue(client, "B")
        # Add by human ref (key), 201.
        r = client.post(
            f"/issues/{a['id']}/links",
            json={"target_ref": b["key"] or str(b["id"]), "relation": "blocks"},
            headers=_H,
        )
        assert r.status_code == 201
        assert [x["id"] for x in r.json()["blocks"]] == [b["id"]]
        # Read it back, open (no auth needed for GET).
        got = client.get(f"/issues/{a['id']}/links")
        assert [x["id"] for x in got.json()["blocks"]] == [b["id"]]
        # Remove it, 200 + empty.
        rem = client.delete(f"/issues/{a['id']}/links/blocks/{b['id']}", headers=_H)
        assert rem.status_code == 200
        assert rem.json()["blocks"] == []
        # Removing again is a 404 (nothing left).
        assert (
            client.delete(
                f"/issues/{a['id']}/links/blocks/{b['id']}", headers=_H
            ).status_code
            == 404
        )


def test_api_unknown_target_ref_is_422(tmp_path):
    db_file = tmp_path / "ref.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")
        r = client.post(
            f"/issues/{a['id']}/links",
            json={"target_ref": "ATH-9999", "relation": "blocks"},
            headers=_H,
        )
        assert r.status_code == 422


def test_api_contradiction_is_409(tmp_path):
    db_file = tmp_path / "c409.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        a = _make_issue(client, "A")
        b = _make_issue(client, "B")
        client.post(
            f"/issues/{a['id']}/links",
            json={"target_ref": str(b["id"]), "relation": "blocks"},
            headers=_H,
        )
        r = client.post(
            f"/issues/{b['id']}/links",
            json={"target_ref": str(a["id"]), "relation": "blocks"},
            headers=_H,
        )
        assert r.status_code == 409


def test_api_add_link_enforces_write_gate(tmp_path):
    # WHY: declaring a link is a write on the issue, so a non-creator/non-assignee
    # must be refused (403) exactly like status/assign.
    db_file = tmp_path / "gate.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)  # user 1 (creator)
        _seed_user(db_file, email="o@e.com", name="O")  # user 2 (outsider)
        a = _make_issue(client, "A")  # created by user 1
        b = _make_issue(client, "B")
        r = client.post(
            f"/issues/{a['id']}/links",
            json={"target_ref": str(b["id"]), "relation": "blocks"},
            headers={"X-Athena-Actor": "2"},
        )
        assert r.status_code == 403


def test_api_list_missing_issue_is_404(tmp_path):
    db_file = tmp_path / "miss.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _seed_user(db_file)
        assert client.get("/issues/9999/links").status_code == 404


# --- Web: warn-on-close with confirm override ------------------------------


def _login(client, email, name):
    client.post(
        "/users",
        json={"email": email, "name": name, "password": "secret"},
        headers={"X-Athena-Actor": "1"},
    )
    client.post("/login", data={"email": email, "password": "secret"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def test_web_close_warns_then_confirm_overrides(tmp_path):
    # WHY: closing an issue with an open blocker must NOT silently apply — it
    # re-renders with a warning. A second submit carrying confirm=1 goes through.
    db_file = tmp_path / "warn.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")  # user 1
        target = _make_issue(client, "target")
        blocker = _make_issue(client, "blocker")
        client.post(
            f"/issues/{target['id']}/links",
            json={"target_ref": str(blocker["id"]), "relation": "blocked_by"},
            headers=_H,
        )

        # First close attempt: warned, NOT applied (200, status still not done).
        warned = client.post(
            f"/aegis/issues/{target['id']}/status",
            data={"status": "done"},
            follow_redirects=False,
        )
        assert warned.status_code == 200
        assert "still blocked" in warned.text.lower()
        assert client.get(f"/issues/{target['id']}").json()["status"] != "done"

        # Confirmed close: applied (303), status now done.
        ok = client.post(
            f"/aegis/issues/{target['id']}/status",
            data={"status": "done", "confirm": "1"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert client.get(f"/issues/{target['id']}").json()["status"] == "done"


def test_web_close_without_blockers_applies_directly(tmp_path):
    # WHY: the warning must not fire when there is nothing blocking — a normal
    # close still redirects on the first submit.
    db_file = tmp_path / "noblk.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        issue = _make_issue(client, "free")
        ok = client.post(
            f"/aegis/issues/{issue['id']}/status",
            data={"status": "done"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert client.get(f"/issues/{issue['id']}").json()["status"] == "done"


def test_web_add_and_remove_link_roundtrip(tmp_path):
    db_file = tmp_path / "webrt.db"
    app = create_app(db_file)
    with TestClient(app) as client:
        _login(client, "ann@e.com", "Ann")
        a = _make_issue(client, "A")
        b = _make_issue(client, "B")
        add = client.post(
            f"/aegis/issues/{a['id']}/links",
            data={"relation": "blocks", "target_ref": str(b["id"])},
            follow_redirects=False,
        )
        assert add.status_code == 303
        assert [
            x["id"] for x in client.get(f"/issues/{a['id']}/links").json()["blocks"]
        ] == [b["id"]]
        rem = client.post(
            f"/aegis/issues/{a['id']}/links/blocks/{b['id']}/delete",
            follow_redirects=False,
        )
        assert rem.status_code == 303
        assert client.get(f"/issues/{a['id']}/links").json()["blocks"] == []
