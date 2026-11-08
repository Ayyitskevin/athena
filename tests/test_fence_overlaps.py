"""Two live leases whose declared file fences overlap.

The claim command refuses an overlapping fence when it is claimed. Overlaps can
still exist afterwards — a root catalog that changed under live leases, rows
written outside the command — and nothing said so. This read finds them:

- every live lease's declared paths, decoded and normalised by the lease
  module's own grammar (``./`` and trailing ``/`` drop, ``\\`` becomes ``/``, a
  ``..`` path is unreadable and never collapsed);
- compared within one resolved tree (root prefix plus path), or lexically when a
  lease's root is unknown; one path overlaps another when they are equal or one
  is a directory prefix of the other (the claim command's own rule);
- an expired lease never counts; a hidden project's lease is never shown to a
  non-member, in rows or counts;
- shown, never enforced: the claim command's policy stays the only gate.
"""

from datetime import UTC, datetime, timedelta
import hashlib
import json

from fastapi.testclient import TestClient

from athena import config
from athena.aegis import fence_overlaps, fleet_attention, office
from athena.core import coordination_roots, db, users
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
ADMIN = {"id": 1, "role": "admin"}
_TS = "%Y-%m-%d %H:%M:%S"


def _app(tmp_path, name="fences.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _agent(client, email, name):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _issue(client, title, **fields):
    return client.post("/issues", json={"title": title, **fields}, headers=H1).json()


def _stamp(**delta):
    return (datetime.now(UTC) + timedelta(**delta)).strftime(_TS)


def _lease(db_file, issue_id, holder_id, paths, *, live=True, root=None):
    """A lease row as it would be stored, written directly: overlaps only exist
    where something bypassed the claim command's check."""
    generation = f"{issue_id:032x}"
    binding = coordination_roots.encode_binding(root, generation) if root else None
    conn = db.connect(db_file)
    conn.execute(
        "INSERT INTO issue_leases (issue_id, holder_id, claimed_at, expires_at, "
        "generation, declared_paths, coordination_root) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            issue_id,
            holder_id,
            _stamp(hours=-1),
            _stamp(hours=1) if live else _stamp(minutes=-5),
            generation,
            json.dumps(paths),
            binding,
        ),
    )
    conn.commit()
    conn.close()


def _pairs(db_file, actor=ADMIN):
    conn = db.connect(db_file)
    try:
        return fence_overlaps.find_overlaps(conn, actor=actor)
    finally:
        conn.close()


def _pair_ids(result):
    return {
        tuple(sorted((p["a"]["issue"]["id"], p["b"]["issue"]["id"])))
        for p in result["pairs"]
    }


def _setup(c):
    _bootstrap(c)
    one = _agent(c, "one@e.com", "One")
    two = _agent(c, "two@e.com", "Two")
    return one["user"]["id"], two["user"]["id"]


# ── overlap and no overlap ──────────────────────────────────────────────────


def test_overlap_and_no_overlap(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        app_dir = _issue(c, "app dir")
        app_file = _issue(c, "app file")
        docs = _issue(c, "docs")
        lookalike = _issue(c, "lookalike")
        issue_only = _issue(c, "issue only")
        _lease(db_file, app_dir["id"], one, ["src/app"])
        _lease(db_file, app_file["id"], two, ["src/app/models.py"])
        _lease(db_file, docs["id"], two, ["docs"])
        _lease(db_file, lookalike["id"], two, ["src/application"])
        _lease(db_file, issue_only["id"], two, [])

        result = _pairs(db_file)
        assert _pair_ids(result) == {tuple(sorted((app_dir["id"], app_file["id"])))}
        assert result["total"] == 1
        (pair,) = result["pairs"]
        assert {pair["a"]["path"], pair["b"]["path"]} == {
            "src/app",
            "src/app/models.py",
        }
        assert {pair["a"]["holder"]["name"], pair["b"]["holder"]["name"]} == {
            "One",
            "Two",
        }
        assert pair["basis"] == "lexical, root unknown"
        # Positive controls: a name that merely starts the same ("src/application")
        # is not inside "src/app", a separate tree is not, and an issue-only lease
        # fences no files.
        ids = {i for p in _pair_ids(result) for i in p}
        assert lookalike["id"] not in ids and docs["id"] not in ids
        assert issue_only["id"] not in ids


def test_equal_paths_overlap_and_one_lease_never_overlaps_itself(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        left = _issue(c, "left")
        right = _issue(c, "right")
        _lease(db_file, left["id"], one, ["a/b", "a/b/c", "z"])
        _lease(db_file, right["id"], two, ["z"])
        result = _pairs(db_file)
        # One pair, however many paths of the two leases touch.
        assert _pair_ids(result) == {tuple(sorted((left["id"], right["id"])))}
        assert result["total"] == 1


def test_the_index_finds_exactly_the_pairs_a_pairwise_check_finds(tmp_path):
    # The read indexes paths and walks each path's ancestors instead of comparing
    # every pair. On random fences it must find exactly what the claim command's
    # own paths_overlap finds pair by pair.
    import random

    from athena.aegis.lease_commands import paths_overlap

    rng = random.Random(20261108)
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        fences = {}
        for n in range(30):
            issue = _issue(c, f"random {n}")
            paths = [
                "/".join(rng.choice("abc") for _ in range(rng.randint(1, 3)))
                for _ in range(rng.randint(1, 3))
            ]
            _lease(db_file, issue["id"], one if n % 2 else two, paths)
            fences[issue["id"]] = list(dict.fromkeys(paths))
    expected = {
        (a, b)
        for a in fences
        for b in fences
        if a < b and any(paths_overlap(x, y) for x in fences[a] for y in fences[b])
    }
    result = _pairs(db_file)
    conn = db.connect(db_file)
    everything = fence_overlaps.find_overlaps(conn, actor=ADMIN, limit=10_000)
    conn.close()
    assert _pair_ids(everything) == expected
    assert everything["total"] == len(expected) == result["total"]
    # Positive control: the random data has both overlapping and separate pairs.
    assert 0 < len(expected) < len(fences) * (len(fences) - 1) // 2


# ── normalisation ───────────────────────────────────────────────────────────


def test_normalisation_trailing_slash_dot_and_dotdot(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        slash = _issue(c, "trailing slash")
        dot = _issue(c, "dot prefix")
        dotdot = _issue(c, "dotdot")
        etc = _issue(c, "etc")
        _lease(db_file, slash["id"], one, ["lib/"])
        _lease(db_file, dot["id"], two, ["./lib/x.py"])
        # ".." is never collapsed: "lib/../etc" is unreadable, not "etc".
        _lease(db_file, dotdot["id"], one, ["lib/../etc"])
        _lease(db_file, etc["id"], two, ["etc"])

        result = _pairs(db_file)
        assert _pair_ids(result) == {tuple(sorted((slash["id"], dot["id"])))}
        (pair,) = result["pairs"]
        assert {pair["a"]["path"], pair["b"]["path"]} == {"lib", "lib/x.py"}
        assert result["unreadable_fences"] == 1
        # Positive control: the same lease written plainly as "etc" overlaps.
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE issue_leases SET declared_paths = ? WHERE issue_id = ?",
            (json.dumps(["etc"]), dotdot["id"]),
        )
        conn.commit()
        conn.close()
        again = _pairs(db_file)
        assert tuple(sorted((dotdot["id"], etc["id"]))) in _pair_ids(again)
        assert again["unreadable_fences"] == 0


# ── an expired lease never counts ───────────────────────────────────────────


def test_an_expired_lease_never_counts(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        live = _issue(c, "live")
        lapsed = _issue(c, "lapsed")
        _lease(db_file, live["id"], one, ["x"])
        _lease(db_file, lapsed["id"], two, ["x"], live=False)
        assert _pairs(db_file)["total"] == 0
        # Positive control: the same lease, live again, overlaps. (Reactivating a
        # lapsed row needs a fresh generation: a trigger insists, like a re-claim.)
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE issue_leases SET expires_at = ?, generation = ? WHERE issue_id = ?",
            (_stamp(hours=1), "f" * 32, lapsed["id"]),
        )
        conn.commit()
        conn.close()
        assert _pairs(db_file)["total"] == 1


# ── roots: one tree, compared by its real prefix ────────────────────────────


CATALOG = {
    "version": 1,
    "qualification": {
        "storage_namespace": "fictional-storage",
        "topology_epoch": "epoch-1",
        "coverage": "whole-entry",
        "path_semantics": "posix-case-sensitive-normalization-preserving-v1",
        "alias_closed": True,
        "families_disjoint": True,
        "effects": "declared-checkout-files-only",
    },
    "roots": [
        {
            "root_key": "one",
            "tree_id": "tree-a",
            "prefix": "",
            "evidence_sha256": "a" * 64,
        },
        {
            "root_key": "nested",
            "tree_id": "tree-a",
            "prefix": "sub",
            "evidence_sha256": "a" * 64,
        },
        {
            "root_key": "two",
            "tree_id": "tree-b",
            "prefix": "",
            "evidence_sha256": "b" * 64,
        },
    ],
}
CATALOG_JSON = json.dumps(CATALOG, separators=(",", ":"))
CATALOG_SHA = hashlib.sha256(CATALOG_JSON.encode()).hexdigest()


def _root(key):
    return {"catalog_sha256": CATALOG_SHA, "root_key": key, "relative_prefix": ""}


def test_rooted_fences_compare_by_tree_and_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "LEASE_ROOT_CATALOG", coordination_roots.parse_catalog(CATALOG_JSON)
    )
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        whole = _issue(c, "tree-a sub dir")
        nested = _issue(c, "tree-a nested file")
        other_tree = _issue(c, "tree-b sub dir")
        unrooted = _issue(c, "root unknown")
        _lease(db_file, whole["id"], one, ["sub"], root=_root("one"))
        # Root "nested" sits at tree-a/sub, so its "x" is tree-a's "sub/x".
        _lease(db_file, nested["id"], two, ["x"], root=_root("nested"))
        _lease(db_file, other_tree["id"], two, ["sub"], root=_root("two"))
        _lease(db_file, unrooted["id"], two, ["sub"])
        result = _pairs(db_file)
        assert _pair_ids(result) == {tuple(sorted((whole["id"], nested["id"])))}
        (pair,) = result["pairs"]
        assert pair["basis"] == "same root"
        # Positive controls: the same "sub" in another tree does not overlap, and a
        # fence whose root is unknown is not compared with a rooted one.
        ids = {i for p in _pair_ids(result) for i in p}
        assert other_tree["id"] not in ids and unrooted["id"] not in ids


# ── visibility ──────────────────────────────────────────────────────────────


def test_a_hidden_projects_lease_is_never_revealed_to_a_non_member(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        outsider = c.post(
            "/users",
            json={"email": "out@e.com", "name": "Out", "password": "pw"},
            headers=H1,
        ).json()
        project = c.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=H1
        ).json()
        hidden = _issue(c, "hidden fence", project_id=project["id"])
        public = _issue(c, "public fence")
        public_too = _issue(c, "public fence too")
        _lease(db_file, hidden["id"], one, ["p"])
        _lease(db_file, public["id"], two, ["p"])
        _lease(db_file, public_too["id"], one, ["q"])
        c.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        conn = db.connect(db_file)
        out = users.get_user(conn, outsider["id"])
        theirs = fence_overlaps.find_overlaps(conn, actor=out)
        ranking = fleet_attention.build_attention_ranking(
            conn, actor=out, signals={"fence_overlap"}
        )
        two_user = users.get_user(conn, two)
        two_office = office.build_office(conn, actor=two_user)
        mine = fence_overlaps.find_overlaps(conn, actor=ADMIN)
        conn.close()
        assert theirs["pairs"] == [] and theirs["total"] == 0
        assert ranking["items"] == [] and ranking["total"] == 0
        assert "hidden fence" not in json.dumps(theirs) + str(ranking)
        # The holder of the public lease cannot see the hidden issue either: its
        # office does not reveal it.
        assert two_office["fence_overlaps"] == []
        assert "hidden fence" not in json.dumps(two_office)
        # Positive control: the admin sees the pair.
        assert _pair_ids(mine) == {tuple(sorted((hidden["id"], public["id"])))}


# ── where it shows ──────────────────────────────────────────────────────────


def test_the_card_counts_pairs_and_ranks_each_pair_with_both_issues(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        left = _issue(c, "left work")
        right = _issue(c, "right work")
        _lease(db_file, left["id"], one, ["src"])
        _lease(db_file, right["id"], two, ["src/x.py"])
        conn = db.connect(db_file)
        card = fleet_attention.build_attention(conn, actor=ADMIN)
        quiet_card = fleet_attention.build_attention(conn)
        conn.close()
        signal = next(s for s in card["signals"] if s["key"] == "fence_overlaps")
        assert signal["count"] == 1
        assert (
            next(s for s in quiet_card["signals"] if s["key"] == "fence_overlaps")[
                "count"
            ]
            == 1
        )
        rows = [i for i in card["now"]["items"] if i["signal"] == "fence_overlap"]
        (row,) = rows
        links = {row["link"], row["source_link"]}
        assert links == {f"/aegis/issues/{left['id']}", f"/aegis/issues/{right['id']}"}
        assert "One" in row["reason"] and "Two" in row["reason"]
        # The card stays within its lane rule: no new composed lane.
        assert set(card["progress"]) >= {"expired_leases", "stale_in_progress"}
        assert "fence_overlaps" not in card["progress"]


def test_each_holders_office_says_whose_fence_it_overlaps(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        left = _issue(c, "left work")
        right = _issue(c, "right work")
        alone = _issue(c, "alone")
        _lease(db_file, left["id"], one, ["src"])
        _lease(db_file, right["id"], two, ["src/x.py"])
        conn = db.connect(db_file)
        one_office = office.build_office(conn, actor=users.get_user(conn, one))
        two_office = office.build_office(conn, actor=users.get_user(conn, two))
        conn.close()
        (mine,) = one_office["fence_overlaps"]
        assert mine["issue"]["id"] == right["id"] and mine["holder"]["name"] == "Two"
        assert (
            f"your fence overlaps issue #{right['id']}, held by Two"
            in one_office["warnings"]
        )
        (theirs,) = two_office["fence_overlaps"]
        assert theirs["issue"]["id"] == left["id"] and theirs["holder"]["name"] == "One"
        assert (
            f"your fence overlaps issue #{left['id']}, held by One"
            in two_office["warnings"]
        )
        # Positive control: an issue with no overlap is not mentioned.
        assert "alone" not in json.dumps(one_office["fence_overlaps"])
        assert alone["id"]


def test_nothing_is_blocked_and_reading_writes_nothing(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        one, two = _setup(c)
        left = _issue(c, "left work")
        right = _issue(c, "right work")
        _lease(db_file, left["id"], one, ["src"])
        _lease(db_file, right["id"], two, ["src"])
        conn = db.connect(db_file)
        before_rows = [tuple(r) for r in conn.execute("SELECT * FROM issue_leases")]
        before = conn.total_changes
        fence_overlaps.find_overlaps(conn, actor=ADMIN)
        fleet_attention.build_attention(conn, actor=ADMIN)
        office.build_office(conn, actor=users.get_user(conn, one))
        assert conn.total_changes == before
        assert [
            tuple(r) for r in conn.execute("SELECT * FROM issue_leases")
        ] == before_rows
        conn.close()
        # The overlapping holders can still read their own leases: nothing refuses.
        assert c.get(f"/issues/{left['id']}/lease", headers=H1).status_code == 200
