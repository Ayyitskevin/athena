"""Stopping an account names its leases and may release them (C8, D12).

FLEET-9: pausing an agent, revoking its tokens or offboarding it left every
lease it held, and the path fence each carries, live until expiry, and the
answer said nothing about them. A stopped account cannot release its own
leases, so the work sat fenced while the operator had to find each lease,
read its generation and release it by hand.

Each act now answers with the account's active leases. The operator may also
release them in the same act, as an explicit option (``release_leases``),
never as a default (ruling K1: nothing is swept). Each release goes through
the command an admin's release of one lease uses (U3), so each is recorded
per lease as ``lease_released``, naming the holder, and the act and its
releases commit or roll back together.

The second half: an admin's ``complete`` on a dead agent's EXPIRED lease was
refused ``claim_required``, so the admin had to claim the issue first. An
admin now settles it with the lease's generation. The possession already
lapsed, so its lapse is recorded first (once per generation, as every
observer records it), and the settlement is the same ``lease_released`` an
admin's live release writes: the trail says the clock ended the possession
and the admin cleared it, never that the admin ended live work.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from support import (
    ADMIN,
    AppClock,
    add_user,
    as_user,
    bearer,
    bootstrap,
    events,
    lapse_lease,
    make_app,
    mint_token,
    mcp_sessions,
    onboard_agent,
)

from athena.aegis import leases
from athena.core import agent_commands, db, packet_time


def _issue(client: TestClient, title: str, **fields) -> dict:
    response = client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def _claim(client: TestClient, agent: dict, issue_id: int, **body) -> dict:
    delegated = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    assert delegated.status_code in (200, 201), delegated.text
    read = client.get(f"/issues/{issue_id}", headers=bearer(agent))
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        json=body or None,
        headers={**bearer(agent), "If-Match": read.headers["ETag"]},
    )
    assert claimed.status_code in (200, 201), claimed.text
    return claimed.json()


def _lease_row(db_file, issue_id: int) -> dict | None:
    conn = db.connect(db_file)
    try:
        row = conn.execute(
            "SELECT holder_id, generation FROM issue_leases WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _told(db_file, event_id: int) -> set[int]:
    conn = db.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT user_id FROM notifications WHERE event_id = ?", (event_id,)
        ).fetchall()
    finally:
        conn.close()
    return {row["user_id"] for row in rows}


def _expected(issue: dict, lease: dict, *, released: bool) -> dict:
    return {
        "issue_id": issue["id"],
        "key": issue["key"],
        "title": issue["title"],
        "generation": lease["generation"],
        "expires_at": lease["expires_at"],
        "released": released,
    }


#: Each act, as REST performs it: (method, path suffix, JSON body or None).
_ACTS = {
    "pause": ("put", "paused", {"paused": True}),
    "revoke_tokens": ("delete", "tokens", None),
    "offboard": ("post", "offboard", None),
    "remove": ("post", "remove", None),
}


def _act(client: TestClient, act: str, user_id: int, **params):
    method, suffix, body = _ACTS[act]
    kwargs: dict = {"headers": ADMIN, "params": params}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(f"/users/{user_id}/{suffix}", **kwargs)


@pytest.mark.parametrize("act", sorted(_ACTS))
def test_each_act_names_the_active_leases_and_releases_nothing(clock, tmp_path, act):
    """The baseline said nothing. Without the option nothing is released: the
    lease and its fence stay exactly as they were, and no event is written."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        first = _issue(client, "first held")
        second = _issue(client, "second held")
        free = _issue(client, "not held")
        lease_one = _claim(client, agent, first["id"])
        lease_two = _claim(client, agent, second["id"])
        # A lapsed row is not an active lease, so the act does not name it. The
        # shortest lease there is: the clock passes it and leaves the others.
        _claim(client, agent, free["id"], lease_seconds=60)
        lapse_lease(clock, db_file, free["id"])

        answer = _act(client, act, agent["user"]["id"])
        assert answer.status_code == 200, answer.text

    named = sorted(answer.json()["active_leases"], key=lambda row: row["issue_id"])
    assert named == [
        _expected(first, lease_one, released=False),
        _expected(second, lease_two, released=False),
    ]
    assert _lease_row(db_file, first["id"])["generation"] == lease_one["generation"]
    assert _lease_row(db_file, second["id"])["generation"] == lease_two["generation"]
    assert events(db_file, "lease_released", "claim_completed") == []


@pytest.mark.parametrize("act", sorted(_ACTS))
def test_each_act_releases_every_lease_when_asked(tmp_path, act):
    """With ``release_leases=true`` each active lease is released through the
    U3 command: one ``lease_released`` per lease, by the admin, naming the
    holder and that lease's generation. Nothing is recorded as completed."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        admin = bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        bystander = onboard_agent(client, email="vega@e.com", name="Vega")
        first = _issue(client, "first held")
        second = _issue(client, "second held")
        other = _issue(client, "held by someone else")
        lease_one = _claim(client, agent, first["id"])
        lease_two = _claim(client, agent, second["id"])
        lease_other = _claim(client, bystander, other["id"])

        answer = _act(client, act, agent["user"]["id"], release_leases="true")
        assert answer.status_code == 200, answer.text

    named = sorted(answer.json()["active_leases"], key=lambda row: row["issue_id"])
    assert named == [
        _expected(first, lease_one, released=True),
        _expected(second, lease_two, released=True),
    ]
    assert _lease_row(db_file, first["id"]) is None
    assert _lease_row(db_file, second["id"]) is None
    # Another account's lease is not touched.
    assert _lease_row(db_file, other["id"])["generation"] == lease_other["generation"]
    released = sorted(
        events(db_file, "lease_released"), key=lambda row: row["target_id"]
    )
    assert [(row["actor_id"], row["target_id"], row["detail"]) for row in released] == [
        (
            admin["id"],
            first["id"],
            f"generation {lease_one['generation']}; "
            f"holder_id {agent['user']['id']}; holder Sol Holder",
        ),
        (
            admin["id"],
            second["id"],
            f"generation {lease_two['generation']}; "
            f"holder_id {agent['user']['id']}; holder Sol Holder",
        ),
    ]
    assert events(db_file, "claim_completed") == []


def test_the_released_holder_is_told(tmp_path):
    """The holder learns who ended its possession, as with U3: when it is
    resumed, its inbox says the operator released its lease."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        paused = onboard_agent(client, name="Paused One")
        issue = _issue(client, "paused holder")
        _claim(client, paused, issue["id"])
        answer = _act(client, "pause", paused["user"]["id"], release_leases="true")
        assert answer.is_success, answer.text

    (released,) = events(db_file, "lease_released")
    assert paused["user"]["id"] in _told(db_file, released["id"])


def test_a_repeated_pause_still_names_and_may_release(tmp_path):
    """A pause of a paused account records no second ``paused_user``, but the
    operator may still ask for the leases the first pause left."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue = _issue(client, "held through the first pause")
        lease = _claim(client, agent, issue["id"])
        assert _act(client, "pause", agent["user"]["id"]).is_success
        again = _act(client, "pause", agent["user"]["id"], release_leases="true")
        assert again.status_code == 200, again.text

    assert again.json()["active_leases"] == [_expected(issue, lease, released=True)]
    assert len(events(db_file, "paused_user")) == 1
    assert len(events(db_file, "lease_released")) == 1


def test_resume_names_leases_and_refuses_the_release_option(tmp_path):
    """Releasing belongs to stopping an account. Asked of a resume it is
    refused with a registered code, and nothing changes."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue = _issue(client, "held")
        lease = _claim(client, agent, issue["id"])
        refused = client.put(
            f"/users/{agent['user']['id']}/paused",
            json={"paused": False},
            params={"release_leases": "true"},
            headers=ADMIN,
        )
        resumed = client.put(
            f"/users/{agent['user']['id']}/paused",
            json={"paused": False},
            headers=ADMIN,
        )

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "validation_error"
    assert "release_leases" in refused.json()["detail"]
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["active_leases"] == [_expected(issue, lease, released=False)]
    assert _lease_row(db_file, issue["id"]) is not None


@pytest.mark.parametrize("act", ["pause", "revoke_tokens", "offboard"])
def test_an_admin_cannot_release_its_own_leases_this_way(tmp_path, act):
    """The release records another account's lease ended by an admin. An admin
    stopping itself would be recorded as completing its own work, so the
    option is refused on one's own account and nothing changes."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        add_user(client, "b@e.com", role="admin")
        issue = _issue(client, "held by the admin")
        read = client.get(f"/issues/{issue['id']}", headers=ADMIN)
        claimed = client.post(
            f"/issues/{issue['id']}/claim",
            headers={**ADMIN, "If-Match": read.headers["ETag"]},
        )
        assert claimed.status_code in (200, 201), claimed.text
        refused = _act(client, act, 1, release_leases="true")

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "validation_error"
    assert _lease_row(db_file, issue["id"]) is not None
    assert events(db_file, "paused_user", "offboarded_user", "lease_released") == []


def test_a_lease_on_an_issue_the_admin_cannot_see_is_neither_named_nor_released(
    tmp_path, monkeypatch
):
    """Every admin sees every project today, so this cannot happen through the
    API; the rule is held here so a narrower admin would not learn of the
    issue. A hidden lease reads as missing: not named, not released."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        shown = _issue(client, "shown")
        hidden = _issue(client, "hidden")
        lease_shown = _claim(client, agent, shown["id"])
        lease_hidden = _claim(client, agent, hidden["id"])
        from athena.core import access

        real = access.can_see_issue
        monkeypatch.setattr(
            access,
            "can_see_issue",
            lambda conn, actor, issue_id: (
                issue_id != hidden["id"] and real(conn, actor, issue_id)
            ),
        )
        answer = _act(client, "pause", agent["user"]["id"], release_leases="true")
        assert answer.status_code == 200, answer.text

    assert answer.json()["active_leases"] == [
        _expected(shown, lease_shown, released=True)
    ]
    assert _lease_row(db_file, hidden["id"])["generation"] == lease_hidden["generation"]
    assert [row["target_id"] for row in events(db_file, "lease_released")] == [
        shown["id"]
    ]


def test_a_failed_release_rolls_back_the_act(tmp_path, monkeypatch):
    """The act and every release are one transaction: if a release fails the
    pause is not left behind without it, and nothing is recorded."""
    from athena.core import account_leases

    app, db_file = make_app(tmp_path)

    def broken(conn, actor, lease):
        raise RuntimeError("release failed")

    with TestClient(app, raise_server_exceptions=False) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue = _issue(client, "held")
        _claim(client, agent, issue["id"])
        monkeypatch.setattr(account_leases, "_release", broken)
        failed = _act(client, "pause", agent["user"]["id"], release_leases="true")
        user = client.get(f"/users/{agent['user']['id']}", headers=ADMIN).json()

    assert failed.status_code == 500
    assert user["paused_at"] is None
    assert _lease_row(db_file, issue["id"]) is not None
    assert events(db_file, "paused_user", "lease_released") == []


def test_the_option_is_not_a_default_below_the_transport(tmp_path):
    """The command itself releases nothing unless asked."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue = _issue(client, "held")
        lease = _claim(client, agent, issue["id"])
    conn = db.connect(db_file)
    try:
        admin = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
        answer = agent_commands.revoke_agent_tokens(
            conn, actor=admin, target_user_id=agent["user"]["id"]
        )
    finally:
        conn.close()
    # The command answers stored timestamps; the transport shapes them.
    named = [
        {**row, "expires_at": packet_time.rfc3339(row["expires_at"])}
        for row in answer["active_leases"]
    ]
    assert named == [_expected(issue, lease, released=False)]
    assert _lease_row(db_file, issue["id"]) is not None


def test_mcp_tools_pass_the_option_and_return_the_leases(tmp_path):
    """Wherever an MCP tool performs the act, it takes the same option and
    returns the same leases."""
    from athena.mcp import server as mcp_server
    from athena.mcp.client import AthenaClient

    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        paused = onboard_agent(client, name="Paused")
        revoked = onboard_agent(client, email="r@e.com", name="Revoked")
        offboarded = onboard_agent(client, email="o@e.com", name="Offboarded")
        issues = {
            name: _issue(client, f"held by {name}")
            for name in ("paused", "revoked", "offboarded")
        }
        held = {
            "paused": _claim(client, paused, issues["paused"]["id"]),
            "revoked": _claim(client, revoked, issues["revoked"]["id"]),
            "offboarded": _claim(client, offboarded, issues["offboarded"]["id"]),
        }
        orchestrator = mint_token(client, scopes=["admin"], name="orch")
        athena = AthenaClient(
            client=TestClient(app, headers=bearer(orchestrator)), run_id="orch"
        )
        server = mcp_server.build_server(
            athena,
            scopes=mcp_server.probe_scopes(athena),
            profile=mcp_server.profile_from_env({mcp_server.PROFILE_ENV: "full"}),
        )

        async def play() -> dict:
            async with mcp_sessions(server) as (session,):
                answers = {}
                for tool, name, user in (
                    ("pause_agent", "paused", paused),
                    ("revoke_agent_tokens", "revoked", revoked),
                    ("offboard_agent", "offboarded", offboarded),
                ):
                    result = await session.call_tool(
                        tool,
                        {
                            "user_id": user["user"]["id"],
                            "release_leases": name != "revoked",
                        },
                    )
                    assert not result.isError, result.content
                    answers[name] = json.loads(result.content[0].text)
                return answers

        answers = asyncio.run(play())

    for name, released in (("paused", True), ("revoked", False), ("offboarded", True)):
        assert answers[name]["active_leases"] == [
            _expected(issues[name], held[name], released=released)
        ]
    assert _lease_row(db_file, issues["revoked"]["id"]) is not None
    assert _lease_row(db_file, issues["paused"]["id"]) is None


# --- an admin settles an expired lease without claiming it ---------------------


def _dead_agent_with_lapsed_lease(
    clock: AppClock, client: TestClient, db_file, *, assignee_id: int = 1
) -> tuple[dict, dict]:
    agent = onboard_agent(client, name="Dead Agent")
    issue = _issue(client, "left behind", assignee_id=assignee_id)
    lease = _claim(client, agent, issue["id"])
    lapse_lease(clock, db_file, issue["id"])
    return issue, lease


def test_an_admin_settles_a_dead_agents_expired_lease_without_claiming(clock, tmp_path):
    """The baseline answered ``409 claim_required``. Now one ``complete`` with
    the lease's generation settles it: the lapse is recorded (the clock ended
    the possession) and the settlement is ``lease_released`` (the admin cleared
    it). No claim, no completion."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        admin = bootstrap(client)
        issue, lease = _dead_agent_with_lapsed_lease(clock, client, db_file)
        settled = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": lease["generation"]},
            headers=ADMIN,
        )
        assert settled.status_code == 200, settled.text

    body = settled.json()
    assert body["released"] is True and body["lapsed"] is True
    assert _lease_row(db_file, issue["id"]) is None
    trail = [
        row
        for row in events(db_file, "lease_lapsed", "lease_released", "claim_completed")
        if row["target_id"] == issue["id"]
    ]
    # Newest first: the lapse, then the release that settled it.
    assert [row["verb"] for row in trail] == ["lease_released", "lease_lapsed"]
    assert all(row["actor_id"] == admin["id"] for row in trail)
    assert all(
        row["detail"].startswith(f"generation {lease['generation']};") for row in trail
    )
    assert events(db_file, "claimed")[0]["actor_id"] != admin["id"]


def test_a_lapse_already_recorded_is_not_recorded_twice(clock, tmp_path):
    """One possession, one lapse: an observer that recorded the lapse first
    leaves the settlement to write only the release."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue, lease = _dead_agent_with_lapsed_lease(clock, client, db_file)
        # The assignee's event feed observes the lapse (C4).
        assert client.get("/events", headers=ADMIN).status_code == 200
        assert len(events(db_file, "lease_lapsed")) == 1
        settled = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": lease["generation"]},
            headers=ADMIN,
        )
        assert settled.status_code == 200, settled.text

    assert len(events(db_file, "lease_lapsed")) == 1
    assert len(events(db_file, "lease_released")) == 1


def test_settling_another_accounts_expired_lease_is_fenced_by_generation(
    clock, tmp_path
):
    """Only the holder's own lapsed row may omit the generation. An admin names
    the possession it settles; a missing one is not handed out, and a stale
    one is refused. Each refusal records nothing."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue, lease = _dead_agent_with_lapsed_lease(clock, client, db_file)
        missing = client.post(f"/issues/{issue['id']}/complete", headers=ADMIN)
        stale = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": "0" * leases.GENERATION_HEX_CHARS},
            headers=ADMIN,
        )

    assert missing.status_code == 428, missing.text
    assert missing.json()["code"] == "lease_generation_required"
    assert missing.json().get("generation") is None
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "lease_generation_mismatch"
    assert _lease_row(db_file, issue["id"])["generation"] == lease["generation"]
    assert events(db_file, "lease_lapsed", "lease_released") == []


def test_a_member_still_cannot_settle_another_accounts_expired_lease(clock, tmp_path):
    """The lever is an admin's. A member eligible on the issue is refused as
    before and records nothing."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com")
        # The member is the assignee, so eligible to claim the issue itself.
        issue, lease = _dead_agent_with_lapsed_lease(
            clock, client, db_file, assignee_id=member["id"]
        )
        refused = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": lease["generation"]},
            headers=as_user(member["id"]),
        )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "claim_required"
    assert _lease_row(db_file, issue["id"]) is not None
    assert events(db_file, "lease_released") == []
