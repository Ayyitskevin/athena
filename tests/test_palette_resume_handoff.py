"""Resuming a claim handoff from the cockpit's palette (decision 8, T2).

The palette claimed an issue and yielded it with a handoff, but could not
resume one. An operator who took over an issue a seat had handed off could not
acknowledge the handoff in the browser, and their own yield with a handoff was
then refused until the handoff was resumed over REST. The palette now offers
"Resume handoff" beside yield and complete, to the exact current leaseholder
only, while a handoff is open, and runs it through the same command REST calls,
``lease_commands.resume_claim_handoff``, behind the cockpit's CSRF check.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from support import (
    ADMIN,
    add_user,
    bearer,
    bootstrap,
    events,
    login,
    make_app,
    onboard_agent,
)


def _world(client: TestClient) -> dict:
    """The operator (1, signed in), a member Mia, and the seat Sol, who claims
    the issue over REST and yields it with a handoff."""
    bootstrap(client, email="admin@e.com", name="Admin")
    mia = add_user(client, "mia@e.com", name="Mia")
    sol = onboard_agent(client)
    issue = client.post("/issues", json={"title": "handed off"}, headers=ADMIN).json()
    delegated = client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": sol["user"]["id"]},
        headers=ADMIN,
    )
    assert delegated.status_code == 201, delegated.text
    as_sol = bearer(sol)
    etag = client.get(f"/issues/{issue['id']}", headers=as_sol).headers["ETag"]
    held = client.post(
        f"/issues/{issue['id']}/claim",
        json={},
        headers={**as_sol, "If-Match": etag},
    )
    assert held.status_code == 201, held.text
    handoff = client.post(
        f"/issues/{issue['id']}/yield",
        json={
            "generation": held.json()["generation"],
            "reason": "needs_input",
            "attempted_work": "read the brief",
            "evidence": ["the brief names two owners"],
            "blocking_question": "which owner?",
            "resume_instructions": "pick one owner, then continue",
        },
        headers=as_sol,
    )
    assert handoff.status_code == 201, handoff.text
    return {"issue": issue, "mia": mia, "handoff": handoff.json()}


def _actions(client: TestClient, issue_id: int) -> dict:
    answer = client.get(f"/aegis/palette/actions?issue_ref={issue_id}")
    assert answer.status_code == 200, answer.text
    return {action["id"]: action for action in answer.json()["actions"]}


def _field(action: dict, name: str) -> str:
    return next(f["value"] for f in action["fields"] if f["name"] == name)


def _palette_claim(client: TestClient, issue_id: int) -> str:
    etag = _field(_actions(client, issue_id)["claim"], "etag")
    claimed = client.post(
        f"/aegis/palette/issues/{issue_id}/claim", data={"etag": etag}
    )
    assert claimed.status_code == 201, claimed.text
    return claimed.json()["generation"]


def test_the_holder_resumes_an_open_handoff_from_the_palette(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        issue_id = world["issue"]["id"]
        login(client, "admin@e.com")

        # Not holding the claim: nothing to resume, only the claim.
        before = _actions(client, issue_id)
        assert "resume" not in before and "claim" in before

        generation = _palette_claim(client, issue_id)
        actions = _actions(client, issue_id)
        assert {"yield", "complete", "resume"} <= set(actions)
        resume = actions["resume"]
        assert resume["endpoint"] == f"/aegis/palette/issues/{issue_id}/resume"
        assert _field(resume, "handoff_token") == world["handoff"]["handoff_token"]
        assert _field(resume, "generation") == generation

        # The operator's own yield is refused while the handoff stays open.
        early = client.post(
            f"/aegis/palette/issues/{issue_id}/yield",
            data={
                "generation": generation,
                "reason": "blocked",
                "attempted_work": "looked",
                "blocking_question": "who?",
                "resume_instructions": "ask",
            },
        )
        assert early.status_code == 409, early.text

        resumed = client.post(
            resume["endpoint"],
            data={
                "handoff_token": _field(resume, "handoff_token"),
                "generation": _field(resume, "generation"),
                "resume_note": "owner is Mia",
            },
        )
        assert resumed.status_code == 200, resumed.text
        body = resumed.json()
        assert body["issue_id"] == issue_id
        assert body["handoff_token"] == world["handoff"]["handoff_token"]
        assert body["state"] == "resumed"

        # The handoff is closed for REST as well, and the action is gone.
        read = client.get(f"/issues/{issue_id}/lease", headers=ADMIN).json()
        assert read["open_claim_handoff"] is None
        assert "resume" not in _actions(client, issue_id)

        # Resumed, the operator may hand the issue off in turn.
        later = client.post(
            f"/aegis/palette/issues/{issue_id}/yield",
            data={
                "generation": generation,
                "reason": "blocked",
                "attempted_work": "picked the owner",
                "blocking_question": "is Mia free?",
                "resume_instructions": "ask Mia",
            },
        )
        assert later.status_code == 200, later.text
    resumes = events(db_file, "claim_handoff_resumed")
    assert len(resumes) == 1
    assert resumes[0]["actor_id"] == 1


def test_only_the_exact_leaseholder_is_offered_or_may_run_resume(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        issue_id = world["issue"]["id"]
        token = world["handoff"]["handoff_token"]
        login(client, "admin@e.com")
        generation = _palette_claim(client, issue_id)

        # Mia can see the issue, and is not the holder: no action, and the
        # command refuses her post as the claim of another.
        login(client, "mia@e.com")
        assert "resume" not in _actions(client, issue_id)
        refused = client.post(
            f"/aegis/palette/issues/{issue_id}/resume",
            data={"handoff_token": token, "generation": generation},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "claim_held"

        # The holder with a stale generation is fenced by the command.
        login(client, "admin@e.com")
        stale = client.post(
            f"/aegis/palette/issues/{issue_id}/resume",
            data={"handoff_token": token, "generation": "0" * 32},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "lease_generation_mismatch"
    assert events(db_file, "claim_handoff_resumed") == []


def test_resume_is_csrf_protected_and_needs_a_session(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        issue_id = world["issue"]["id"]
        token = world["handoff"]["handoff_token"]
        login(client, "admin@e.com")
        generation = _palette_claim(client, issue_id)
        form = {"handoff_token": token, "generation": generation}

        forged = client.post(
            f"/aegis/palette/issues/{issue_id}/resume",
            data=form,
            headers={"X-CSRF-Token": "forged"},
        )
        assert forged.status_code == 403, forged.text

    with TestClient(app) as anonymous:
        signed_out = anonymous.post(
            f"/aegis/palette/issues/{issue_id}/resume", data=form
        )
        assert signed_out.status_code in (401, 403), signed_out.text
    assert events(db_file, "claim_handoff_resumed") == []
