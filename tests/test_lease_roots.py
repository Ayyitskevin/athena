"""Declared work roots: coordination evidence, never filesystem authority."""

import copy
import hashlib
import json
import runpy

from fastapi.testclient import TestClient
import pytest

from athena import config
from athena.main import create_app


# Explicitly fictional topology: these declarations do not inspect any host.
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
            "root_key": "alias",
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


def root(key="one", prefix=""):
    return {"catalog_sha256": CATALOG_SHA, "root_key": key, "relative_prefix": prefix}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Exercise the actual process-configuration parser without reloading the
    # conftest's synthetic authentication/network/attachment settings. On the
    # original binary the new configuration is simply absent: the first red
    # therefore reaches the old REST collision, not a missing-module error.
    monkeypatch.setenv("ATHENA_LEASE_ROOT_CATALOG", CATALOG_JSON)
    configured = runpy.run_path(config.__file__)
    monkeypatch.setattr(
        config,
        "LEASE_ROOT_CATALOG",
        configured.get("LEASE_ROOT_CATALOG"),
        raising=False,
    )
    with TestClient(create_app(tmp_path / "roots.db")) as tc:
        response = tc.post(
            "/users",
            json={
                "email": "owner@example.test",
                "name": "Owner",
                "password": "synthetic-password",
            },
        )
        assert response.status_code == 201, response.text
        tc.headers["X-Athena-Actor"] = "1"
        yield tc


def issue(client, title="work"):
    response = client.post("/issues", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def claim(client, issue_id, **payload):
    reviewed = client.get(f"/issues/{issue_id}")
    assert reviewed.status_code == 200, reviewed.text
    return client.post(
        f"/issues/{issue_id}/claim",
        json=payload,
        headers={"If-Match": reviewed.headers["ETag"]},
    )


def test_evidenced_separate_roots_can_claim_identical_relative_paths(client):
    first, second = issue(client, "first"), issue(client, "second")
    accepted = claim(client, first, paths=["docs"], coordination_root=root("one"))
    assert accepted.status_code == 201, accepted.text
    separate = claim(
        client, second, paths=["docs/change.md"], coordination_root=root("two")
    )
    assert separate.status_code == 201, separate.text
    assert separate.json()["coordination_root"] == root("two")
    assert separate.json()["path_fence"] == "rooted"


def test_old_client_renewal_keeps_the_root_and_paths(client):
    item = issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    renewed = claim(client, item, generation=first["generation"])
    assert renewed.status_code == 201, renewed.text
    assert renewed.json()["generation"] == first["generation"]
    assert renewed.json()["declared_paths"] == ["docs"]
    assert renewed.json()["coordination_root"] == first["coordination_root"]
    assert renewed.json()["path_fence"] == "rooted"


def test_catalog_without_whole_entry_qualification_cannot_start(monkeypatch):
    # A syntactically valid root map alone is not evidence of safe separation.
    incomplete = copy.deepcopy(CATALOG)
    del incomplete["qualification"]
    monkeypatch.setenv("ATHENA_LEASE_ROOT_CATALOG", json.dumps(incomplete))
    with pytest.raises(ValueError, match="qualification"):
        runpy.run_path(config.__file__)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("alias_closed", False, id="interior-alias-not-lexically-closed"),
        pytest.param("families_disjoint", False, id="cross-entry-alias"),
        pytest.param("path_semantics", "case-insensitive", id="case-alias"),
        pytest.param("path_semantics", "unicode-normalizing", id="normalization-alias"),
        ("coverage", "partial-subtrees"),
        ("effects", "shared-git-and-caches"),
        ("alias_closed", 1),
        ("storage_namespace", "../host"),
        ("topology_epoch", ""),
    ],
)
def test_unsupported_qualification_cannot_start(monkeypatch, field, value):
    unsupported = copy.deepcopy(CATALOG)
    unsupported["qualification"][field] = value
    monkeypatch.setenv("ATHENA_LEASE_ROOT_CATALOG", json.dumps(unsupported))
    # A false operator declaration is rejected before an app can admit either
    # of its claims. This does not discover real aliases or validate truth.
    with pytest.raises(ValueError):
        runpy.run_path(config.__file__)


@pytest.mark.parametrize(
    ("left", "left_path", "right", "right_path"),
    [
        (root("one"), "docs", root("alias"), "docs/file"),
        (root("one"), "sub/docs", root("nested"), "docs"),
        (root("one", "sub"), "docs", root("nested"), "docs/file"),
    ],
)
def test_aliases_and_nested_roots_conflict(client, left, left_path, right, right_path):
    first, second = issue(client), issue(client)
    assert (
        claim(client, first, paths=[left_path], coordination_root=left).status_code
        == 201
    )
    denied = claim(client, second, paths=[right_path], coordination_root=right)
    assert denied.status_code == 409
    assert (
        denied.json()["detail"] == "declared paths conflict with another active lease"
    )
    assert client.get(f"/issues/{second}/lease").json() is None


def test_canonical_join_bound_applies_without_a_competitor(client):
    item = issue(client)
    denied = claim(
        client, item, paths=["b" * 256], coordination_root=root(prefix="a" * 256)
    )
    assert denied.status_code == 422
    assert client.get(f"/issues/{item}/lease").json() is None


@pytest.mark.parametrize("requested", [root("two"), root("one", "other")])
def test_root_identity_change_requires_new_possession(client, requested):
    item = issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    denied = claim(
        client, item, generation=first["generation"], coordination_root=requested
    )
    assert denied.status_code == 409
    stored = client.get(f"/issues/{item}/lease").json()
    assert stored["generation"] == first["generation"]
    assert stored["coordination_root"] == first["coordination_root"]
    assert stored["declared_paths"] == ["docs"]


def test_equivalent_alias_renewal_retains_original_binding(client):
    item = issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    result = claim(
        client, item, generation=first["generation"], coordination_root=root("alias")
    )
    assert result.status_code == 201
    assert result.json()["coordination_root"] == root()
    assert result.json()["declared_paths"] == ["docs"]


@pytest.mark.parametrize("known_first", [False, True])
def test_nonempty_unknown_origin_never_proves_disjointness(client, known_first):
    first, second = issue(client), issue(client)
    assert (
        claim(
            client,
            first,
            paths=["docs"],
            coordination_root=root() if known_first else None,
        ).status_code
        == 201
    )
    denied = claim(
        client,
        second,
        paths=["unrelated"],
        coordination_root=None if known_first else root("two"),
    )
    assert denied.status_code == 409


def test_explicit_empty_renewal_is_audited_as_issue_only(client):
    item = issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    result = claim(client, item, generation=first["generation"], paths=[])
    assert result.status_code == 201
    renewed = result.json()
    assert renewed["generation"] == first["generation"]
    assert renewed["declared_paths"] == []
    assert renewed["coordination_root"] == root()
    assert renewed["path_fence"] == "issue_only"
    events = client.get(
        "/activity", params={"target_kind": "issue", "target_id": item}
    ).json()
    event = next(event for event in events if event["verb"] == "lease_renewed")
    assert "path_fence issue_only" in event["detail"]
    assert first["generation"] in event["detail"]


def test_official_client_forwards_root_to_real_rest(client):
    from athena.mcp.client import AthenaClient

    item = issue(client)
    tag = client.get(f"/issues/{item}").headers["ETag"]
    result = AthenaClient(client=client).claim_issue(
        item,
        if_match=tag,
        paths=["docs"],
        coordination_root=root(),
    )
    assert result["coordination_root"] == root()
    assert result["path_fence"] == "rooted"


def test_office_and_desk_expose_the_same_qualified_fence(client):
    item = issue(client)
    assert (
        claim(client, item, paths=["docs"], coordination_root=root()).status_code == 201
    )
    chair = client.get("/office").json()["chair"]
    assert chair["coordination_root"] == root()
    assert chair["path_fence"] == "rooted"
    desk = client.get("/desk").json()
    assert desk["office"]["chair"]["coordination_root"] == root()


@pytest.mark.parametrize(
    "echo",
    [None, {"catalog_sha256": "b" * 64, "root_key": "other", "relative_prefix": ""}],
)
def test_client_refuses_unconfirmed_root_success_without_retry(echo):
    import httpx
    from athena.mcp.client import AthenaClient, AthenaError

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            201, json={"generation": "a" * 32, "coordination_root": echo}
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond), base_url="https://fictional.test"
    ) as transport:
        client = AthenaClient(client=transport)
        with pytest.raises(AthenaError) as caught:
            client.claim_issue(
                1, if_match='"etag"', paths=["docs"], coordination_root=root()
            )
    assert caught.value.code == "coordination_root_unconfirmed"
    assert "may already exist" in str(caught.value)
    assert len(calls) == 1
    assert json.loads(calls[0].content)["coordination_root"] == root()


def test_epoch_change_preserves_unresolved_binding_on_renewal(client, monkeypatch):
    item, other = issue(client), issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    old_catalog = config.LEASE_ROOT_CATALOG
    replacement = copy.deepcopy(CATALOG)
    replacement["qualification"]["topology_epoch"] = "epoch-2"
    monkeypatch.setenv("ATHENA_LEASE_ROOT_CATALOG", json.dumps(replacement))
    changed = runpy.run_path(config.__file__)["LEASE_ROOT_CATALOG"]
    monkeypatch.setattr(config, "LEASE_ROOT_CATALOG", changed)
    assert client.get(f"/issues/{item}/lease").json()["path_fence"] == "unresolved"
    renewed = claim(
        client, item, generation=first["generation"], paths=None, coordination_root=None
    )
    assert renewed.status_code == 201
    assert renewed.json()["coordination_root"] is None
    assert renewed.json()["declared_paths"] == ["docs"]
    current_root = {**root("two"), "catalog_sha256": changed.sha256}
    assert (
        claim(
            client, other, paths=["unrelated"], coordination_root=current_root
        ).status_code
        == 409
    )
    # Observation of retained bytes via the public reader, not a rollout recipe:
    # restoring the test's original catalog shows the original binding survived.
    monkeypatch.setattr(config, "LEASE_ROOT_CATALOG", old_catalog)
    assert client.get(f"/issues/{item}/lease").json()["coordination_root"] == root()


def test_unavailable_selection_refuses_even_issue_only_and_cannot_upgrade_legacy(
    client,
):
    item = issue(client)
    missing = {**root(), "root_key": "missing"}
    assert claim(client, item, paths=[], coordination_root=missing).status_code == 409
    assert client.get(f"/issues/{item}/lease").json() is None
    first = claim(client, item, paths=["docs"]).json()
    denied = claim(
        client, item, generation=first["generation"], coordination_root=root()
    )
    assert denied.status_code == 409
    assert client.get(f"/issues/{item}/lease").json()["path_fence"] == "unresolved"


@pytest.mark.parametrize(
    "selector",
    [
        {**root(), "qualification": CATALOG["qualification"]},
        {**root(), "relative_prefix": "../escape"},
        {**root(), "relative_prefix": "/absolute"},
        {**root(), "relative_prefix": "x" * 257},
        {**root(), "catalog_sha256": "bad"},
    ],
)
def test_invalid_root_selector_is_atomic(client, selector):
    item = issue(client)
    before = client.get(
        "/activity", params={"target_kind": "issue", "target_id": item}
    ).json()
    assert (
        claim(client, item, paths=["docs"], coordination_root=selector).status_code
        == 422
    )
    assert client.get(f"/issues/{item}/lease").json() is None
    assert (
        client.get(
            "/activity", params={"target_kind": "issue", "target_id": item}
        ).json()
        == before
    )


def test_stale_etag_and_generation_cannot_drop_fence(client):
    item = issue(client)
    old_tag = client.get(f"/issues/{item}").headers["ETag"]
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    updated = client.patch(
        f"/issues/{item}", json={"title": "changed"}, headers={"If-Match": old_tag}
    )
    assert updated.status_code == 200, updated.text
    before = client.get(f"/issues/{item}/lease").json()
    denied = client.post(
        f"/issues/{item}/claim",
        json={"generation": first["generation"], "paths": []},
        headers={"If-Match": old_tag},
    )
    assert denied.status_code == 412
    assert claim(client, item, generation="0" * 32, paths=[]).status_code == 409
    assert client.get(f"/issues/{item}/lease").json() == before


def test_cross_issue_alias_race_has_one_winner(client):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from athena.aegis import issue_commands, lease_commands, leases
    from athena.core import db, users

    items = [issue(client), issue(client)]
    tags = [client.get(f"/issues/{item}").headers["ETag"] for item in items]
    barrier = threading.Barrier(2)

    def compete(index):
        conn = db.connect(client.app.state.db_path)
        try:
            actor = users.get_user(conn, 1)
            barrier.wait(timeout=5)
            try:
                lease_commands.claim_issue(
                    conn,
                    actor=actor,
                    issue_id=items[index],
                    if_match=[tags[index]],
                    paths=["docs"],
                    coordination_root=root("one" if index == 0 else "alias"),
                )
                return "claimed"
            except issue_commands.IssueCommandError as exc:
                return exc.kind
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, i) for i in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ["claimed", "conflict"]
    conn = db.connect(client.app.state.db_path)
    try:
        assert len(leases.list_active_leases(conn)) == 1
    finally:
        conn.close()


def test_audit_storage_failure_rolls_back_root_and_empty_transition(client):
    import sqlite3
    from athena.core import db

    item = issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    before_events = client.get(
        "/activity", params={"target_kind": "issue", "target_id": item}
    ).json()
    conn = db.connect(client.app.state.db_path)
    try:
        # Real test-database storage failure, not a mock of a command collaborator.
        conn.execute(
            "CREATE TRIGGER reject_lease_event BEFORE INSERT ON activity WHEN NEW.verb = 'lease_renewed' BEGIN SELECT RAISE(ABORT, 'synthetic audit unavailable'); END"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit unavailable"):
        claim(client, item, generation=first["generation"], paths=[])
    assert client.get(f"/issues/{item}/lease").json() == first
    assert (
        client.get(
            "/activity", params={"target_kind": "issue", "target_id": item}
        ).json()
        == before_events
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "{}",
        "x" * 2049,
        json.dumps(
            {
                "version": 1,
                **root(),
                "possession_generation": "0" * 32,
            }
        ),
    ],
)
def test_malformed_or_wrong_generation_stored_binding_is_unresolved(client, raw):
    from athena.core import db

    item, other = issue(client), issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    conn = db.connect(client.app.state.db_path)
    try:
        # Simulate legacy/corrupted persisted input; observe via the real reader.
        conn.execute(
            "UPDATE issue_leases SET coordination_root = ? WHERE issue_id = ?",
            (raw, item),
        )
        conn.commit()
    finally:
        conn.close()
    observed = client.get(f"/issues/{item}/lease").json()
    assert observed["generation"] == first["generation"]
    assert observed["coordination_root"] is None
    assert observed["path_fence"] == "unresolved"
    assert (
        claim(
            client, other, paths=["elsewhere"], coordination_root=root("two")
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate-json-key",
        "duplicate-root",
        "unknown-field",
        "missing-qualification-field",
        "unknown-qualification-field",
        "too-many-roots",
        "too-many-bytes",
        "boolean-version",
        "nan",
        "invalid-prefix",
        "invalid-evidence",
    ],
)
def test_catalog_shape_rejection_is_strict(monkeypatch, kind):
    catalog = copy.deepcopy(CATALOG)
    if kind == "duplicate-root":
        catalog["roots"].append(catalog["roots"][0])
    elif kind == "unknown-field":
        catalog["extra"] = True
    elif kind == "missing-qualification-field":
        del catalog["qualification"]["alias_closed"]
    elif kind == "unknown-qualification-field":
        catalog["qualification"]["override"] = True
    elif kind == "too-many-roots":
        catalog["roots"] = [
            {**catalog["roots"][0], "root_key": f"root-{n}"} for n in range(33)
        ]
    elif kind == "boolean-version":
        catalog["version"] = True
    elif kind == "nan":
        catalog["version"] = float("nan")
    elif kind == "invalid-prefix":
        catalog["roots"][0]["prefix"] = "../escape"
    elif kind == "invalid-evidence":
        catalog["roots"][0]["evidence_sha256"] = "no-evidence"
    raw = json.dumps(catalog)
    if kind == "duplicate-json-key":
        raw = raw.replace('"version": 1', '"version": 1, "version": 1', 1)
    elif kind == "too-many-bytes":
        raw += " " * 16384
    monkeypatch.setenv("ATHENA_LEASE_ROOT_CATALOG", raw)
    with pytest.raises(ValueError):
        runpy.run_path(config.__file__)


def test_old_schema_migration_keeps_possession_and_unknown_fence(
    tmp_path, monkeypatch, migration_inventory_through
):
    from athena.aegis import leases
    from athena.core import db

    full_inventory = db.MIGRATIONS_DIR
    migration_inventory_through("0080_notification_priority.sql")
    conn = db.connect(tmp_path / "historical.db")
    try:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO users (id,email,name) VALUES (1,'fictional@example.test','Fixture')"
        )
        conn.execute(
            "INSERT INTO issues (id,title,created_by) VALUES (1,'Retained fixture',1)"
        )
        conn.execute(
            "INSERT INTO issue_leases (issue_id,holder_id,expires_at,generation,declared_paths) VALUES (1,1,datetime('now','+1 hour'),?,?)",
            ("a" * 32, '["docs"]'),
        )
        conn.commit()
        monkeypatch.setattr(db, "MIGRATIONS_DIR", full_inventory)
        db.migrate(conn)
        retained = leases.get_lease(conn, 1)
        assert retained["holder_id"] == 1
        assert retained["generation"] == "a" * 32
        assert retained["declared_paths"] == ["docs"]
        assert retained["coordination_root"] is None
        assert retained["path_fence"] == "unresolved"
    finally:
        conn.close()


def test_hidden_issue_still_blocks_but_denial_does_not_disclose(client):
    from athena.core import db

    project = client.post(
        "/projects", json={"name": "Fictional hidden", "key": "FIC"}
    ).json()["id"]
    hidden = client.post(
        "/issues", json={"title": "Fictional private work", "project_id": project}
    ).json()["id"]
    outsider = client.post(
        "/users",
        json={
            "email": "outsider@example.test",
            "name": "Other",
            "password": "synthetic-password",
            "role": "member",
        },
    ).json()["id"]
    public = issue(client, "Public fixture")
    assigned = client.put(f"/issues/{public}/assignee", json={"assignee_id": outsider})
    assert assigned.status_code == 200, assigned.text
    assert (
        claim(client, hidden, paths=["docs"], coordination_root=root()).status_code
        == 201
    )
    conn = db.connect(client.app.state.db_path)
    try:
        conn.execute(
            "UPDATE projects SET visibility = 'private' WHERE id = ?", (project,)
        )
        conn.commit()
    finally:
        conn.close()
    client.headers["X-Athena-Actor"] = str(outsider)
    assert client.get(f"/issues/{hidden}").status_code == 404
    denied = claim(client, public, paths=["docs"], coordination_root=root("alias"))
    assert denied.status_code == 409
    assert denied.json() == {
        "detail": "declared paths conflict with another active lease"
    }
    assert (
        claim(client, public, paths=["docs"], coordination_root=root("two")).status_code
        == 201
    )


def test_project_floor_and_occupancy_match_lease_reader(client):
    from athena.aegis import office
    from athena.core import db

    project = client.post(
        "/projects", json={"name": "Fictional floor", "key": "FLR"}
    ).json()["id"]
    item = client.post(
        "/issues", json={"title": "Chair", "project_id": project}
    ).json()["id"]
    assert (
        claim(client, item, paths=["docs"], coordination_root=root()).status_code == 201
    )
    floor = client.get(f"/projects/{project}/floor").json()
    occupant = next(chair for chair in floor["chairs"] if chair["issue_id"] == item)[
        "occupant"
    ]
    assert occupant["coordination_root"] == root()
    assert occupant["path_fence"] == "rooted"
    conn = db.connect(client.app.state.db_path)
    try:
        row = next(
            row for row in office.build_occupancy(conn) if row["issue_id"] == item
        )
        assert row["coordination_root"] == root()
        assert row["path_fence"] == "rooted"
    finally:
        conn.close()


def test_real_mcp_claim_and_idempotency_bind_root(client):
    import asyncio
    from athena.mcp.client import AthenaClient
    from athena.mcp.server import build_server

    item = issue(client)
    athena = AthenaClient(client=client)
    server = build_server(client=athena)
    args = {
        "issue_id": item,
        "if_match": client.get(f"/issues/{item}").headers["ETag"],
        "paths": ["docs"],
        "coordination_root": root(),
        "idempotency_key": "fictional-root-claim",
    }
    first = asyncio.run(server.call_tool("claim_issue", args))
    assert asyncio.run(server.call_tool("claim_issue", args)) == first
    lease = client.get(f"/issues/{item}/lease").json()
    assert lease["coordination_root"] == root()
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="idempotency_mismatch"):
        asyncio.run(
            server.call_tool("claim_issue", {**args, "coordination_root": root("two")})
        )
    assert client.get(f"/issues/{item}/lease").json() == lease


def test_qualified_directory_nonoverlap_can_coexist(client):
    first, second = issue(client), issue(client)
    assert (
        claim(client, first, paths=["src/foo"], coordination_root=root()).status_code
        == 201
    )
    assert (
        claim(
            client, second, paths=["src/foobar"], coordination_root=root("alias")
        ).status_code
        == 201
    )


def test_expired_reacquisition_replaces_whole_root_binding(client):
    from athena.core import db

    item = issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    conn = db.connect(client.app.state.db_path)
    try:
        conn.execute(
            "UPDATE issue_leases SET expires_at = datetime('now','-1 second') WHERE issue_id = ?",
            (item,),
        )
        conn.commit()
    finally:
        conn.close()
    assert (
        claim(
            client, item, generation=first["generation"], coordination_root=root("two")
        ).status_code
        == 409
    )
    fresh = claim(client, item, paths=["docs"], coordination_root=root("two"))
    assert fresh.status_code == 201
    assert fresh.json()["generation"] != first["generation"]
    assert fresh.json()["coordination_root"] == root("two")


def test_release_does_not_need_original_catalog(client, monkeypatch):
    item = issue(client)
    first = claim(client, item, paths=["docs"], coordination_root=root()).json()
    monkeypatch.setattr(config, "LEASE_ROOT_CATALOG", None)
    from athena.mcp.client import AthenaClient

    result = AthenaClient(client=client).complete_claim(
        item, generation=first["generation"]
    )
    assert result["released"] is True
    assert client.get(f"/issues/{item}/lease").json() is None


def test_drive_prefix_cannot_hide_behind_dot_segments(client):
    item = issue(client)
    result = claim(
        client, item, paths=["docs"], coordination_root=root(prefix="./C:/task")
    )
    assert result.status_code == 422
    assert client.get(f"/issues/{item}/lease").json() is None


def test_catalog_drive_prefix_cannot_hide_behind_dot_segments(monkeypatch):
    unsupported = copy.deepcopy(CATALOG)
    unsupported["roots"][0]["prefix"] = "./C:/task"
    monkeypatch.setenv("ATHENA_LEASE_ROOT_CATALOG", json.dumps(unsupported))
    with pytest.raises(ValueError, match="prefix"):
        runpy.run_path(config.__file__)


@pytest.mark.parametrize("echo_generation", [None, "bad", "b" * 32])
def test_client_requires_exact_renewal_generation_echo(echo_generation):
    import httpx
    from athena.mcp.client import AthenaClient, AthenaError

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            201, json={"generation": echo_generation, "coordination_root": root()}
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond), base_url="https://fictional.test"
    ) as transport:
        with pytest.raises(AthenaError, match="may already exist"):
            AthenaClient(client=transport).claim_issue(
                1,
                if_match='"etag"',
                generation="a" * 32,
                coordination_root=root(),
            )
    assert len(calls) == 1


def test_client_normalizes_optional_selector_and_alias_renewal_is_unconfirmed(client):
    from athena.mcp.client import AthenaClient, AthenaError

    item = issue(client)
    athena = AthenaClient(client=client)
    tag = client.get(f"/issues/{item}").headers["ETag"]
    selected = root()
    del selected["relative_prefix"]
    first = athena.claim_issue(
        item, if_match=tag, paths=["docs"], coordination_root=selected
    )
    assert first["coordination_root"] == root()
    with pytest.raises(AthenaError, match="may already exist"):
        athena.claim_issue(
            item,
            if_match=tag,
            generation=first["generation"],
            coordination_root=root("alias"),
        )
    stored = client.get(f"/issues/{item}/lease").json()
    assert stored["coordination_root"] == root()
    assert stored["generation"] == first["generation"]
    assert stored["declared_paths"] == ["docs"]


def test_explicit_stale_catalog_is_conflict_not_unknown_fallback(client):
    item = issue(client)
    result = claim(
        client,
        item,
        paths=["docs"],
        coordination_root={**root(), "catalog_sha256": "f" * 64},
    )
    assert result.status_code == 409
    assert client.get(f"/issues/{item}/lease").json() is None
