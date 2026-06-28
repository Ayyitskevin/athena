"""The visibility resolver — who may read a private project or space.

Athena is public by default; this is the core that makes "private" mean something.
These pin the rule (public → everyone incl. signed-out; private → admins, the
creator, and explicit members only), the membership join tables (idempotent add,
reported remove, name-resolving list), the list-level `visible_*_ids` filters, and
that deleting a container sweeps its membership rows (ON DELETE CASCADE).

Marking a project/space private and the REST/web to manage members arrive in a later
slice; here we flip the `visibility` column directly to exercise the resolver.
"""
import sqlite3

import pytest

from athena.aegis import projects
from athena.core import access, db, users
from athena.mentor import spaces


def _conn(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "access.db")
    db.migrate(conn)
    return conn


def _make_private(conn, table: str, row_id: int) -> None:
    # Slice 5 adds the real setter (on aegis.projects / mentor.spaces) + API; for the
    # resolver's sake we just flip the column. The table name is a fixed literal here.
    conn.execute(f"UPDATE {table} SET visibility = 'private' WHERE id = ?", (row_id,))
    conn.commit()


@pytest.fixture
def people(tmp_path):
    """A connection plus three actors: an admin, the creator of things, and an
    unrelated outsider — the cast every visibility test needs."""
    conn = _conn(tmp_path)
    admin = users.create_user(conn, email="admin@e.com", name="Admin", role="admin")
    creator = users.create_user(conn, email="c@e.com", name="Creator", role="member")
    outsider = users.create_user(conn, email="o@e.com", name="Outsider", role="member")
    return conn, admin, creator, outsider


# --- public: nothing changes, everyone sees everything ----------------------

def test_public_project_is_visible_to_everyone_including_anonymous(people):
    conn, admin, creator, outsider = people
    p = projects.create_project(conn, name="Ops", key="OPS", created_by=creator["id"])

    # Public is the default; every actor — and the signed-out None viewer — can read it.
    for actor in (admin, creator, outsider, None):
        assert access.can_see_project(conn, actor, p["id"]) is True
        assert p["id"] in access.visible_project_ids(conn, actor)


def test_public_space_is_visible_to_everyone_including_anonymous(people):
    conn, admin, creator, outsider = people
    s = spaces.create_space(conn, key="ENG", name="Eng", created_by=creator["id"])

    for actor in (admin, creator, outsider, None):
        assert access.can_see_space(conn, actor, s["id"]) is True
        assert s["id"] in access.visible_space_ids(conn, actor)


# --- private: admins, creator, members in; everyone else out ----------------

def test_private_project_hidden_from_outsiders_and_anonymous(people):
    conn, admin, creator, outsider = people
    p = projects.create_project(conn, name="Secret", key="SEC", created_by=creator["id"])
    _make_private(conn, "projects", p["id"])

    # Creator and admin keep access; the outsider and the signed-out viewer lose it.
    assert access.can_see_project(conn, creator, p["id"]) is True
    assert access.can_see_project(conn, admin, p["id"]) is True
    assert access.can_see_project(conn, outsider, p["id"]) is False
    assert access.can_see_project(conn, None, p["id"]) is False

    assert p["id"] in access.visible_project_ids(conn, creator)
    assert p["id"] in access.visible_project_ids(conn, admin)
    assert p["id"] not in access.visible_project_ids(conn, outsider)
    assert p["id"] not in access.visible_project_ids(conn, None)


def test_private_project_opens_to_an_added_member(people):
    conn, admin, creator, outsider = people
    p = projects.create_project(conn, name="Secret", key="SEC", created_by=creator["id"])
    _make_private(conn, "projects", p["id"])
    assert access.can_see_project(conn, outsider, p["id"]) is False

    # Granting membership lets them in; revoking shuts them back out.
    assert access.add_project_member(conn, p["id"], outsider["id"], added_by=creator["id"]) is True
    assert access.can_see_project(conn, outsider, p["id"]) is True
    assert p["id"] in access.visible_project_ids(conn, outsider)

    assert access.remove_project_member(conn, p["id"], outsider["id"]) is True
    assert access.can_see_project(conn, outsider, p["id"]) is False


def test_private_space_membership_mirrors_projects(people):
    conn, admin, creator, outsider = people
    s = spaces.create_space(conn, key="SEC", name="Secret", created_by=creator["id"])
    _make_private(conn, "spaces", s["id"])

    assert access.can_see_space(conn, outsider, s["id"]) is False
    assert access.add_space_member(conn, s["id"], outsider["id"], added_by=creator["id"]) is True
    assert access.can_see_space(conn, outsider, s["id"]) is True
    assert s["id"] in access.visible_space_ids(conn, outsider)
    # Creator and admin are implicit; the outsider needed the explicit grant.
    assert access.can_see_space(conn, creator, s["id"]) is True
    assert access.can_see_space(conn, admin, s["id"]) is True


# --- membership mechanics ---------------------------------------------------

def test_membership_add_is_idempotent_and_remove_reports(people):
    conn, _admin, creator, outsider = people
    p = projects.create_project(conn, name="P", key="P", created_by=creator["id"])

    assert access.is_project_member(conn, p["id"], outsider["id"]) is False
    assert access.add_project_member(conn, p["id"], outsider["id"], added_by=creator["id"]) is True
    # Re-adding the same pair is a no-op (returns False) so callers log only real grants.
    assert access.add_project_member(conn, p["id"], outsider["id"], added_by=creator["id"]) is False
    assert access.is_project_member(conn, p["id"], outsider["id"]) is True

    assert access.remove_project_member(conn, p["id"], outsider["id"]) is True
    # Removing a non-member reports False so the caller can 404.
    assert access.remove_project_member(conn, p["id"], outsider["id"]) is False


def test_list_project_members_resolves_names_and_excludes_implicit(people):
    conn, admin, creator, outsider = people
    second = users.create_user(conn, email="s2@e.com", name="Alice", role="member")
    p = projects.create_project(conn, name="P", key="P", created_by=creator["id"])
    access.add_project_member(conn, p["id"], outsider["id"], added_by=creator["id"])
    access.add_project_member(conn, p["id"], second["id"], added_by=creator["id"])

    members = access.list_project_members(conn, p["id"])
    # Only the explicitly-added rows, name-resolved, alphabetical — not creator/admin.
    assert [m["name"] for m in members] == ["Alice", "Outsider"]
    assert all(m["added_by"] == creator["id"] and m["added_at"] for m in members)
    assert creator["id"] not in {m["user_id"] for m in members}


def test_unknown_container_is_never_visible(people):
    conn, admin, _creator, _outsider = people
    # A missing project/space resolves to "not visible" (the caller turns it into 404),
    # even for an admin — there's nothing to see.
    assert access.can_see_project(conn, admin, 9999) is False
    assert access.can_see_space(conn, admin, 9999) is False


def test_admin_sees_every_private_container(people):
    conn, admin, creator, outsider = people
    a = projects.create_project(conn, name="A", key="A", created_by=creator["id"])
    b = projects.create_project(conn, name="B", key="B", created_by=outsider["id"])
    _make_private(conn, "projects", a["id"])
    _make_private(conn, "projects", b["id"])

    # The god view: admin's visible set holds both private projects...
    admin_ids = access.visible_project_ids(conn, admin)
    assert {a["id"], b["id"]} <= admin_ids
    # ...while a plain member sees only the one they created.
    assert access.visible_project_ids(conn, creator) == {a["id"]}


# --- schema: deleting a container sweeps its membership ----------------------

def test_deleting_project_cascades_membership(people):
    conn, _admin, creator, outsider = people
    p = projects.create_project(conn, name="Temp", key="TMP", created_by=creator["id"])
    access.add_project_member(conn, p["id"], outsider["id"], added_by=creator["id"])

    # delete_project is allowed once the project holds no issues; the membership row
    # must not block it and must not survive it (ON DELETE CASCADE).
    assert projects.delete_project(conn, p["id"]) is True
    leftover = conn.execute(
        "SELECT COUNT(*) AS n FROM project_members WHERE project_id = ?", (p["id"],)
    ).fetchone()["n"]
    assert leftover == 0
