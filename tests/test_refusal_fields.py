"""A refusal that names a holder, an expiry, a generation or a limit carries each
as a field (F11, FLEET-15).

WHY: an agent branches on fields, never on prose. `claim_held` named the holder and
the expiry only inside `detail` ("issue is claimed by Ada until 2026-..."), a
renewal without its generation named the generation only in prose, and the limit
refusals named their bound in a header or not at all. An agent that wanted to know
WHO holds the work, UNTIL WHEN, or HOW MUCH it may send had to parse English.

These tests hold, for each such refusal, that the body carries the fact as a field
equal to the authoritative value (the lease, the limiter's decision, the configured
bound), beside the unchanged `detail` and `code`; that MCP hands the same fields to
the agent; and that a field naming another account's lease is filled only for a
caller who could read that lease, as `fence_overlap` already does.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, add_user, as_user, bearer, bootstrap, make_app, onboard_agent

from athena import config
from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError
from athena.workflows import playbook_commands


class Pair:
    """An operator (user 1) and two seats, Ada and Bo, both delegated one issue."""

    def __init__(self, app, client: TestClient):
        self.app = app
        self.client = client
        bootstrap(client)
        self.ada = onboard_agent(client, email="ada@e.com", name="Ada")
        self.bo = onboard_agent(client, email="bo@e.com", name="Bo")
        made = client.post("/projects", json={"name": "W", "key": "WRK"}, headers=ADMIN)
        assert made.status_code == 201, made.text
        self.project_id = made.json()["id"]
        made = client.post(
            "/issues",
            json={"title": "Shared", "project_id": self.project_id},
            headers=ADMIN,
        )
        assert made.status_code == 201, made.text
        self.issue_id = made.json()["id"]
        for seat in (self.ada, self.bo):
            handed = client.post(
                f"/issues/{self.issue_id}/delegate",
                json={"user_id": seat["user"]["id"]},
                headers=ADMIN,
            )
            assert handed.status_code == 201, handed.text

    def etag(self, headers: dict[str, str]) -> str:
        return self.client.get(f"/issues/{self.issue_id}", headers=headers).headers[
            "ETag"
        ]

    def claim(self, seat: dict, **body: Any):
        headers = bearer(seat)
        return self.client.post(
            f"/issues/{self.issue_id}/claim",
            json=body,
            headers={**headers, "If-Match": self.etag(headers)},
        )

    def lease(self) -> dict:
        return self.client.get(f"/issues/{self.issue_id}/lease", headers=ADMIN).json()


@pytest.fixture
def pair(tmp_path):
    app, _db_file = make_app(tmp_path)
    with TestClient(app) as client:
        yield Pair(app, client)


def _holder_fields(body: dict, lease: dict) -> None:
    assert body["code"] == "claim_held"
    assert body["holder"] == {"id": lease["holder_id"], "name": lease["holder_name"]}
    assert body["expires_at"] == lease["expires_at"]
    # The prose is unchanged and names the same holder.
    assert body["detail"].startswith(f"issue is claimed by {lease['holder_name']}")
    # A rival is never handed the holder's generation.
    assert lease["generation"] not in str(body)


# --- claim_held: the holder and the expiry ---------------------------------------


def test_claim_held_on_claim_carries_the_holder_and_the_expiry(pair):
    assert pair.claim(pair.ada).status_code == 201
    refused = pair.claim(pair.bo)
    assert refused.status_code == 409, refused.text
    body = refused.json()
    _holder_fields(body, pair.lease())
    assert body["detail"] == (
        f"issue is claimed by Ada until {pair.lease()['expires_at']}"
    )
    # The fields name another account's lease, so they reach only a caller who
    # could read that lease: one who cannot see the issue meets 404 before any
    # holder is named, as it does for the lease read itself.
    private = pair.client.put(
        f"/projects/{pair.project_id}/visibility",
        json={"visibility": "private"},
        headers=ADMIN,
    )
    assert private.status_code == 200, private.text
    outsider = add_user(pair.client, "o@e.com", name="Out")
    hidden = pair.client.post(
        f"/issues/{pair.issue_id}/claim",
        json={},
        headers={**as_user(outsider["id"]), "If-Match": '"x"'},
    )
    assert hidden.status_code == 404, hidden.text
    assert "holder" not in hidden.json() and "Ada" not in hidden.text


def test_claim_held_on_yield_resume_and_complete_carries_them_too(pair):
    claimed = pair.claim(pair.ada)
    assert claimed.status_code == 201
    lease = pair.lease()
    headers = bearer(pair.bo)
    attempts = {
        "yield": pair.client.post(
            f"/issues/{pair.issue_id}/yield",
            json={
                "generation": lease["generation"],
                "reason": "blocked",
                "attempted_work": "w",
                "evidence": ["e"],
                "blocking_question": "q?",
                "resume_instructions": "r",
            },
            headers=headers,
        ),
        "resume": pair.client.post(
            f"/issues/{pair.issue_id}/claim-handoffs/{'a' * 32}/resume",
            json={"generation": lease["generation"]},
            headers=headers,
        ),
        "complete": pair.client.post(
            f"/issues/{pair.issue_id}/complete",
            json={"generation": lease["generation"]},
            headers=headers,
        ),
    }
    for name, refused in attempts.items():
        assert refused.status_code == 409, (name, refused.text)
        _holder_fields(refused.json(), lease)


def test_mcp_hands_the_agent_the_same_holder_fields(pair):
    assert pair.claim(pair.ada).status_code == 201
    api = AthenaClient(client=TestClient(pair.app, headers=bearer(pair.bo)))
    with pytest.raises(AthenaError) as raised:
        api.claim_issue(pair.issue_id, if_match=pair.etag(bearer(pair.bo)))
    refusal = raised.value.refusal()
    lease = pair.lease()
    assert refusal["holder"] == {"id": lease["holder_id"], "name": "Ada"}
    assert refusal["expires_at"] == lease["expires_at"]
    assert refusal["status_code"] == 409


# --- lease_generation_required: the generation, to its holder alone ----------------


def test_a_renewal_without_its_generation_names_it_as_a_field(pair):
    assert pair.claim(pair.ada).status_code == 201
    lease = pair.lease()
    refused = pair.claim(pair.ada)
    assert refused.status_code == 428, refused.text
    body = refused.json()
    assert body["code"] == "lease_generation_required"
    assert body["generation"] == lease["generation"]
    assert f"generation={lease['generation']}" in body["detail"]


def test_every_holder_site_names_the_generation_and_no_other_caller_is_told(pair):
    """The registry says a holder's lease_generation_required names its
    generation. Every write the holder makes without one says so as a field:
    complete, yield, handoff resume and decline, as well as a repeated claim.
    An admin releasing the holder's lease without a generation is refused the
    same way and is not told it."""
    assert pair.claim(pair.ada).status_code == 201
    lease = pair.lease()
    headers = bearer(pair.ada)
    path = f"/issues/{pair.issue_id}"
    handoff = {
        "reason": "blocked",
        "attempted_work": "a",
        "evidence": ["e"],
        "blocking_question": "b",
        "resume_instructions": "c",
    }
    holder_sites = {
        "complete": pair.client.post(f"{path}/complete", json={}, headers=headers),
        "yield": pair.client.post(f"{path}/yield", json=handoff, headers=headers),
        "resume": pair.client.post(
            f"{path}/claim-handoffs/{'a' * 32}/resume", json={}, headers=headers
        ),
        "decline": pair.client.post(f"{path}/decline", json={}, headers=headers),
    }
    for site, refused in holder_sites.items():
        assert refused.status_code == 428, (site, refused.text)
        body = refused.json()
        assert body["code"] == "lease_generation_required", (site, body)
        assert body["generation"] == lease["generation"], (site, body)
    # Nothing above released or changed the lease.
    assert pair.lease()["generation"] == lease["generation"]
    admin = pair.client.post(f"{path}/complete", json={}, headers=ADMIN)
    assert admin.status_code == 428, admin.text
    assert admin.json()["code"] == "lease_generation_required"
    assert "generation" not in admin.json()
    assert lease["generation"] not in admin.text


# --- the limits ----------------------------------------------------------------------


def test_a_token_rate_limit_refusal_carries_the_limit(tmp_path):
    app = create_app(tmp_path / "limits.db", token_rate_limit_per_minute=1)
    with TestClient(app) as client:
        bootstrap(client)
        reader = onboard_agent(client, email="r@e.com", name="R", scopes=("read",))
        assert client.get("/issues", headers=bearer(reader)).status_code == 200
        limited = client.get("/issues", headers=bearer(reader))
        assert limited.status_code == 429, limited.text
        body = limited.json()
        assert body["code"] == "rate_limited"
        assert body["detail"] == "token rate limit exceeded"
        assert body["rate_limit"] == {
            "limit": 1,
            "remaining": 0,
            "retry_after_seconds": int(limited.headers["Retry-After"]),
        }
        assert int(limited.headers["X-RateLimit-Limit"]) == 1


def test_an_anonymous_rate_limit_refusal_carries_the_limit(tmp_path):
    app = create_app(tmp_path / "anon.db", anon_rate_limit_per_minute=1)
    with TestClient(app) as client:
        bootstrap(client)
        bad = {"Authorization": "Bearer not-a-token"}
        # The bootstrap request may already have spent the one anonymous unit.
        answers = [client.get("/issues", headers=bad) for _ in range(2)]
        limited = answers[-1]
        assert limited.status_code == 429, limited.text
        body = limited.json()
        assert body["code"] == "rate_limited"
        assert body["rate_limit"] == {
            "limit": 1,
            "remaining": 0,
            "retry_after_seconds": int(limited.headers["Retry-After"]),
        }


def test_a_body_over_the_limit_carries_the_limit(tmp_path):
    app = create_app(tmp_path / "body.db", max_request_body_bytes=64)
    with TestClient(app) as client:
        bootstrap(client)
        large = client.post("/issues", json={"title": "x" * 200}, headers=ADMIN)
        assert large.status_code == 413, large.text
        assert large.json() == {
            "detail": "request body too large",
            "code": "payload_too_large",
            "limit_bytes": 64,
        }


def test_an_attachment_over_the_limit_carries_the_limit(pair, monkeypatch):
    monkeypatch.setattr(config, "ATTACH_MAX_BYTES", 8)
    refused = pair.client.post(
        f"/issues/{pair.issue_id}/attachments",
        files={"file": ("big.txt", b"x" * 64, "text/plain")},
        headers=ADMIN,
    )
    assert refused.status_code == 413, refused.text
    assert refused.json() == {
        "detail": "attachment too large",
        "code": "payload_too_large",
        "limit_bytes": 8,
    }


def test_a_full_worker_registry_carries_its_limit(pair, monkeypatch):
    monkeypatch.setattr(config, "WORKER_MAX_PER_AGENT", 1)
    headers = bearer(pair.ada)
    first = pair.client.put(
        "/workers/heartbeat", json={"worker_key": "one"}, headers=headers
    )
    assert first.status_code == 200, first.text
    full = pair.client.put(
        "/workers/heartbeat", json={"worker_key": "two"}, headers=headers
    )
    assert full.status_code == 429, full.text
    body = full.json()
    assert body["code"] == "capacity_reached"
    assert body["limit"] == 1


def test_a_full_run_check_in_table_carries_its_limit(pair, monkeypatch):
    monkeypatch.setattr(config, "AGENT_RUN_MAX_CHECKINS_PER_AGENT", 1)
    headers = bearer(pair.ada)
    first = pair.client.put(
        "/agent-runs/heartbeat", json={"run_id": "run-1"}, headers=headers
    )
    assert first.status_code == 200, first.text
    full = pair.client.put(
        "/agent-runs/heartbeat", json={"run_id": "run-2"}, headers=headers
    )
    assert full.status_code == 409, full.text
    body = full.json()
    assert body["code"] == "capacity_reached"
    assert body["limit"] == 1


def test_a_playbook_over_its_step_limit_carries_the_limit_and_the_count(pair):
    space = pair.client.post(
        "/spaces", json={"key": "OPS", "name": "Ops"}, headers=ADMIN
    ).json()
    steps = playbook_commands.MAX_ITEMS + 1
    page = pair.client.post(
        f"/spaces/{space['id']}/pages",
        json={
            "title": "Deploy",
            "body": "".join(f"- [ ] s{n}\n" for n in range(steps)),
        },
        headers=ADMIN,
    ).json()
    label = pair.client.post(
        "/labels", json={"name": playbook_commands.PLAYBOOK_LABEL}, headers=ADMIN
    ).json()
    attached = pair.client.post(
        f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=ADMIN
    )
    assert attached.status_code in (200, 201), attached.text
    refused = pair.client.post(
        f"/pages/{page['id']}/start-playbook", json={}, headers=ADMIN
    )
    assert refused.status_code == 429, refused.text
    body = refused.json()
    assert body["code"] == "capacity_reached"
    assert body["limit"] == playbook_commands.MAX_ITEMS
    assert body["requested"] == steps
