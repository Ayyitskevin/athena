"""Path fences on a default instance: disjoint paths coexist, overlaps say so (C6).

WHY: on an instance with no root catalog, which is the default, a second lease
declaring any path at all was refused 409 ``conflict``, whatever the path, in
any project, and for a holder the refused seat could not see; the refusal for
an unrelated path was byte for byte the refusal for the same path. A fleet
whose recipe tells every worker to declare its paths could run one worker at a
time (FLEET-1). Decision D5 settles it: with no catalog, paths are compared
lexically under one implicit root and the basis is reported as "lexical, root
unknown". These tests hold that rule, the refusal an overlap answers with and
what it may name, pickup with paths, and the catalog digest a selector needs.
"""

from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, as_user, bootstrap, add_user, lapse_lease, make_app

from athena import config
from athena.core import coordination_roots, refusal_codes

LEXICAL = "lexical, root unknown"

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
            "root_key": "two",
            "tree_id": "tree-b",
            "prefix": "",
            "evidence_sha256": "b" * 64,
        },
    ],
}
CATALOG_JSON = json.dumps(CATALOG, separators=(",", ":"))
CATALOG_SHA = hashlib.sha256(CATALOG_JSON.encode()).hexdigest()


def root(key: str = "one") -> dict:
    return {"catalog_sha256": CATALOG_SHA, "root_key": key, "relative_prefix": ""}


class World:
    """An admin (1) and three member seats (2, 3, 4), a public project and the
    shared backlog. Each seat is the assignee of the issues it claims."""

    def __init__(self, client: TestClient, db_file):
        self.client = client
        self.db_file = db_file
        self.project = self.make_project("Public", "PUB")

    def make_project(self, name: str, key: str, *, private: bool = False) -> int:
        made = self.client.post(
            "/projects", json={"name": name, "key": key}, headers=ADMIN
        )
        assert made.status_code == 201, made.text
        project_id = made.json()["id"]
        if private:
            flipped = self.client.put(
                f"/projects/{project_id}/visibility",
                json={"visibility": "private"},
                headers=ADMIN,
            )
            assert flipped.status_code == 200, flipped.text
        return project_id

    def issue(self, assignee: int, *, project_id: int | None = -1, title="work"):
        body = {"title": title}
        if project_id == -1:
            body["project_id"] = self.project
        elif project_id is not None:
            body["project_id"] = project_id
        made = self.client.post("/issues", json=body, headers=ADMIN)
        assert made.status_code == 201, made.text
        issue_id = made.json()["id"]
        if assignee != 1:
            assigned = self.client.put(
                f"/issues/{issue_id}/assignee",
                json={"assignee_id": assignee},
                headers=ADMIN,
            )
            assert assigned.status_code == 200, assigned.text
        return issue_id

    def claim(self, user: int, issue_id: int, **payload):
        reviewed = self.client.get(f"/issues/{issue_id}", headers=as_user(user))
        assert reviewed.status_code == 200, reviewed.text
        return self.client.post(
            f"/issues/{issue_id}/claim",
            json=payload,
            headers={**as_user(user), "If-Match": reviewed.headers["ETag"]},
        )

    def lease(self, issue_id: int):
        return self.client.get(f"/issues/{issue_id}/lease", headers=ADMIN).json()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEASE_ROOT_CATALOG", None)  # the default instance
    app, db_file = make_app(tmp_path, "fences.db")
    with TestClient(app) as client:
        bootstrap(client)
        for email in ("s2@e.com", "s3@e.com", "s4@e.com"):
            add_user(client, email)
        yield World(client, db_file)


@pytest.fixture
def catalog_world(world, monkeypatch):
    monkeypatch.setattr(
        config, "LEASE_ROOT_CATALOG", coordination_roots.parse_catalog(CATALOG_JSON)
    )
    return world


def granted(response) -> dict:
    assert response.status_code == 201, response.text
    return response.json()


def refused(response, code: str) -> dict:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


# --- no catalog: lexical comparison under one implicit root -----------------------


def test_disjoint_paths_on_two_issues_are_both_granted_without_a_catalog(world):
    first, second = world.issue(2), world.issue(3)
    held = granted(world.claim(2, first, paths=["src/alpha/a.py"]))
    other = granted(world.claim(3, second, paths=["docs/beta/b.md"]))
    for lease, paths in ((held, ["src/alpha/a.py"]), (other, ["docs/beta/b.md"])):
        assert lease["declared_paths"] == paths
        assert lease["path_fence"] == LEXICAL
        assert lease["coordination_root"] is None
    assert world.lease(first)["active"] is True
    assert world.lease(second)["path_fence"] == LEXICAL


def test_the_same_path_is_refused_as_an_overlap_naming_it(world):
    first, second = world.issue(2, title="Holder"), world.issue(3)
    granted(world.claim(2, first, paths=["src/app/models.py"]))
    body = refused(world.claim(3, second, paths=["src/app/models.py"]), "fence_overlap")
    holder_key = world.client.get(f"/issues/{first}", headers=ADMIN).json()["key"]
    assert body["fence"] == {
        "basis": LEXICAL,
        "path": "src/app/models.py",
        "issue": {"id": first, "key": holder_key},
        "holder": {"id": 2, "name": "s2"},
        "holder_path": "src/app/models.py",
        # The newest event on the holder's issue: the cursor `next` waits from.
        "events_after": body["fence"]["events_after"],
    }
    assert isinstance(body["fence"]["events_after"], int)
    assert "src/app/models.py" in body["detail"]
    assert world.lease(second) is None


@pytest.mark.parametrize(
    ("held", "wanted"),
    [
        pytest.param("src/app", "src/app/models.py", id="file-under-held-directory"),
        pytest.param("src/app/models.py", "src/app", id="directory-over-held-file"),
    ],
)
def test_a_path_inside_or_around_a_held_path_is_an_overlap(world, held, wanted):
    first, second = world.issue(2), world.issue(3)
    granted(world.claim(2, first, paths=[held]))
    body = refused(
        world.claim(3, second, paths=["docs/unrelated.md", wanted]), "fence_overlap"
    )
    assert body["fence"]["path"] == wanted
    assert body["fence"]["holder_path"] == held


@pytest.mark.parametrize(("held", "wanted"), [("src/a", "src/ab"), ("src/ab", "src/a")])
def test_sibling_names_sharing_a_prefix_do_not_overlap(world, held, wanted):
    first, second = world.issue(2), world.issue(3)
    granted(world.claim(2, first, paths=[held]))
    assert granted(world.claim(3, second, paths=[wanted]))["path_fence"] == LEXICAL


def test_paths_are_normalized_before_they_are_compared(world):
    first, second, third = world.issue(2), world.issue(3), world.issue(4)
    held = granted(world.claim(2, first, paths=["./src//a/"]))
    assert held["declared_paths"] == ["src/a"]
    body = refused(world.claim(3, second, paths=["src/a//x.py/"]), "fence_overlap")
    assert body["fence"]["path"] == "src/a/x.py"
    assert body["fence"]["holder_path"] == "src/a"
    assert granted(world.claim(4, third, paths=["./src/b/"]))["declared_paths"] == [
        "src/b"
    ]


@pytest.mark.parametrize("path", ["../outside", "src/../../etc", "/etc/passwd"])
def test_an_escaping_or_absolute_path_is_invalid_input_not_compared(world, path):
    first, second = world.issue(2), world.issue(3)
    granted(world.claim(2, first, paths=["src"]))
    response = world.claim(3, second, paths=[path])
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert world.lease(second) is None


def test_an_overlap_is_refused_across_projects(world):
    elsewhere = world.make_project("Elsewhere", "ELS")
    first = world.issue(2, project_id=elsewhere)
    second, backlog = world.issue(3), world.issue(4, project_id=None)
    granted(world.claim(2, first, paths=["src/shared"]))
    refused(world.claim(3, second, paths=["src/shared/x.py"]), "fence_overlap")
    refused(world.claim(4, backlog, paths=["src"]), "fence_overlap")


def test_a_hidden_holder_is_not_named_and_the_two_refusals_differ_only_there(world):
    private = world.make_project("Private", "PRV", private=True)
    member = world.client.post(
        f"/projects/{private}/members", json={"user_id": 3}, headers=ADMIN
    )
    assert member.status_code == 201, member.text
    hidden = world.issue(1, project_id=private, title="Private work")
    granted(world.claim(1, hidden, paths=["src/shared"]))
    outsider_issue, member_issue = world.issue(2), world.issue(3)
    assert (
        world.client.get(f"/issues/{hidden}/lease", headers=as_user(2)).status_code
        == 404
    )
    assert (
        world.client.get(f"/issues/{hidden}/lease", headers=as_user(3)).status_code
        == 200
    )

    blind = refused(
        world.claim(2, outsider_issue, paths=["src/shared/x.py"]), "fence_overlap"
    )
    seen = refused(
        world.claim(3, member_issue, paths=["src/shared/x.py"]), "fence_overlap"
    )
    named = ("issue", "holder", "holder_path", "events_after")
    assert {key: blind["fence"][key] for key in named} == dict.fromkeys(named)
    assert seen["fence"]["issue"]["id"] == hidden
    assert seen["fence"]["holder"] == {"id": 1, "name": "Ann"}
    assert seen["fence"]["holder_path"] == "src/shared"
    strip = lambda body: {  # noqa: E731
        **body,
        "fence": {k: v for k, v in body["fence"].items() if k not in named},
    }
    assert strip(blind) == strip(seen)
    assert "Private work" not in json.dumps(blind) and "PRV" not in json.dumps(blind)


def test_a_renewal_keeps_its_own_paths_and_does_not_fence_itself(world):
    first, second = world.issue(2), world.issue(3)
    held = granted(world.claim(2, first, paths=["src/a"]))
    granted(world.claim(3, second, paths=["src/b"]))
    kept = granted(world.claim(2, first, generation=held["generation"]))
    assert kept["declared_paths"] == ["src/a"] and kept["path_fence"] == LEXICAL
    wider = granted(
        world.claim(2, first, generation=held["generation"], paths=["src/a", "src/a/x"])
    )
    assert wider["declared_paths"] == ["src/a", "src/a/x"]
    body = refused(
        world.claim(2, first, generation=held["generation"], paths=["src/b/y"]),
        "fence_overlap",
    )
    assert body["fence"]["issue"]["id"] == second
    assert world.lease(first)["declared_paths"] == ["src/a", "src/a/x"]


def test_an_expired_or_settled_lease_no_longer_fences(clock, world):
    first, second, third = world.issue(2), world.issue(3), world.issue(4)
    held = granted(world.claim(2, first, paths=["src/a"]))
    lapse_lease(clock, world.db_file, first)
    taken = granted(world.claim(3, second, paths=["src/a"]))
    completed = world.client.post(
        f"/issues/{second}/complete",
        json={"generation": taken["generation"]},
        headers=as_user(3),
    )
    assert completed.status_code == 200, completed.text
    granted(world.claim(4, third, paths=["src/a"]))
    assert held["generation"] != taken["generation"]


def test_a_claim_without_paths_is_unchanged_beside_a_fence(world):
    first, second = world.issue(2), world.issue(3)
    granted(world.claim(2, first, paths=["src"]))
    plain = granted(world.claim(3, second))
    assert plain["declared_paths"] == [] and plain["path_fence"] == "issue_only"
    assert set(plain) == {
        "issue_id",
        "holder_id",
        "holder_name",
        "claimed_at",
        "expires_at",
        "expires_in_seconds",
        "generation",
        "active",
        "declared_paths",
        "coordination_root",
        "path_fence",
        "open_claim_handoff",
        # The issue's ETag beside the lease, on every claim answer (A12).
        "issue_etag",
    }


def test_a_root_selector_on_an_instance_without_a_catalog_is_fence_unresolved(world):
    item = world.issue(2)
    body = refused(
        world.claim(2, item, paths=["src"], coordination_root=root()),
        "fence_unresolved",
    )
    assert "catalog" in body["detail"]
    assert world.lease(item) is None


# --- with a catalog: the catalog basis, as before, with the new codes -------------


def test_with_a_catalog_one_root_overlap_is_fence_overlap_on_the_same_root(
    catalog_world,
):
    world = catalog_world
    first, second, third = world.issue(2), world.issue(3), world.issue(4)
    held = granted(world.claim(2, first, paths=["docs"], coordination_root=root()))
    assert held["path_fence"] == "rooted"
    body = refused(
        world.claim(3, second, paths=["docs/x.md"], coordination_root=root()),
        "fence_overlap",
    )
    assert body["fence"]["basis"] == "same root"
    assert body["fence"]["path"] == "docs/x.md"
    assert body["fence"]["issue"]["id"] == first
    separate = granted(
        world.claim(4, third, paths=["docs/x.md"], coordination_root=root("two"))
    )
    assert separate["path_fence"] == "rooted"


def test_with_a_catalog_an_unrooted_fence_still_cannot_prove_separation(
    catalog_world,
):
    world = catalog_world
    first, second = world.issue(2), world.issue(3)
    held = granted(world.claim(2, first, paths=["docs"]))
    assert held["path_fence"] == "unresolved"
    body = refused(
        world.claim(3, second, paths=["unrelated"], coordination_root=root("two")),
        "fence_unresolved",
    )
    assert body["fence"]["basis"] == "unresolved"
    assert body["fence"]["path"] is None
    assert body["fence"]["issue"]["id"] == first
    stale = {**root(), "catalog_sha256": "f" * 64}
    refused(
        world.claim(3, second, paths=["x"], coordination_root=stale), "fence_unresolved"
    )
    assert world.lease(second) is None


# --- pickup takes paths ----------------------------------------------------------


def pickup(world, user, payload=None, **headers):
    return world.client.post(
        "/issues/claim-next",
        json={} if payload is None else payload,
        headers={**as_user(user), **headers},
    )


def delegate_ready(world, user, title="ready"):
    return world.issue(user, title=title)


def test_pickup_with_paths_applies_them_to_the_claim_it_makes(world):
    held = world.issue(2)
    granted(world.claim(2, held, paths=["src/a"]))
    ready = delegate_ready(world, 3)
    answer = pickup(world, 3, {"paths": ["./src/b/"]})
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert result["claimed"] is True and result["issue"]["id"] == ready
    assert result["lease"]["declared_paths"] == ["src/b"]
    assert result["lease"]["path_fence"] == LEXICAL
    assert world.lease(ready)["declared_paths"] == ["src/b"]


def test_pickup_whose_paths_overlap_claims_nothing_and_names_the_overlap(world):
    held = world.issue(2, title="Holder")
    granted(world.claim(2, held, paths=["src/a"]))
    first = delegate_ready(world, 3, "first")
    delegate_ready(world, 3, "second")
    answer = pickup(world, 3, {"paths": ["src/a/x.py"], "limit": 2})
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert result["claimed"] is False and result["reason"] == "fence_overlap"
    # The verdict does not depend on the candidate, so no second one is tried.
    assert [a["issue_id"] for a in result["attempts"]] == [first]
    assert result["fence"]["path"] == "src/a/x.py"
    assert result["fence"]["issue"]["id"] == held
    assert result["review_request"] is None
    assert world.lease(first) is None


def test_pickup_refuses_an_invalid_path_before_selection(world):
    ready = delegate_ready(world, 3)
    for paths in (["../x"], ["/abs"], "src", [1]):
        assert pickup(world, 3, {"paths": paths}).status_code == 422
    assert world.lease(ready) is None


def test_pickup_paths_are_part_of_what_a_key_is_matched_on(world):
    delegate_ready(world, 3)
    headers = {"Idempotency-Key": "pickup-with-paths"}
    first = pickup(world, 3, {"paths": ["src/a"]}, **headers)
    assert first.status_code == 200, first.text
    replay = pickup(world, 3, {"paths": ["src/a"]}, **headers)
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replay"] == "true"
    other = pickup(world, 3, {"paths": ["src/b"]}, **headers)
    assert other.status_code == 409 and other.json()["code"] == "idempotency_mismatch"


# --- the office reports the basis, never the catalog; the codes are registered --


def test_the_office_reports_the_fence_basis_only(world, monkeypatch):
    office = world.client.get("/office", headers=as_user(2)).json()
    assert office["fence"] == {"basis": LEXICAL}
    monkeypatch.setattr(
        config, "LEASE_ROOT_CATALOG", coordination_roots.parse_catalog(CATALOG_JSON)
    )
    desk = world.client.get("/desk", headers=as_user(2)).json()
    assert desk["office"]["fence"] == {"basis": "same root"}


@pytest.mark.parametrize("user", [1, 2], ids=["admin", "member"])
def test_no_read_discloses_the_catalog_digest_or_root_keys(
    catalog_world, monkeypatch, user
):
    """The basis shows whether a catalog is configured (D5); nothing returns its
    digest, root keys or contents. A selector comes from the operator."""
    import asyncio

    from athena.mcp import server as mcp_server
    from athena.mcp.client import AthenaClient

    world = catalog_world
    item = world.issue(user)
    granted(world.claim(user, item, paths=["docs"], coordination_root=root()))
    # A lease echoes the selector its own holder sent; drop it so only the
    # office and desk reads are left to say anything about the catalog.
    world.client.post(
        f"/issues/{item}/complete",
        json={"generation": world.lease(item)["generation"]},
        headers=as_user(user),
    )
    seat = TestClient(world.client.app, headers=as_user(user))
    server = mcp_server.build_server(
        AthenaClient(client=seat), profile=mcp_server.PROFILES["full"]
    )
    answers = {
        "GET /office": seat.get("/office").text,
        "GET /desk": seat.get("/desk").text,
    }
    for tool in ("my_desk", "my_office"):
        result = asyncio.run(server.call_tool(tool, {}))
        blocks = result[0] if isinstance(result, tuple) else result
        answers[tool] = "".join(getattr(block, "text", "") for block in blocks)
    for name, text in answers.items():
        assert '"basis"' in text, name
        assert "catalog_sha256" not in text and CATALOG_SHA not in text, name
        assert "root_key" not in text, name
        for key in ("one", "two"):
            assert f'"{key}"' not in text, (name, key)


def test_both_fence_codes_are_registered_and_only_an_overlap_names_a_next_call():
    for code in ("fence_overlap", "fence_unresolved"):
        assert refusal_codes.REFUSAL_CODES[code].statuses == (409,)
    assert refusal_codes.REFUSAL_CODES["fence_overlap"].next is not None
    # No read returns a selector, so no tool call resolves fence_unresolved: the
    # detail says to omit the selector, or to ask the operator for one.
    assert refusal_codes.REFUSAL_CODES["fence_unresolved"].next is None


def test_fence_unresolved_details_say_what_to_do(world, catalog_world):
    first, second = world.issue(2), world.issue(3)
    granted(world.claim(2, first, paths=["docs"]))
    mine = refused(
        world.claim(3, second, paths=["x"], coordination_root=root("two")),
        "fence_unresolved",
    )
    assert "the other lease's fence cannot be placed" in mine["detail"]
    third = world.issue(4)
    own = refused(world.claim(4, third, paths=["y"]), "fence_unresolved")
    assert own["detail"].endswith(
        "obtain a current reviewed selector from the operator"
    )
    stale = {**root(), "catalog_sha256": "f" * 64}
    stale_answer = refused(
        world.claim(4, third, paths=["y"], coordination_root=stale), "fence_unresolved"
    )
    assert "from the operator" in stale_answer["detail"]


# --- MCP reaches the same command; the selector's schema is closed ----------------


def _wire_call(server, name: str, arguments: dict):
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session

    async def call():
        async with create_connected_server_and_client_session(
            server._mcp_server  # noqa: SLF001 — the protocol server under test
        ) as session:
            return await session.call_tool(name, arguments)

    return asyncio.run(call())


def _seat_server(world, user: int):
    from athena.mcp import server as mcp_server
    from athena.mcp.client import AthenaClient

    seat = TestClient(world.client.app, headers=as_user(user))
    server = mcp_server.build_server(
        AthenaClient(client=seat),
        scopes=frozenset({"read", "issue:write"}),
        profile=mcp_server.PROFILES["seat"],
    )
    return seat, server


def test_over_mcp_an_overlap_is_the_rest_refusal_with_its_next_call(world):
    first, second = world.issue(2), world.issue(3)
    granted(world.claim(2, first, paths=["src/a"]))
    seat, server = _seat_server(world, 3)
    tag = seat.get(f"/issues/{second}").headers["ETag"]
    arguments = {"issue_id": second, "if_match": tag, "paths": ["src/a/b.py"]}
    result = _wire_call(server, "claim_issue", arguments)
    assert result.isError is True
    refusal = json.loads(result.content[0].text)
    rest = world.claim(3, second, paths=["src/a/b.py"]).json()
    assert {key: refusal[key] for key in rest} == rest
    assert refusal["status_code"] == 409
    assert refusal["next"] == refusal_codes.next_call("fence_overlap")
    assert refusal["next"]["tool"] == "wait_for_events"


def test_over_mcp_pickup_takes_paths(world):
    granted(world.claim(2, world.issue(2), paths=["src/a"]))
    ready = delegate_ready(world, 3)
    _seat, server = _seat_server(world, 3)
    fenced = _wire_call(server, "claim_next_ready", {"paths": ["src"]})
    assert fenced.isError is False
    answer = json.loads(fenced.content[0].text)
    assert answer["reason"] == "fence_overlap" and answer["fence"]["path"] == "src"
    taken = _wire_call(server, "claim_next_ready", {"paths": ["docs"]})
    answer = json.loads(taken.content[0].text)
    assert answer["claimed"] is True and answer["issue"]["id"] == ready
    assert answer["lease"]["declared_paths"] == ["docs"]


def test_the_coordination_root_schema_is_closed_on_mcp_and_rest(world):
    import asyncio

    from athena.agent_openapi import build_agent_openapi
    from athena.main import AGENT_TRANSPORT
    from athena.mcp import server as mcp_server

    _seat, server = _seat_server(world, 2)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schema = tools["claim_issue"].inputSchema
    selector = schema["$defs"]["CoordinationRootSelector"]
    assert selector["additionalProperties"] is False
    assert set(selector["properties"]) == {
        "catalog_sha256",
        "root_key",
        "relative_prefix",
    }
    assert set(selector["required"]) == {"catalog_sha256", "root_key"}
    spec = build_agent_openapi(world.client.app, AGENT_TRANSPORT)
    rest = spec["components"]["schemas"]["CoordinationRootSelector"]
    assert rest["additionalProperties"] is False
    assert mcp_server.TOOL_SCOPES["claim_issue"] == "issue:write"
