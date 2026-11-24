"""An issue's assignment or creator changing fences that issue's receipts only (F6).

WHY: one workspace-wide authorization revision moved on any change to any issue's
assignee, project or creator, so in a fleet, where the conductor assigns work all
day, an unrelated assignment voided every stored receipt: its key answered 409
``idempotency_authorization_changed`` and its marker never expired (PERF-1: 30%
of receipts in a sustained run). Decision D17 lets the fence narrow for an
issue's assignment and creator IF a replay still never returns what its caller
could no longer obtain. The argument is in ``core/idempotency.py``; this file is
its check:

* the HTTP scenarios: an unrelated assignment leaves a receipt replayable; an
  assignment on the issue still fences it, and the refusal names the issue;
  role, membership, visibility, pause and placement still fence everything; a
  receipt that cannot be scoped is fenced by any assignment; a fenced marker
  expires when its receipt would have and the key is then a key never seen;
* a scripted fleet run whose fenced receipts are exactly those of reassigned
  issues;
* a property over generated histories (issues in public and private projects,
  accounts of every role and membership, writes and acts interleaved at random):
  after every act, every receipt's replay is either refused or the stored
  response, and then the same request made now by the same caller is allowed and
  nothing the stored response exposes is hidden from that caller;
* the same property against fences deliberately narrowed too far, which it must
  catch.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
import json
import random
import sqlite3
from typing import Any

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, add_user, as_user, bearer, bootstrap, make_app, onboard_agent

from athena.aegis import (
    comment_commands,
    contributors,
    dependencies,
    issue_commands,
    issue_etags,
    issues,
    lease_commands,
    leases,
    projects,
    statuses,
)
from athena.core import (
    access,
    approvals,
    budgets,
    db,
    identity,
    idempotency,
    tokens,
    users,
)

REPLAY = "idempotent-replay"


def _keyed(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


def _issue(client: TestClient, title: str, **fields: Any) -> int:
    made = client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    assert made.status_code == 201, made.text
    return int(made.json()["id"])


def _delegate(client: TestClient, issue_id: int, user_id: int) -> None:
    handed = client.post(
        f"/issues/{issue_id}/delegate", json={"user_id": user_id}, headers=ADMIN
    )
    assert handed.status_code == 201, handed.text


def _assign(client: TestClient, issue_id: int, user_id: int | None) -> None:
    moved = client.put(
        f"/issues/{issue_id}/assignee", json={"assignee_id": user_id}, headers=ADMIN
    )
    assert moved.status_code == 200, moved.text


def _row(db_file, key: str) -> dict[str, Any]:
    with closing(db.connect(db_file)) as conn:
        row = conn.execute(
            "SELECT state, failure_code, authority_scope, scope_issue_id, "
            "response_body, expires_at FROM idempotency_keys "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    assert row is not None, key
    return dict(row)


# --- the HTTP scenarios ----------------------------------------------------------


def test_an_unrelated_assignment_leaves_a_receipt_replayable(tmp_path):
    """PERF-1's case: a write on A, then B assigned. A's key still replays."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        a, b = _issue(client, "A"), _issue(client, "B")
        _delegate(client, a, sol["user"]["id"])
        keyed = _keyed(bearer(sol), "write-on-a")
        # A priority, not a title: a bearer caller sends a title or body only
        # with If-Match (F10), and this test is about the fence.
        write = {"priority": "high"}
        first = client.patch(f"/issues/{a}", json=write, headers=keyed)
        assert first.status_code == 200, first.text

        _assign(client, b, 1)
        replay = client.patch(f"/issues/{a}", json=write, headers=keyed)

    assert replay.status_code == 200, replay.text
    assert replay.headers.get(REPLAY) == "true"
    assert replay.json() == first.json()
    row = _row(db_file, "write-on-a")
    assert (row["state"], row["authority_scope"], row["scope_issue_id"]) == (
        "completed",
        "issue",
        a,
    )


def test_an_assignment_on_the_issue_fences_its_receipt_and_names_it(tmp_path):
    """The old assignee loses the issue: its stored edit must not replay, and the
    refusal names the issue to read to learn whether the edit applied."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com")
        x = _issue(client, "X")
        _assign(client, x, member["id"])
        keyed = _keyed(as_user(member["id"]), "edit-x")
        first = client.patch(f"/issues/{x}", json={"body": "mine"}, headers=keyed)
        assert first.status_code == 200, first.text

        _assign(client, x, 1)
        fresh = client.patch(f"/issues/{x}", json={"body": "mine"}, headers=as_user(2))
        replay = client.patch(f"/issues/{x}", json={"body": "mine"}, headers=keyed)

    assert fresh.status_code == 403  # what the caller could obtain now
    assert replay.status_code == 409
    assert replay.json() == {
        "detail": (
            "Authorization changed after this Idempotency-Key was claimed; "
            "its stored response cannot be replayed"
        ),
        "code": "idempotency_authorization_changed",
        "issue_id": x,
    }
    assert replay.headers.get(REPLAY) is None
    row = _row(db_file, "edit-x")
    assert row["state"] == "indeterminate"
    assert row["failure_code"] == "authorization_changed"
    assert row["response_body"] is None
    assert row["expires_at"] is not None


def test_a_create_receipt_survives_assignments_even_of_what_it_created(tmp_path):
    """POST /issues reads no existing issue's assignee or creator, and the caller
    is the creator of what it makes: no assignment fences its receipt, not even
    the one the create makes itself."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        other = _issue(client, "elsewhere")
        body = {"title": "made", "assignee_id": sol["user"]["id"]}
        keyed = _keyed(bearer(sol), "create")
        first = client.post("/issues", json=body, headers=keyed)
        assert first.status_code == 201, first.text
        _assign(client, other, sol["user"]["id"])
        _assign(client, first.json()["id"], 1)
        replay = client.post("/issues", json=body, headers=keyed)
        titles = [
            i["title"] for i in client.get("/issues", headers=ADMIN).json()["issues"]
        ]

    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY) == "true"
    assert replay.json() == first.json()
    assert titles.count("made") == 1
    assert _row(db_file, "create")["authority_scope"] == "none"


def _role(client, user_id):
    changed = client.put(
        f"/users/{user_id}/role", json={"role": "viewer"}, headers=ADMIN
    )
    assert changed.status_code == 200, changed.text


def _membership(client, user_id, project_id):
    removed = client.delete(f"/projects/{project_id}/members/{user_id}", headers=ADMIN)
    assert removed.status_code == 200, removed.text


def _visibility(client, user_id, project_id):
    # The member stays a member; another account's receipt is what is fenced.
    flipped = client.put(
        f"/projects/{project_id}/visibility",
        json={"visibility": "public"},
        headers=ADMIN,
    )
    assert flipped.status_code == 200, flipped.text


def _pause(client, user_id):
    for paused in (True, False):
        flipped = client.put(
            f"/users/{user_id}/paused", json={"paused": paused}, headers=ADMIN
        )
        assert flipped.status_code == 200, flipped.text


def _placement(client, user_id, project_id):
    elsewhere = client.post(
        "/projects", json={"name": "Elsewhere", "key": "ELS"}, headers=ADMIN
    ).json()["id"]
    unrelated = _issue(client, "unrelated", project_id=project_id)
    moved = client.put(
        f"/issues/{unrelated}/project", json={"project_id": elsewhere}, headers=ADMIN
    )
    assert moved.status_code == 200, moved.text


GLOBAL_ACTS: dict[str, Callable[..., None]] = {
    "role": lambda client, user_id, project_id: _role(client, user_id),
    "membership": _membership,
    "visibility": _visibility,
    "pause": lambda client, user_id, project_id: _pause(client, user_id),
    "placement": _placement,
}


@pytest.mark.parametrize("act", sorted(GLOBAL_ACTS))
def test_role_membership_visibility_pause_and_placement_still_fence_everything(
    tmp_path, act
):
    """Each act touches nothing of issue X, yet X's receipt is fenced: these are
    the acts that can hide what any response embeds or refuse any request."""
    app, _db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com")
        project = client.post(
            "/projects", json={"name": "Private", "key": "PRV"}, headers=ADMIN
        ).json()["id"]
        client.put(
            f"/projects/{project}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        client.post(
            f"/projects/{project}/members",
            json={"user_id": member["id"]},
            headers=ADMIN,
        )
        x = _issue(client, "X")
        _assign(client, x, member["id"])
        keyed = _keyed(as_user(member["id"]), f"before-{act}")
        assert (
            client.patch(f"/issues/{x}", json={"title": "Y"}, headers=keyed).status_code
            == 200
        )

        GLOBAL_ACTS[act](client, member["id"], project)
        replay = client.patch(f"/issues/{x}", json={"title": "Y"}, headers=keyed)

    assert replay.status_code == 409, replay.text
    assert replay.json()["code"] == "idempotency_authorization_changed"


def _bulk_after_an_unrelated_assignment(tmp_path, mutate=None):
    """A keyed bulk edit of A and B, then C assigned; the replay of the bulk key.
    ``mutate(conn)`` may narrow the fence first (the mutation tests)."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        if mutate is not None:
            with closing(db.connect(db_file)) as conn:
                mutate(conn)
                conn.commit()
        a, b, c = _issue(client, "A"), _issue(client, "B"), _issue(client, "C")
        body = {"ids": [a, b], "priority": "high"}
        keyed = _keyed(ADMIN, "bulk")
        first = client.post("/issues/bulk", json=body, headers=keyed)
        assert first.status_code == 200, first.text
        _assign(client, c, 1)
        return client.post("/issues/bulk", json=body, headers=keyed), db_file


def test_a_receipt_that_cannot_be_scoped_is_fenced_by_any_assignment(tmp_path):
    """Bulk names many issues: an assignment anywhere fences it, as before."""
    replay, db_file = _bulk_after_an_unrelated_assignment(tmp_path)
    assert replay.status_code == 409, replay.text
    assert "issue_id" not in replay.json()
    assert _row(db_file, "bulk")["authority_scope"] == "workspace"


def test_a_fenced_marker_expires_when_its_receipt_would_have(tmp_path):
    """Before its receipt's expiry the key stays refused; after it the key is a key
    never seen: the same request under it runs again, exactly as it would have
    once an unfenced receipt expired."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com")
        x = _issue(client, "X")
        _assign(client, x, member["id"])
        keyed = _keyed(as_user(member["id"]), "comment-x")
        said = client.post(f"/issues/{x}/comments", json={"body": "hi"}, headers=keyed)
        assert said.status_code == 201, said.text
        receipt_expiry = _row(db_file, "comment-x")["expires_at"]
        _assign(client, x, 1)
        _assign(client, x, member["id"])  # handed back: still fenced
        refused = client.post(
            f"/issues/{x}/comments", json={"body": "hi"}, headers=keyed
        )
        assert refused.status_code == 409, refused.text
        marker = _row(db_file, "comment-x")
        assert marker["failure_code"] == "authorization_changed"
        assert marker["expires_at"] == receipt_expiry

        # The clock passes the receipt's expiry (no test sleeps, no injectable clock).
        with closing(db.connect(db_file)) as conn:
            conn.execute(
                "UPDATE idempotency_keys SET expires_at = 1 "
                "WHERE idempotency_key = 'comment-x'"
            )
            conn.commit()
        again = client.post(f"/issues/{x}/comments", json={"body": "hi"}, headers=keyed)
        listed = client.get(f"/issues/{x}/comments", headers=ADMIN).json()["comments"]

    assert again.status_code == 201, again.text
    assert again.headers.get(REPLAY) is None
    assert again.json()["id"] != said.json()["id"]
    assert [c["body"] for c in listed] == ["hi", "hi"]
    assert _row(db_file, "comment-x")["state"] == "completed"


def test_lazy_retention_removes_expired_markers_too(tmp_path):
    db_file = tmp_path / "retention.db"
    with closing(db.connect(db_file)) as conn:
        db.migrate(conn)
        conn.executemany(
            "INSERT INTO idempotency_keys (idempotency_key, identity, "
            "request_fingerprint, authorization_revision, method, path, state, "
            "failure_code, expires_at) VALUES (?, 'actor:1', 'f', 0, 'POST', "
            "'/issues', 'indeterminate', ?, ?)",
            [
                ("fenced-expired", "authorization_changed", 100),
                ("fenced-live", "authorization_changed", 10_000),
                ("legacy-fenced", "authorization_changed", None),
                ("unknown-outcome", "execution_failed", None),
            ],
        )
        conn.commit()
        idempotency.claim_or_read(
            conn,
            key="other",
            identity="actor:1",
            method="POST",
            path="/issues",
            request_fingerprint="f",
            lease_seconds=60,
            now=101,
        )
        left = {
            row["idempotency_key"]
            for row in conn.execute("SELECT idempotency_key FROM idempotency_keys")
        }
    assert left == {"fenced-live", "legacy-fenced", "unknown-outcome", "other"}


def test_the_allow_lists_name_real_keyed_routes_and_odd_spellings_stay_global(
    tmp_path,
):
    from athena.middleware import idempotency_key_accepted

    app, _ = make_app(tmp_path)
    keyed = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if idempotency_key_accepted(method.upper(), path)
    }
    assert idempotency.ISSUE_SCOPED_ROUTES <= keyed
    assert idempotency.ISSUE_INDEPENDENT_ROUTES <= keyed
    scope = idempotency.receipt_scope
    assert scope("PATCH", "/issues/12") == idempotency.ReceiptScope("issue", 12)
    assert scope("DELETE", "/issues/7/links/relates/9").issue_id == 7
    assert scope("POST", "/issues/3/review-requests").issue_id == 3
    assert scope("POST", "/issues").kind == "none"
    for method, path in [
        ("POST", "/issues/bulk"),
        ("POST", "/issues/claim-next"),
        ("PATCH", "/issues/012"),
        ("PATCH", "/issues/+12"),
        ("PATCH", "/issues/１２"),
        ("POST", "/issues/12/not-reviewed"),
        ("PUT", "/issues/12"),
        ("PATCH", "/pages/12"),
    ]:
        assert scope(method, path) == idempotency.WORKSPACE_SCOPE, (method, path)


# --- narrowings that moved no counter before F6's correction ----------------------
#
# Each scenario stores a receipt, then narrows what its caller may do by an act
# that, before 0090's correction, moved no revision at all (on 9152454 the next
# unrelated assignment closed the hole; under a scoped fence it stayed open until
# expiry). The fresh request is refused; the replay must be too.


def _gated_close(client, user_id, project_id=None):
    x = _issue(client, "X", **({"project_id": project_id} if project_id else {}))
    _assign(client, x, user_id)
    return x


def _narrow_contributor(client, db_file):
    sol = onboard_agent(client)
    x = _issue(client, "X")
    _delegate(client, x, sol["user"]["id"])
    keyed = _keyed(bearer(sol), "edit")
    body = {"priority": "high"}
    request = lambda headers: client.patch(f"/issues/{x}", json=body, headers=headers)  # noqa: E731
    assert request(keyed).status_code == 200
    removed = client.delete(
        f"/issues/{x}/contributors/{sol['user']['id']}", headers=ADMIN
    )
    assert removed.status_code == 200, removed.text
    return request, keyed, bearer(sol), x


def _narrow_is_agent(client, db_file):
    human = add_user(client, "h@e.com")
    project = client.post(
        "/projects", json={"name": "P", "key": "PP"}, headers=ADMIN
    ).json()["id"]
    tag = client.get(f"/projects/{project}", headers=ADMIN).headers["ETag"]
    policy = client.put(
        f"/projects/{project}/policy",
        json={"block_agent_closes_when_blocked": True},
        headers={**ADMIN, "If-Match": tag},
    )
    assert policy.status_code == 200, policy.text
    x = _gated_close(client, human["id"], project)
    blocker = _issue(client, "B", project_id=project)
    linked = client.post(
        f"/issues/{x}/links",
        json={"target_ref": str(blocker), "relation": "blocked_by"},
        headers=ADMIN,
    )
    assert linked.status_code == 201, linked.text
    body = {"status": "done", "override_blocked_close": True}
    keyed = _keyed(as_user(human["id"]), "close")
    request = lambda headers: client.patch(f"/issues/{x}", json=body, headers=headers)  # noqa: E731
    assert request(keyed).status_code == 200
    flipped = client.put(
        f"/users/{human['id']}/agent", json={"is_agent": True}, headers=ADMIN
    )
    assert flipped.status_code == 200, flipped.text
    client.patch(f"/issues/{x}", json={"status": "open"}, headers=ADMIN)
    return request, keyed, as_user(human["id"]), x


def _narrow_close_policy(client, db_file):
    sol = onboard_agent(client)
    project = client.post(
        "/projects", json={"name": "P", "key": "PP"}, headers=ADMIN
    ).json()["id"]
    x = _gated_close(client, sol["user"]["id"], project)
    blocker = _issue(client, "B", project_id=project)
    client.post(
        f"/issues/{x}/links",
        json={"target_ref": str(blocker), "relation": "blocked_by"},
        headers=ADMIN,
    )
    keyed = _keyed(bearer(sol), "close")
    body = {"status": "done"}
    request = lambda headers: client.patch(f"/issues/{x}", json=body, headers=headers)  # noqa: E731
    assert request(keyed).status_code == 200
    tag = client.get(f"/projects/{project}", headers=ADMIN).headers["ETag"]
    policy = client.put(
        f"/projects/{project}/policy",
        json={"block_agent_closes_when_blocked": True},
        headers={**ADMIN, "If-Match": tag},
    )
    assert policy.status_code == 200, policy.text
    client.patch(f"/issues/{x}", json={"status": "open"}, headers=ADMIN)
    return request, keyed, bearer(sol), x


def _narrow_approval_gate(client, db_file):
    sol = onboard_agent(client)
    x = _gated_close(client, sol["user"]["id"])
    keyed = _keyed(bearer(sol), "close")
    body = {"status": "done"}
    request = lambda headers: client.patch(f"/issues/{x}", json=body, headers=headers)  # noqa: E731
    assert request(keyed).status_code == 200
    gated = client.put(
        f"/approvals/policies/{sol['user']['id']}",
        json={"action_kind": "issue.close"},
        headers=ADMIN,
    )
    assert gated.status_code == 200, gated.text
    client.patch(f"/issues/{x}", json={"status": "open"}, headers=ADMIN)
    return request, keyed, bearer(sol), x


def _narrow_budget(client, db_file):
    sol = onboard_agent(client)
    x = _gated_close(client, sol["user"]["id"])
    keyed = _keyed(bearer(sol), "edit")
    body = {"priority": "high"}
    request = lambda headers: client.patch(f"/issues/{x}", json=body, headers=headers)  # noqa: E731
    assert request(keyed).status_code == 200
    frozen = client.put(
        f"/users/{sol['user']['id']}/budget",
        json={"window": "day", "action_limit": 0},
        headers=ADMIN,
    )
    assert frozen.status_code == 200, frozen.text
    return request, keyed, bearer(sol), x


def _narrow_decline(client, db_file):
    sol = onboard_agent(client)
    x = _issue(client, "X")
    _delegate(client, x, sol["user"]["id"])
    tag = client.get(f"/issues/{x}", headers=bearer(sol)).headers["ETag"]
    keyed = {**_keyed(bearer(sol), "claim"), "If-Match": tag}
    claimed = client.post(f"/issues/{x}/claim", json={}, headers=keyed)
    assert claimed.status_code == 201, claimed.text
    generation = claimed.json()["generation"]
    declined = client.post(
        f"/issues/{x}/decline", json={"generation": generation}, headers=bearer(sol)
    )
    assert declined.status_code == 200, declined.text

    def request(headers):
        if headers is keyed:
            return client.post(f"/issues/{x}/claim", json={}, headers=keyed)
        fresh_tag = client.get(f"/issues/{x}", headers=bearer(sol)).headers["ETag"]
        return client.post(
            f"/issues/{x}/claim", json={}, headers={**headers, "If-Match": fresh_tag}
        )

    return request, keyed, bearer(sol), x


NARROWINGS: dict[str, tuple[Callable[..., Any], int, str | None]] = {
    # name: (scenario, the fresh request's status, its code)
    "contributor_removed": (_narrow_contributor, 403, "forbidden"),
    "declined": (_narrow_decline, 403, "forbidden"),
    "is_agent_flipped": (_narrow_is_agent, 409, "blocked_issue_close_policy"),
    "close_policy_on": (_narrow_close_policy, 409, "blocked_issue_close_policy"),
    "approval_gate_added": (_narrow_approval_gate, 202, "approval_required"),
    "budget_set": (_narrow_budget, 429, "agent_budget_exhausted"),
}


@pytest.mark.parametrize("narrowing", sorted(NARROWINGS))
def test_a_narrowing_that_moved_no_counter_now_fences_the_replay(tmp_path, narrowing):
    scenario, status, code = NARROWINGS[narrowing]
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        request, keyed, plain, _issue_id = scenario(client, db_file)
        fresh = request(_keyed(plain, "fresh"))
        replay = request(keyed)
    assert (fresh.status_code, fresh.json().get("code")) == (status, code), fresh.text
    assert replay.status_code == 409, replay.text
    assert replay.json()["code"] == "idempotency_authorization_changed"
    assert replay.headers.get(REPLAY) is None


def test_a_decline_fences_its_own_receipt_like_an_assignment(tmp_path):
    """A decline removes its caller from the contributors, which is a per-issue
    act: its fresh answer is sent once, and its key then refuses, as a keyed
    ``PUT /assignee`` that hands the issue away always has."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        x = _issue(client, "X")
        _delegate(client, x, sol["user"]["id"])
        keyed = _keyed(bearer(sol), "decline")
        first = client.post(f"/issues/{x}/decline", json={}, headers=keyed)
        again = client.post(f"/issues/{x}/decline", json={}, headers=keyed)
    assert first.status_code == 200, first.text
    assert again.status_code == 409, again.text
    assert again.json() == {
        **again.json(),
        "code": "idempotency_authorization_changed",
        "issue_id": x,
    }
    assert _row(db_file, "decline")["failure_code"] == "authorization_changed"


def test_spending_a_budget_moves_no_counter(tmp_path):
    """The one narrowing the fence does not see, by design: spending. Every metered
    write spends, and a replay applies nothing and is not metered, so the receipt
    replays while a fresh request is refused for the spent budget."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        budget = client.put(
            f"/users/{sol['user']['id']}/budget",
            json={"window": "day", "action_limit": 1},
            headers=ADMIN,
        )
        assert budget.status_code == 200, budget.text
        keyed = _keyed(bearer(sol), "create")
        first = client.post("/issues", json={"title": "one"}, headers=keyed)
        assert first.status_code == 201, first.text
        fresh = client.post(
            "/issues", json={"title": "one"}, headers=_keyed(bearer(sol), "n")
        )
        replay = client.post("/issues", json={"title": "one"}, headers=keyed)
    assert fresh.status_code == 429
    assert replay.status_code == 201
    assert replay.headers.get(REPLAY) == "true"


# --- processes still running the old code -----------------------------------------


def test_every_counter_trigger_also_moves_the_old_revision(tmp_path):
    """Mixed versions: a process still running the old code compares only
    ``revision``. Every trigger that moves any fence counter also moves it, so
    such a process fails closed on every act the new code fences, whatever order
    the processes restart in (the reviewer's test_r6_mixed.py runs one)."""
    with closing(db.connect(tmp_path / "triggers.db")) as conn:
        db.migrate(conn)
        bodies = {
            row["name"]: row["sql"]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND sql LIKE '%idempotency_authorization_state%'"
            )
        }
    assert len(bodies) >= 30
    for name, sql in bodies.items():
        assert "revision = revision + 1" in " ".join(sql.split()), name


def test_an_old_reader_sees_every_per_issue_act(tmp_path):
    """What an old process compares: ``revision`` moves on an assignment, a
    creator change and a contributor removal, though the new code's global
    component (revision minus the issue counter) does not."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        sol = onboard_agent(client)
        x = _issue(client, "X")
        _delegate(client, x, sol["user"]["id"])

        def counters():
            with closing(db.connect(db_file)) as conn:
                row = conn.execute(
                    "SELECT revision, issue_authority_revision "
                    "FROM idempotency_authorization_state"
                ).fetchone()
            return row["revision"], row["revision"] - row["issue_authority_revision"]

        before = counters()
        _assign(client, x, 1)
        after_assign = counters()
        client.delete(f"/issues/{x}/contributors/{sol['user']['id']}", headers=ADMIN)
        after_removal = counters()
    assert after_assign[0] == before[0] + 1 and after_assign[1] == before[1]
    assert after_removal[0] == before[0] + 2 and after_removal[1] == before[1]


# --- the scripted fleet run --------------------------------------------------------

#: The same run, on 9152454 and on this change: writes by four agents on ten
#: issues and creates, with assignments by the operator interleaved at random.
RUN_WRITES = 160
RUN_ASSIGNMENTS = 32
RUN_ISSUES = 10


def test_a_scripted_fleet_run_fences_only_the_receipts_of_reassigned_issues(tmp_path):
    rng = random.Random(6)
    app, _db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        seats = [
            onboard_agent(client, email=f"s{n}@e.com", name=f"S{n}") for n in range(4)
        ]
        issue_ids = [_issue(client, f"I{n}") for n in range(RUN_ISSUES)]
        for issue_id in issue_ids:
            for seat in seats:
                _delegate(client, issue_id, seat["user"]["id"])
        assignee: dict[int, int | None] = dict.fromkeys(issue_ids)
        steps = ["write"] * RUN_WRITES + ["assign"] * RUN_ASSIGNMENTS
        rng.shuffle(steps)
        sent: list[tuple[str, str, str, dict, dict, int | None]] = []
        changed_at: dict[int, int] = {}  # issue -> step of its last real change
        for index, step in enumerate(steps):
            if step == "assign":
                issue_id = rng.choice(issue_ids)
                who = rng.choice([None, *(s["user"]["id"] for s in seats)])
                _assign(client, issue_id, who)
                if who != assignee[issue_id]:
                    changed_at[issue_id] = index
                assignee[issue_id] = who
                continue
            seat = rng.choice(seats)
            key = f"w{index}"
            kind = rng.choice(["patch", "comment", "create"])
            target = None if kind == "create" else rng.choice(issue_ids)
            method, path, body = {
                "patch": (
                    "PATCH",
                    f"/issues/{target}",
                    {"priority": ("low", "medium", "high", "urgent")[index % 4]},
                ),
                "comment": (
                    "POST",
                    f"/issues/{target}/comments",
                    {"body": f"c{index}"},
                ),
                "create": ("POST", "/issues", {"title": f"n{index}"}),
            }[kind]
            headers = _keyed(bearer(seat), key)
            done = client.request(method, path, json=body, headers=headers)
            assert done.status_code in (200, 201), done.text
            sent.append((key, method, path, body, headers, target))
        fenced, expected = [], []
        for key, method, path, body, headers, target in sent:
            index = int(key[1:])
            replay = client.request(method, path, json=body, headers=headers)
            if replay.status_code == 409:
                assert replay.json()["code"] == "idempotency_authorization_changed"
                fenced.append(key)
            else:
                assert replay.headers.get(REPLAY) == "true", replay.text
            if target is not None and changed_at.get(target, -1) > index:
                expected.append(key)

    share = len(fenced) / len(sent)
    print(
        f"\nF6 scripted run: {len(fenced)} of {len(sent)} receipts fenced ({share:.1%})"
    )
    assert fenced == expected, (
        f"fenced {len(fenced)} of {len(sent)} ({share:.1%}); only the receipts of "
        f"issues reassigned after the write should be: {len(expected)}"
    )


# --- the property over generated histories -----------------------------------------
#
# The harness runs each write as its route does (the route's identity and writer
# gates, then the command) inside the real claim and completion of
# ``core/idempotency.py``, so the middleware's scope classification, the triggers
# and the counters are the shipped ones. HTTP is left out for speed (a keyed HTTP
# write costs ~30 ms); the scenarios above cover the transport, and so does the
# reviewer-named bulk test below.

#: Histories the property runs over in the suite, and the receipt TTL it uses.
HISTORIES = 3000
NOW = 1_800_000_000
TTL = 86_400
#: Refusal kinds a fresh request meets when the caller may not make it.
REFUSED = frozenset({"unauthorized", "forbidden", "not_found"})
#: What the commands the harness calls refuse with.
COMMAND_ERRORS = (
    issue_commands.IssueCommandError,
    comment_commands.CommentCommandError,
    approvals.ApprovalRequired,
    approvals.ApprovalRejected,
    budgets.BudgetExhausted,
)


def _refusal(exc: Exception) -> str | None:
    """The authority refusal an exception is, or None for a refusal of state.

    Authority: the caller may not make this request (401, 403, a hidden 404), an
    operator policy refuses it (the blocked-close policy, an approval gate) or an
    action budget the operator set does. State: a 409 or 422 about the target as it
    is now (a lease someone holds, a claim already completed, a cycle, an
    unknown agent). A replay returns the original outcome whatever the state; the
    fence is about authority. Budget refusals are classified by the caller, which
    knows whether the budget itself changed."""
    if isinstance(exc, approvals.ApprovalRequired | approvals.ApprovalRejected):
        return "approval_required"
    if isinstance(exc, budgets.BudgetExhausted):
        return "budget"
    kind = getattr(exc, "kind", None)
    if kind in REFUSED:
        return str(kind)
    if getattr(exc, "code", None) == issue_commands.BLOCKED_CLOSE_POLICY_ERROR_CODE:
        return "blocked_issue_close_policy"
    return None


@dataclass(frozen=True)
class Request:
    """One keyed write, as the route runs it: the method and path the middleware
    scopes the receipt by, the command the route calls, and what its answer
    exposes (issue ids, project ids)."""

    name: str
    method: str
    path: str
    run: Callable[[sqlite3.Connection, dict], Any]
    exposes: Callable[[Any], tuple[set[int], set[int]]]
    #: Restores, before the fresh request, the state its authority is decided in
    #: (a close is gated only as a transition into done), inside the undone
    #: savepoint. The question is whether the caller may make the request now.
    prepare: Callable[[sqlite3.Connection], None] | None = None


def _issue_exposure(answer: Any) -> tuple[set[int], set[int]]:
    shown = {int(answer["id"])}
    if answer.get("parent_id") is not None:
        shown.add(int(answer["parent_id"]))
    projects_shown = set()
    if answer.get("project_id") is not None:
        projects_shown.add(int(answer["project_id"]))
    return shown, projects_shown


def _just(issue_id: int) -> Callable[[Any], tuple[set[int], set[int]]]:
    return lambda answer: ({issue_id}, set())


def _link_exposure(issue_id: int) -> Callable[[Any], tuple[set[int], set[int]]]:
    def exposes(answer: Any) -> tuple[set[int], set[int]]:
        linked = {
            int(entry["id"])
            for relation in ("blocks", "blocked_by", "relates")
            for entry in answer.get(relation, [])
        }
        return {issue_id} | linked, set()

    return exposes


def _bulk(ids: list[int], priority: str) -> Callable[[sqlite3.Connection, dict], Any]:
    """``POST /issues/bulk`` as the route runs it: best effort, item by item, each
    item's refusal reported inside a 200."""

    def run(conn: sqlite3.Connection, actor: dict) -> Any:
        results = []
        for issue_id in ids:
            try:
                issue_commands.update_issue(
                    conn, actor=actor, issue_id=issue_id, priority=priority
                )
                results.append({"id": issue_id, "ok": True, "refusal": None})
            except COMMAND_ERRORS as exc:
                results.append(
                    {"id": issue_id, "ok": False, "refusal": _refusal(exc) or "state"}
                )
        return {"results": results}

    return run


def _bulk_exposure(answer: Any) -> tuple[set[int], set[int]]:
    return {int(r["id"]) for r in answer["results"] if r["ok"]}, set()


@dataclass
class Receipt:
    key: str
    user_id: int
    request: Request
    answer: Any
    budget: tuple | None
    #: What the answer showed that its caller could not read even when it was
    #: given (a hidden parent's id on a visible issue: the route's own answer,
    #: outside F6). The property is about what changed since.
    hidden_at_write: frozenset[int] = frozenset()


@dataclass
class History:
    """One generated workspace and the acts and writes made in it, inside one
    transaction that the harness rolls back afterwards."""

    conn: sqlite3.Connection
    rng: random.Random
    users: list[int] = field(default_factory=list)
    tokens: dict[int, str] = field(default_factory=dict)
    projects: list[int] = field(default_factory=list)
    issues: list[int] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    writes: int = 0
    log: list[str] = field(default_factory=list)
    act_kinds: list[str] = field(default_factory=list)
    write_kinds: list[str] = field(default_factory=list)

    # the world --------------------------------------------------------------

    def build(self) -> None:
        conn, rng = self.conn, self.rng
        # Swarm testing: each history draws its own few kinds of act and of write,
        # so an interaction that needs one act to stay unfenced by the others (a
        # close, an account turned agent, a reopen) comes up as often as a common
        # one.
        self.act_kinds = rng.sample(self.ACTS, rng.randint(1, 5))
        self.write_kinds = rng.sample(self.WRITES, rng.randint(2, 6))
        for user_id in range(1, 7):
            role = (
                "admin"
                if user_id == 1
                else rng.choice(["member", "member", "viewer", "admin"])
            )
            conn.execute(
                "INSERT INTO users (id, email, name, role, is_agent) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, f"u{user_id}@e.com", f"U{user_id}", role, rng.random() < 0.4),
            )
            self.users.append(user_id)
            if user_id > 1 and rng.random() < 0.5:
                minted = tokens.create_token(
                    conn,
                    user_id=user_id,
                    name="t",
                    scopes=rng.choice(
                        [
                            ["read", "issue:write"],
                            ["read", "issue:write"],
                            ["read"],
                            ["admin"],
                        ]
                    ),
                    commit=False,
                )
                self.tokens[user_id] = minted["token"]
        for n, visibility in enumerate(["public", "private", "private"]):
            made = projects.create_project(
                conn,
                name=f"P{n}",
                key=f"P{n}K",
                created_by=rng.choice(self.users),
                commit=False,
            )
            conn.execute(
                "UPDATE projects SET visibility = ?, "
                "block_agent_closes_when_blocked = ? WHERE id = ?",
                (visibility, rng.random() < 0.7, made["id"]),
            )
            self.projects.append(int(made["id"]))
            for user_id in self.users[1:]:
                if rng.random() < 0.4:
                    access.add_project_member(
                        conn, made["id"], user_id, 1, commit=False
                    )
        for n in range(5):
            project_id = rng.choice([None, *self.projects])
            made = issues.create_issue(
                conn,
                title=f"I{n}",
                body="",
                created_by=rng.choice(self.users),
                status=statuses.first_status(conn, project_id),
                project_id=project_id,
                commit=False,
            )
            issue_id = int(made["id"])
            issues.set_assignee(
                conn, issue_id, rng.choice([None, *self.users]), commit=False
            )
            for user_id in self.users[1:]:
                if rng.random() < 0.25:
                    contributors.add_contributor(
                        conn, issue_id, user_id, 1, commit=False
                    )
            self.issues.append(issue_id)
        # Open blockers nobody writes to or moves: the blocked-close policy then
        # refuses a close only when an operator policy narrows.
        for issue_id in self.issues:
            if rng.random() < 0.7:
                blocker = issues.create_issue(
                    conn,
                    title="blocker",
                    body="",
                    created_by=1,
                    status=statuses.first_status(conn, None),
                    commit=False,
                )
                dependencies.add_link(
                    conn,
                    from_id=int(blocker["id"]),
                    to_id=issue_id,
                    relation="blocks",
                    created_by=1,
                    commit=False,
                )
        for user_id in self.users[1:]:
            if rng.random() < 0.2:
                self._gate(user_id)
            if rng.random() < 0.2:
                self._budget(user_id, rng.randint(2, 6))

    def _gate(self, user_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO agent_approval_policies (user_id, action_kind) "
            "VALUES (?, ?)",
            (user_id, approvals.ACTION_ISSUE_CLOSE),
        )

    def _budget(self, user_id: int, limit: int) -> None:
        if budgets.get_budget(self.conn, user_id) is None:
            self.conn.execute(
                "INSERT INTO agent_budgets (user_id, window, action_limit, "
                "window_started_at) VALUES (?, 'day', ?, ?)",
                (user_id, limit, budgets.stamp_window(budgets.utc_now())),
            )
        else:
            self.conn.execute(
                "UPDATE agent_budgets SET action_limit = ? WHERE user_id = ?",
                (limit, user_id),
            )

    def _budget_of(self, user_id: int) -> tuple | None:
        row = self.conn.execute(
            "SELECT window, action_limit FROM agent_budgets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return tuple(row) if row is not None else None

    # the acts the fence is about ---------------------------------------------

    WRITES = (
        "patch",
        "close",
        "reopen",
        "comment",
        "parent",
        "link",
        "create",
        "bulk",
        "claim",
        "complete",
        "decline",
        "delegate",
    )
    ACTS = (
        "assign",
        "creator",
        "contributor_add",
        "contributor_remove",
        "role",
        "member_remove",
        "member_add",
        "visibility",
        "pause",
        "placement",
        "is_agent",
        "close_policy",
        "gate",
        "ungate",
        "budget",
        "unbudget",
        "token_scopes",
        "lease",
    )

    def act(self) -> None:
        conn, rng = self.conn, self.rng
        issue_id = rng.choice(self.issues)
        # Acts land more often on accounts holding receipts: those are the ones
        # whose narrowing the property can see.
        holders = sorted({r.user_id for r in self.receipts} - {1})
        user_id = (
            rng.choice(holders)
            if holders and rng.random() < 0.6
            else rng.choice(self.users[1:])
        )
        project_id = rng.choice(self.projects)
        kind = rng.choice(self.act_kinds)
        detail = ""
        if kind == "assign":
            who = rng.choice([None, *self.users])
            issues.set_assignee(conn, issue_id, who, commit=False)
            detail = f"issue {issue_id} -> {who}"
        elif kind == "creator":
            who = rng.choice(self.users)
            conn.execute(
                "UPDATE issues SET created_by = ? WHERE id = ?", (who, issue_id)
            )
            detail = f"issue {issue_id} creator -> {who}"
        elif kind == "contributor_add":
            contributors.add_contributor(conn, issue_id, user_id, 1, commit=False)
            detail = f"user {user_id} contributes to {issue_id}"
        elif kind == "contributor_remove":
            # DELETE /contributors and a decline both remove the row.
            contributors.remove_contributor(conn, issue_id, user_id, commit=False)
            detail = f"user {user_id} off {issue_id}"
        elif kind == "role":
            role = rng.choice(["member", "viewer", "admin"])
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            detail = f"user {user_id} -> {role}"
        elif kind == "member_remove":
            access.remove_project_member(conn, project_id, user_id, commit=False)
            detail = f"user {user_id} out of project {project_id}"
        elif kind == "member_add":
            access.add_project_member(conn, project_id, user_id, 1, commit=False)
            detail = f"user {user_id} into project {project_id}"
        elif kind == "visibility":
            visibility = rng.choice(["public", "private"])
            conn.execute(
                "UPDATE projects SET visibility = ? WHERE id = ?",
                (visibility, project_id),
            )
            detail = f"project {project_id} -> {visibility}"
        elif kind == "pause":
            conn.execute(
                "UPDATE users SET paused_at = CASE WHEN paused_at IS NULL "
                "THEN datetime('now') ELSE NULL END WHERE id = ?",
                (user_id,),
            )
            detail = f"user {user_id} pause flipped"
        elif kind == "placement":
            target = rng.choice([None, *self.projects])
            conn.execute(
                "UPDATE issues SET project_id = ? WHERE id = ?", (target, issue_id)
            )
            detail = f"issue {issue_id} -> project {target}"
        elif kind == "is_agent":
            conn.execute(
                "UPDATE users SET is_agent = 1 - is_agent WHERE id = ?", (user_id,)
            )
            detail = f"user {user_id} is_agent flipped"
        elif kind == "close_policy":
            conn.execute(
                "UPDATE projects SET block_agent_closes_when_blocked = "
                "1 - block_agent_closes_when_blocked WHERE id = ?",
                (project_id,),
            )
            detail = f"project {project_id} close policy flipped"
        elif kind == "gate":
            self._gate(user_id)
            detail = f"user {user_id} closes gated"
        elif kind == "ungate":
            conn.execute(
                "DELETE FROM agent_approval_policies WHERE user_id = ?", (user_id,)
            )
            detail = f"user {user_id} closes ungated"
        elif kind == "budget":
            limit = rng.randint(0, 4)
            self._budget(user_id, limit)
            detail = f"user {user_id} budget {limit}"
        elif kind == "unbudget":
            conn.execute("DELETE FROM agent_budgets WHERE user_id = ?", (user_id,))
            detail = f"user {user_id} budget removed"
        elif kind == "token_scopes":
            scopes = rng.choice([["read", "issue:write"], ["read"], ["admin"]])
            conn.execute(
                "UPDATE api_tokens SET scopes = ? WHERE user_id = ?",
                (" ".join(scopes), user_id),
            )
            detail = f"user {user_id} token scopes -> {scopes}"
        else:
            # Someone else takes the issue's lease: state, not authority.
            conn.execute("DELETE FROM issue_leases WHERE issue_id = ?", (issue_id,))
            leases.upsert_lease(conn, issue_id, user_id, 600, commit=False)
            detail = f"user {user_id} holds {issue_id}"
        self.log.append(f"act {kind}: {detail}")

    # the writes that leave receipts ------------------------------------------

    def _status(self, issue_id: int, category: str) -> str:
        project_id = issues.get_issue(self.conn, issue_id)["project_id"]
        return next(
            row["name"]
            for row in statuses.list_statuses(self.conn, project_id)
            if row["category"] == category
        )

    def request(self) -> Request:
        rng = self.rng
        x = rng.choice(self.issues)
        y = rng.choice([i for i in self.issues if i != x])
        n = self.writes
        kind = rng.choice(self.write_kinds)
        if kind == "patch":
            return Request(
                kind,
                "PATCH",
                f"/issues/{x}",
                lambda c, a: issue_commands.update_issue(
                    c, actor=a, issue_id=x, title=f"t{n}"
                ),
                _issue_exposure,
            )
        if kind in ("close", "reopen"):
            status = self._status(x, "done" if kind == "close" else "todo")
            reopened = self._status(x, "todo")
            current = issues.get_issue(self.conn, x)
            # Only a close that is a transition into done is gated; the fresh
            # request re-creates that transition.
            transition = kind == "close" and not statuses.is_done(
                self.conn, current["project_id"], current["status"]
            )
            return Request(
                kind,
                "PATCH",
                f"/issues/{x}",
                lambda c, a: issue_commands.update_issue(
                    c,
                    actor=a,
                    issue_id=x,
                    status=status,
                    override_blocked_close=True,
                ),
                _issue_exposure,
                prepare=(
                    (lambda c: issues.update_issue(c, x, status=reopened, commit=False))
                    if transition
                    else None
                ),
            )
        if kind == "comment":
            return Request(
                kind,
                "POST",
                f"/issues/{x}/comments",
                lambda c, a: comment_commands.create_comment(
                    c, actor=a, issue_id=x, body=f"c{n}"
                ),
                lambda answer: ({int(answer["issue_id"])}, set()),
            )
        if kind == "parent":
            return Request(
                kind,
                "PUT",
                f"/issues/{x}/parent",
                lambda c, a: issue_commands.set_issue_parent(
                    c, actor=a, issue_id=x, parent_id=y
                ),
                _issue_exposure,
            )
        if kind == "link":
            return Request(
                kind,
                "POST",
                f"/issues/{x}/links",
                lambda c, a: issue_commands.link_issues(
                    c, actor=a, issue_id=x, target_ref=str(y), relation="relates"
                ),
                _link_exposure(x),
            )
        if kind == "create":
            project_id = rng.choice([None, *self.projects])
            parent = rng.choice([None, y])
            assignee = rng.choice([None, *self.users])
            delegates = rng.choice([[], [rng.choice(self.users)]])
            return Request(
                kind,
                "POST",
                "/issues",
                lambda c, a: issue_commands.create_issue(
                    c,
                    actor=a,
                    title=f"n{n}",
                    project_id=project_id,
                    parent_id=parent,
                    assignee_id=assignee,
                    delegate_ids=delegates,
                ),
                _issue_exposure,
            )
        if kind == "bulk":
            return Request(
                kind, "POST", "/issues/bulk", _bulk([x, y], "high"), _bulk_exposure
            )
        if kind == "claim":

            def claim(c: sqlite3.Connection, a: dict) -> Any:
                tag = issue_etags.current_etag(c, issues.get_issue(c, x))
                return lease_commands.claim_issue(
                    c, actor=a, issue_id=x, if_match=[tag]
                )

            return Request(kind, "POST", f"/issues/{x}/claim", claim, _just(x))
        if kind == "complete":

            def complete(c: sqlite3.Connection, a: dict) -> Any:
                lease = leases.get_lease(c, x)
                return lease_commands.complete_claim(
                    c,
                    actor=a,
                    issue_id=x,
                    generation=lease["generation"] if lease else None,
                )

            return Request(kind, "POST", f"/issues/{x}/complete", complete, _just(x))
        if kind == "decline":
            return Request(
                kind,
                "POST",
                f"/issues/{x}/decline",
                lambda c, a: lease_commands.decline_delegation(c, actor=a, issue_id=x),
                _just(x),
            )
        target = rng.choice(self.users)
        return Request(
            kind,
            "POST",
            f"/issues/{x}/delegate",
            lambda c, a: issue_commands.add_contributor(
                c, actor=a, issue_id=x, user_id=target, require_agent=True
            ),
            _just(x),
        )

    def _identity(self, user_id: int) -> str:
        raw = self.tokens.get(user_id)
        return f"actor:{user_id}" if raw is None else f"tok:{tokens._hash(raw)}"

    def _actor(self, user_id: int) -> dict | None:
        """The account as the route's identity gate admits it, through its token
        when it has one: a paused account never reaches a route (nor, with a key,
        a replay)."""
        raw = self.tokens.get(user_id)
        if raw is None:
            actor = users.get_user(self.conn, user_id)
        else:
            actor = tokens.inspect_token(self.conn, raw).actor
        if actor is None or actor.get("paused_at"):
            return None
        return actor

    def _route_refusal(self, actor: dict | None) -> str | None:
        """The route dependency ``issue_write_actor``: a writer role and scope."""
        if actor is None:
            return "paused"
        if not identity.can_write(actor):
            return "forbidden"
        if not identity.token_has_scope(actor, tokens.ISSUE_WRITE_SCOPE):
            return "forbidden"
        return None

    def _hidden(
        self, actor: dict | None, shown_issues: set[int], shown_projects: set[int]
    ) -> frozenset[int]:
        """The shown issues (and projects, as negative ids) ``actor`` cannot read."""
        return frozenset(
            {i for i in shown_issues if not access.can_see_issue(self.conn, actor, i)}
            | {
                -p
                for p in shown_projects
                if not access.can_see_project(self.conn, actor, p)
            }
        )

    @contextmanager
    def _undone(self):
        """Run a fresh request and leave no trace of it."""
        self.conn.execute("SAVEPOINT fresh")
        try:
            yield
        finally:
            self.conn.execute("ROLLBACK TO SAVEPOINT fresh")
            self.conn.execute("RELEASE SAVEPOINT fresh")

    def _fresh(self, receipt: Receipt) -> tuple[str | None, Any]:
        """The same request, made now by the same caller and then undone: the
        authority refusal it meets (None when it is allowed), and its answer."""
        actor = self._actor(receipt.user_id)
        refused = self._route_refusal(actor)
        if refused is not None:
            return refused, None
        assert actor is not None
        with self._undone():
            if receipt.request.prepare is not None:
                receipt.request.prepare(self.conn)
            try:
                answer = receipt.request.run(self.conn, actor)
            except COMMAND_ERRORS as exc:
                refusal = _refusal(exc)
                if (
                    refusal == "budget"
                    and self._budget_of(receipt.user_id) == receipt.budget
                ):
                    # Spent, not narrowed: the documented exception (a replay is
                    # not metered and applies nothing).
                    refusal = None
                return refusal, None
        if receipt.request.name == "bulk":
            stored_ok = {r["id"] for r in receipt.answer["results"] if r["ok"]}
            for item in answer["results"]:
                if item["id"] in stored_ok and item["refusal"] not in (None, "state"):
                    if (
                        item["refusal"] == "budget"
                        and self._budget_of(receipt.user_id) == receipt.budget
                    ):
                        continue
                    return f"bulk item {item['id']}: {item['refusal']}", answer
        return None, answer

    def write(self) -> None:
        user_id = self.rng.choice(self.users)
        request = self.request()
        self.writes += 1
        actor = self._actor(user_id)
        if self._route_refusal(actor) is not None:
            return
        assert actor is not None
        key = f"k{self.writes}"
        ident = self._identity(user_id)
        claim = idempotency.claim_or_read(
            self.conn,
            key=key,
            identity=ident,
            method=request.method,
            path=request.path,
            request_fingerprint=key,
            lease_seconds=60,
            now=NOW,
        )
        assert claim.kind == "owner" and claim.record is not None
        owner = claim.record["owner_token"]
        budget = self._budget_of(user_id)
        self.conn.execute("SAVEPOINT route")
        try:
            answer = request.run(self.conn, actor)
        except COMMAND_ERRORS:
            # A 4xx or a 202 ask: nothing applied, the key is released.
            self.conn.execute("ROLLBACK TO SAVEPOINT route")
            self.conn.execute("RELEASE SAVEPOINT route")
            idempotency.release(self.conn, key=key, identity=ident, owner_token=owner)
            return
        self.conn.execute("RELEASE SAVEPOINT route")
        idempotency.complete(
            self.conn,
            key=key,
            identity=ident,
            owner_token=owner,
            status_code=200,
            content_type="application/json",
            response_headers="[]",
            response_body=json.dumps(answer, default=str).encode(),
            ttl_seconds=TTL,
            now=NOW,
        )
        stored = json.loads(json.dumps(answer, default=str))
        self.receipts.append(
            Receipt(
                key,
                user_id,
                request,
                stored,
                budget,
                self._hidden(actor, *request.exposes(stored)),
            )
        )
        self.log.append(
            f"write {request.name} {request.method} {request.path} by {user_id}"
        )

    # the property --------------------------------------------------------------

    def check(self) -> None:
        for receipt in self.receipts:
            if self._actor(receipt.user_id) is None:
                continue  # paused: the middleware never replays (0061)
            claim = idempotency.claim_or_read(
                self.conn,
                key=receipt.key,
                identity=self._identity(receipt.user_id),
                method=receipt.request.method,
                path=receipt.request.path,
                request_fingerprint=receipt.key,
                lease_seconds=60,
                now=NOW,
            )
            if claim.kind == "authorization_changed":
                continue
            assert claim.kind == "replay", (claim.kind, receipt.key)
            stored = json.loads(claim.record["response_body"])
            refusal, fresh = self._fresh(receipt)
            actor = self._actor(receipt.user_id)
            # What the stored answer shows that the caller can neither read now
            # nor get from the same request now. (An answer may show what its
            # caller cannot read, such as a hidden parent's id on a visible issue;
            # that is the route's own answer, replayed or not.)
            shown_issues, shown_projects = receipt.request.exposes(stored)
            fresh_issues, fresh_projects = (
                receipt.request.exposes(fresh) if fresh is not None else (set(), set())
            )
            hidden = sorted(
                self._hidden(
                    actor, shown_issues - fresh_issues, shown_projects - fresh_projects
                )
                - receipt.hidden_at_write
            )
            assert refusal is None and not hidden, (
                f"{receipt.request.name} {receipt.request.method} "
                f"{receipt.request.path} by user {receipt.user_id} replayed its "
                f"stored response, but a fresh request is refused ({refusal}) or "
                f"the replay exposes what the caller can no longer obtain "
                f"({hidden}; negative ids are projects)"
            )

    def play(self, steps: int) -> None:
        self.build()
        for _ in range(steps):
            if self.rng.random() < 0.5:
                self.write()
            else:
                self.act()
            # After a write too: a write can change state (a reopen) so that the
            # fresh request now meets a narrowing an earlier act made.
            self.check()


@contextmanager
def _template(tmp_path):
    """A migrated database and one fast connection: every history runs inside one
    transaction the harness rolls back, so they share nothing."""
    db_file = tmp_path / "property.db"
    with closing(db.connect(db_file)) as conn:
        db.migrate(conn)
        conn.execute("PRAGMA synchronous = OFF")
        yield conn


def run_histories(conn: sqlite3.Connection, count: int, *, first_seed: int = 0) -> int:
    """Play ``count`` histories; raise on the first counterexample, naming its seed
    and its steps. Returns how many receipts were checked."""
    checked = 0
    for seed in range(first_seed, first_seed + count):
        history = History(conn, random.Random(seed))
        conn.execute("BEGIN")
        try:
            history.play(steps=20)
        except AssertionError as exc:
            raise AssertionError(
                f"history seed {seed}:\n  " + "\n  ".join(history.log) + f"\n{exc}"
            ) from None
        finally:
            conn.rollback()
        checked += len(history.receipts)
    return checked


def test_a_replay_never_returns_what_its_caller_could_not_obtain_now(tmp_path):
    with _template(tmp_path) as conn:
        checked = run_histories(conn, HISTORIES)
    assert checked > 2 * HISTORIES  # the generator leaves receipts to check


# --- the property has teeth: fences narrowed too far ------------------------------

_PER_ISSUE_BODY = (
    "UPDATE idempotency_authorization_state SET revision = revision + 1, "
    "issue_authority_revision = issue_authority_revision + 1 WHERE singleton = 1; "
    "INSERT OR IGNORE INTO idempotency_issue_authority VALUES ({row}.{col}, 0); "
    "UPDATE idempotency_issue_authority SET revision = revision + 1 "
    "WHERE issue_id = {row}.{col};"
)


def _scope(
    override: Callable[[str, str, idempotency.ReceiptScope], idempotency.ReceiptScope],
):
    def mutate(monkeypatch, conn):
        real = idempotency.receipt_scope
        monkeypatch.setattr(
            idempotency,
            "receipt_scope",
            lambda method, path: override(method, path, real(method, path)),
        )

    return mutate


def _ddl(*statements: str):
    def mutate(monkeypatch, conn):
        conn.executescript(";".join(statements) + ";")

    return mutate


def _issue_trigger(columns: str, when: str, body: str) -> str:
    return (
        "DROP TRIGGER idempotency_issue_authority_changed; "
        "CREATE TRIGGER idempotency_issue_authority_changed "
        f"AFTER UPDATE OF {columns} ON issues WHEN {when} BEGIN {body} END"
    )


MUTANTS: dict[str, Callable[..., None]] = {
    # Every receipt depends on no issue's assignee or creator.
    "no_issue_fence": _scope(lambda m, p, s: idempotency.ReceiptScope("none")),
    # Scoped to the issue next to the one the path names.
    "wrong_issue": _scope(
        lambda m, p, s: (
            idempotency.ReceiptScope("issue", int(s.issue_id) + 1)
            if s.kind == "issue"
            else s
        )
    ),
    # POST /issues/bulk fenced by no assignment (the reviewer's).
    "bulk_none": _scope(
        lambda m, p, s: idempotency.ReceiptScope("none") if p == "/issues/bulk" else s
    ),
    # POST /issues/bulk scoped to ONE issue, though its path names none (the
    # reviewer's).
    "bulk_first_id": _scope(
        lambda m, p, s: (
            idempotency.ReceiptScope("issue", 1) if p == "/issues/bulk" else s
        )
    ),
    # A creator change moves no counter.
    "creator_unfenced": _ddl(
        _issue_trigger(
            "assignee_id",
            "NEW.assignee_id IS NOT OLD.assignee_id",
            _PER_ISSUE_BODY.format(row="NEW", col="id"),
        )
    ),
    # An assignee change moves no counter (the reviewer's).
    "assignee_unfenced": _ddl(
        _issue_trigger(
            "created_by",
            "NEW.created_by IS NOT OLD.created_by",
            _PER_ISSUE_BODY.format(row="NEW", col="id"),
        )
    ),
    # A per-issue act moves only that issue's counter, so the workspace-scoped
    # receipts (bulk, claim-next, every route outside /issues) are never fenced by
    # an assignment (the reviewer's "workspace counter frozen").
    "workspace_counter_frozen": _ddl(
        _issue_trigger(
            "created_by, assignee_id",
            "NEW.created_by IS NOT OLD.created_by "
            "OR NEW.assignee_id IS NOT OLD.assignee_id",
            "INSERT OR IGNORE INTO idempotency_issue_authority VALUES (NEW.id, 0); "
            "UPDATE idempotency_issue_authority SET revision = revision + 1 "
            "WHERE issue_id = NEW.id;",
        )
    ),
    # Placement fences only the moved issue's receipts, like an assignment.
    "placement_per_issue": _ddl(
        "DROP TRIGGER idempotency_authz_issue_placement_changed",
        "CREATE TRIGGER idempotency_authz_issue_placement_changed "
        "AFTER UPDATE OF project_id ON issues "
        "WHEN NEW.project_id IS NOT OLD.project_id BEGIN "
        + _PER_ISSUE_BODY.format(row="NEW", col="id")
        + " END",
    ),
    # Each narrowing that moved no counter before F6's correction.
    "contributor_removal_unfenced": _ddl(
        "DROP TRIGGER idempotency_issue_contributor_removed"
    ),
    "is_agent_unfenced": _ddl("DROP TRIGGER idempotency_authz_user_is_agent"),
    "close_policy_unfenced": _ddl(
        "DROP TRIGGER idempotency_authz_project_close_policy"
    ),
    "approval_gate_unfenced": _ddl(
        "DROP TRIGGER idempotency_authz_approval_policy_added"
    ),
    "budget_unfenced": _ddl(
        "DROP TRIGGER idempotency_authz_budget_set",
        "DROP TRIGGER idempotency_authz_budget_changed",
    ),
    # And one the fence had from the start (the reviewer's).
    "membership_unfenced": _ddl(
        "DROP TRIGGER idempotency_authz_project_member_removed"
    ),
}


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_the_property_catches_a_fence_narrowed_too_far(tmp_path, monkeypatch, mutant):
    with _template(tmp_path) as conn:
        MUTANTS[mutant](monkeypatch, conn)
        conn.commit()
        with pytest.raises(AssertionError, match="replayed its stored response"):
            run_histories(conn, HISTORIES)


@pytest.mark.parametrize(
    "mutant", ["bulk_none", "bulk_first_id", "workspace_counter_frozen"]
)
def test_the_named_bulk_scenario_catches_the_workspace_mutants(
    tmp_path, monkeypatch, mutant
):
    """The three mutants the first property missed (bulk reports a refused item
    inside a 200) are caught by the property now, and by the named HTTP test
    ``test_a_receipt_that_cannot_be_scoped_is_fenced_by_any_assignment`` too:
    under each, its scenario replays instead of answering 409."""
    replay, _db_file = _bulk_after_an_unrelated_assignment(
        tmp_path, lambda conn: MUTANTS[mutant](monkeypatch, conn)
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers.get(REPLAY) == "true"


def test_the_property_runs_on_the_shipped_schema(tmp_path):
    # The template carries this change's triggers, not an older copy.
    with _template(tmp_path) as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    assert {
        "idempotency_issue_authority_changed",
        "idempotency_issue_contributor_removed",
        "idempotency_authz_issue_placement_changed",
        "idempotency_authz_user_is_agent",
    } <= names
    assert "idempotency_authz_issue_authority_changed" not in names
