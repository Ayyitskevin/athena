"""Renewing a held lease needs the generation alone (A12).

WHY: a seat that held a lease and renewed it as the server's own hint said
("pass generation=... to extend it") was refused 428: a renewal also needed a
fresh issue ETag, so every renewal cost a re-read of the issue, and a renewal
after anyone touched the issue (a comment's projection, a peer's edit) failed
until it re-read. The ETag guards a claimant's decision to take work it has
reviewed; a renewal takes nothing new. The generation is the fence.

The contract pinned here:

- an active renewal by the current holder with its current generation may omit
  ``If-Match``; an ``If-Match`` it does send is still checked;
- a first claim still needs ``If-Match`` (428 without it);
- a stale or wrong generation is refused as before, and so is a renewal by any
  account but the holder, with or without the holder's generation;
- a renewal that also names ``paths`` or ``coordination_root`` follows the
  acquisition rule and still needs ``If-Match``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from support import (
    ADMIN,
    bearer,
    bootstrap,
    lapse_lease,
    lease_facts,
    make_app,
    mcp_server_for,
    onboard_agent,
)

from athena.core import activity, db

SEAT_SCOPES = ("read", "issue:write")
OTHER_GENERATION = "0" * 32


def _ok(response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


class World:
    def __init__(self, tmp_path):
        self.app, self.db_file = make_app(tmp_path)
        self.admin = TestClient(self.app)
        self.admin.__enter__()
        bootstrap(self.admin)
        self.sol = onboard_agent(
            self.admin, email="sol@e.com", name="Sol", scopes=SEAT_SCOPES
        )
        self.terra = onboard_agent(
            self.admin, email="terra@e.com", name="Terra", scopes=SEAT_SCOPES
        )

    def close(self) -> None:
        self.admin.__exit__(None, None, None)

    def issue(self, title: str = "work") -> int:
        """An issue both seats may claim: delegated to each."""
        made = _ok(
            self.admin.post("/issues", json={"title": title}, headers=ADMIN), 201
        )
        for seat in (self.sol, self.terra):
            _ok(
                self.admin.post(
                    f"/issues/{made['id']}/delegate",
                    json={"user_id": seat["user"]["id"]},
                    headers=ADMIN,
                ),
                201,
            )
        return made["id"]

    def etag(self, issue_id: int) -> str:
        return self.admin.get(f"/issues/{issue_id}", headers=ADMIN).headers["ETag"]

    def claim(self, seat, issue_id: int, *, if_match: str | None = None, **body):
        headers = bearer(seat["token"])
        if if_match is not None:
            headers["If-Match"] = if_match
        return self.admin.post(f"/issues/{issue_id}/claim", json=body, headers=headers)

    def lease(self, issue_id: int) -> dict:
        return _ok(self.admin.get(f"/issues/{issue_id}/lease", headers=ADMIN))

    def held(self, seat, issue_id: int) -> dict:
        return _ok(self.claim(seat, issue_id, if_match=self.etag(issue_id)), 201)


def test_the_holder_renews_with_the_generation_alone(tmp_path):
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        held = world.held(world.sol, issue_id)
        # The issue moves after the claim: the ETag the holder read is stale.
        _ok(
            world.admin.patch(
                f"/issues/{issue_id}", json={"title": "work, retitled"}, headers=ADMIN
            )
        )
        renewed = world.claim(
            world.sol, issue_id, generation=held["generation"], lease_seconds=3600
        )
        assert renewed.status_code == 201, renewed.text
        body = renewed.json()
        assert body["generation"] == held["generation"]
        assert body["holder_id"] == world.sol["user"]["id"] and body["active"]
        assert body["expires_at"] > held["expires_at"]
        conn = db.connect(world.db_file)
        try:
            renewals = activity.list_activity(conn, verb="lease_renewed", limit=10)
        finally:
            conn.close()
        assert [row["target_id"] for row in renewals] == [issue_id]
    finally:
        world.close()


def test_a_first_claim_still_needs_if_match(tmp_path):
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        first = world.claim(world.sol, issue_id)
        assert first.status_code == 428, first.text
        assert first.json()["code"] == "precondition_required"
        # A lapsed lease is a new possession, not a renewal: the same answer.
        held = world.held(world.sol, issue_id)
        lapse_lease(world.db_file, issue_id)
        again = world.claim(world.sol, issue_id)
        assert (
            again.status_code == 428 and again.json()["code"] == "precondition_required"
        )
        # And the lapsed generation renews nothing, with or without If-Match.
        stale = world.claim(world.sol, issue_id, generation=held["generation"])
        assert stale.status_code == 409
        assert stale.json()["code"] == "lease_generation_mismatch"
    finally:
        world.close()


def test_a_stale_or_wrong_generation_is_refused(tmp_path):
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        old = world.held(world.sol, issue_id)
        lapse_lease(world.db_file, issue_id)
        new = world.held(world.sol, issue_id)
        assert new["generation"] != old["generation"]
        for generation in (old["generation"], OTHER_GENERATION):
            refused = world.claim(world.sol, issue_id, generation=generation)
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "lease_generation_mismatch"
        assert world.lease(issue_id)["generation"] == new["generation"]
    finally:
        world.close()


def test_another_account_cannot_renew_with_the_holders_generation(tmp_path):
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        held = world.held(world.sol, issue_id)
        before = lease_facts(world.lease(issue_id))
        for if_match in (None, world.etag(issue_id)):
            refused = world.claim(
                world.terra, issue_id, if_match=if_match, generation=held["generation"]
            )
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "claim_held"
        # The admin is eligible to claim any issue, and is no holder either.
        admin = world.admin.post(
            f"/issues/{issue_id}/claim",
            json={"generation": held["generation"]},
            headers=ADMIN,
        )
        assert admin.status_code == 409 and admin.json()["code"] == "claim_held"
        assert lease_facts(world.lease(issue_id)) == before
    finally:
        world.close()


def test_a_renewal_that_names_paths_or_a_root_still_needs_if_match(tmp_path):
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        held = world.held(world.sol, issue_id)
        for extra in (
            {"paths": ["src/a.py"]},
            {"paths": []},
            {"coordination_root": {"catalog_sha256": "a" * 64, "root_key": "main"}},
        ):
            refused = world.claim(
                world.sol, issue_id, generation=held["generation"], **extra
            )
            assert refused.status_code == 428, (extra, refused.text)
            assert refused.json()["code"] == "precondition_required"
        with_paths = world.claim(
            world.sol,
            issue_id,
            if_match=world.etag(issue_id),
            generation=held["generation"],
            paths=["src/a.py"],
        )
        assert with_paths.status_code == 201, with_paths.text
        assert with_paths.json()["declared_paths"] == ["src/a.py"]
        # A plain renewal keeps the declared paths, as before.
        kept = world.claim(world.sol, issue_id, generation=held["generation"])
        assert kept.status_code == 201 and kept.json()["declared_paths"] == ["src/a.py"]
    finally:
        world.close()


def test_an_if_match_a_renewal_sends_is_still_checked(tmp_path):
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        stale_tag = world.etag(issue_id)
        held = world.held(world.sol, issue_id)
        _ok(
            world.admin.patch(
                f"/issues/{issue_id}", json={"title": "moved"}, headers=ADMIN
            )
        )
        refused = world.claim(
            world.sol, issue_id, if_match=stale_tag, generation=held["generation"]
        )
        assert refused.status_code == 412, refused.text
        assert refused.json()["code"] == "precondition_failed"
    finally:
        world.close()


def test_a_renewal_shows_the_issue_changed(tmp_path):
    """A holder that only renews sends no If-Match, so the answer carries the
    issue's current ETag beside the lease: a close, archive or edit since the
    holder read it shows without a second read. The lease read never carries it."""
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        held = world.held(world.sol, issue_id)
        assert held["issue_etag"] == world.etag(issue_id)
        quiet = _ok(
            world.claim(world.sol, issue_id, generation=held["generation"]), 201
        )
        assert quiet["issue_etag"] == held["issue_etag"]
        _ok(
            world.admin.patch(
                f"/issues/{issue_id}", json={"status": "done"}, headers=ADMIN
            )
        )
        moved = _ok(
            world.claim(world.sol, issue_id, generation=held["generation"]), 201
        )
        assert moved["issue_etag"] != held["issue_etag"]
        assert moved["issue_etag"] == world.etag(issue_id)
        assert "issue_etag" not in world.lease(issue_id)
    finally:
        world.close()


def test_following_the_hint_renews(tmp_path):
    """The 428 a holder meets names the generation; that alone renews."""
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        held = world.held(world.sol, issue_id)
        asked = world.claim(world.sol, issue_id)
        assert asked.status_code == 428, asked.text
        assert asked.json()["code"] == "lease_generation_required"
        detail = asked.json()["detail"]
        assert f"generation={held['generation']}" in detail
        assert "no If-Match" in detail
        renewed = world.claim(world.sol, issue_id, generation=held["generation"])
        assert renewed.status_code == 201, renewed.text
    finally:
        world.close()


def test_the_seat_tool_renews_without_if_match(tmp_path):
    world = World(tmp_path)
    try:
        issue_id = world.issue()
        held = world.held(world.sol, issue_id)
        server = mcp_server_for(world.app, world.sol, run_id="sol-run")

        async def run():
            async with create_connected_server_and_client_session(
                server._mcp_server  # noqa: SLF001 — the protocol server under test
            ) as session:
                return await session.call_tool(
                    "claim_issue",
                    {"issue_id": issue_id, "generation": held["generation"]},
                )

        result = asyncio.run(run())
        answer = json.loads(result.content[0].text)
        assert not result.isError, answer
        assert answer["generation"] == held["generation"] and answer["active"]
    finally:
        world.close()
