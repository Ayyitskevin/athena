"""Forge integration — inbound signed events landing as imported history.

Three layers, tested separately because they fail differently:

* the **parser** (`core/forge_events.py`), pure and fixture-free — what a payload
  means and which tokens look like issue keys;
* the **landing rule** (`aegis/forge.py`), where a key becomes an issue and an
  event becomes foreign history;
* the **endpoint**, which is the only route in Athena an unauthenticated stranger
  is expected to hit, and whose refusals are therefore the point.

The rule threaded through all of it: a forge event is a **claim by the source**.
It is recorded as imported, which is what keeps it out of undo, the lifecycle
facts, and every metric — and Athena never calls the forge back to check.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from athena import config
from athena.core import db, forge_events
from athena.core.forge_events import ForgeEventError
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="forge.db"):
    return create_app(tmp_path / name)


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _seed_operator(db_file):
    """An admin user WITHOUT spending anon-limit budget: the HTTP bootstrap
    (POST /users) is itself an anonymous call and would be charged against the
    very limit the burst tests measure."""
    conn = db.connect(db_file)
    conn.execute(
        "INSERT INTO users (email, name, role) VALUES ('a@e.com', 'A', 'admin')"
    )
    conn.commit()
    conn.close()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _source(client, name="gh", host="github.com"):
    return client.post(
        "/event-sources", json={"name": name, "host": host}, headers=H1
    ).json()


def _project_issue(client, key="ATH", title="Fix the redirect"):
    project = client.post(
        "/projects", json={"key": key, "name": key}, headers=H1
    ).json()
    return client.post(
        "/issues", json={"title": title, "project_id": project["id"]}, headers=H1
    ).json()


def _deliver(client, source_name, event, payload, secret, *, signature=None):
    body = json.dumps(payload).encode()
    return client.post(
        f"/forge/{source_name}",
        content=body,
        headers={
            "X-Hub-Signature-256": signature or _sign(secret, body),
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        },
    )


def _push(message, ref="refs/heads/main", url="https://github.com/o/r/commit/abc"):
    return {"ref": ref, "commits": [{"message": message, "url": url}]}


# --- the parser (no database) -----------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ATH-12 fix the redirect", [("ATH", 12)]),
        ("feature/ATH-12-thing", [("ATH", 12)]),
        ("closes ATH-12 and ATH-13", [("ATH", 12), ("ATH", 13)]),
        ("ATH-12 again ATH-12", [("ATH", 12)]),  # deduped
        ("ath-12 lowercase", [("ATH", 12)]),  # project keys are NOCASE
        ("ATH-12x", []),  # a key does not run into a word
        ("no keys here", []),
    ],
)
def test_which_tokens_look_like_issue_keys(text, expected):
    assert forge_events.extract_keys(text) == expected


def test_encoding_names_are_key_shaped_and_that_is_the_databases_problem():
    # This is the whole reason extraction and resolution are separate steps: these
    # are indistinguishable from an issue key by shape alone, and only a real
    # project named UTF or ISO could turn them into a landing.
    assert forge_events.extract_keys("encode as UTF-8 per ISO-8601") == [
        ("UTF", 8),
        ("ISO", 8601),
    ]


def test_a_push_takes_keys_from_the_branch_when_the_message_omits_them():
    facts = forge_events.parse_github(
        "push", _push("tidy up", ref="refs/heads/ATH-12-redirect")
    )
    # Teams that name branches ATH-12-thing routinely write commit messages that
    # never repeat the key.
    assert [f.keys for f in facts] == [(("ATH", 12),)]
    assert facts[0].kind == forge_events.KIND_PUSH


def test_a_merged_pull_request_is_distinguishable_from_a_closed_one():
    def parse(merged):
        return forge_events.parse_github(
            "pull_request",
            {
                "action": "closed",
                "pull_request": {
                    "number": 7,
                    "title": "ATH-12 redirect",
                    "merged": merged,
                    "html_url": "https://github.com/o/r/pull/7",
                },
            },
        )[0]

    # GitHub has no "merged" action — a close carries a flag, and the difference
    # is the entire point of the event.
    assert "merged" in parse(True).summary
    assert "closed" in parse(False).summary


@pytest.mark.parametrize(
    "event,payload",
    [
        ("pull_request", {"action": "labeled", "pull_request": {"title": "ATH-12"}}),
        ("create", {"ref_type": "tag", "ref": "ATH-12"}),
        ("issue_comment", {"whatever": True}),
        ("workflow_run", {"whatever": True}),
    ],
)
def test_an_event_outside_the_vocabulary_is_uninteresting_not_an_error(event, payload):
    # A tracker that absorbed every event shape a forge invents would become a
    # second, worse copy of the forge.
    assert forge_events.parse_github(event, payload) == []


@pytest.mark.parametrize(
    "event,payload,fragment",
    [
        ("push", {"ref": "refs/heads/x"}, "no 'commits'"),
        ("push", {"commits": "not a list"}, "must be a list"),
        ("pull_request", {"action": "opened"}, "no 'pull_request'"),
    ],
)
def test_a_payload_contradicting_its_own_header_is_malformed(event, payload, fragment):
    with pytest.raises(ForgeEventError) as caught:
        forge_events.parse_github(event, payload)
    assert fragment in str(caught.value)


def test_a_push_is_bounded_by_its_commit_ceiling():
    many = {
        "ref": "refs/heads/main",
        "commits": [
            {"message": f"ATH-{n} work", "url": "u"}
            for n in range(forge_events.MAX_COMMITS + 25)
        ],
    }
    assert len(forge_events.parse_github("push", many)) == forge_events.MAX_COMMITS


def test_describe_emits_the_real_vocabulary():
    described = forge_events.describe()
    assert set(described["source_kinds"]) == set(forge_events.SOURCE_KINDS)
    assert described["limits"]["max_body_bytes"] == forge_events.MAX_BODY_BYTES


# --- the endpoint's refusals ------------------------------------------------


def test_an_unknown_source_and_a_bad_signature_refuse_identically(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        payload = _push("ATH-1 x")

        wrong_signature = _deliver(
            c, "gh", "push", payload, source["secret"], signature="sha256=deadbeef"
        )
        unknown_source = _deliver(c, "nope", "push", payload, source["secret"])
        # Answering "no such source" would turn this route into a directory of
        # the workspace's integrations for anyone who can reach it.
        assert wrong_signature.status_code == unknown_source.status_code == 401
        assert wrong_signature.json() == unknown_source.json()


def test_a_non_ascii_signature_is_a_wrong_signature_not_a_crash(tmp_path):
    # WHY: hmac.compare_digest raises TypeError on a non-ASCII str. Before this
    # fix, `sha256=ÿÿ` against a REGISTERED source 500'd (the compare ran) while
    # an unknown source refused cleanly — a one-request oracle for which source
    # names exist, defeating the identical-401 property above. A malformed header
    # is a wrong signature: same 401, same body, for known and unknown alike.
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        _source(c)
        body = json.dumps(_push("ATH-1 x")).encode()
        # Sent as raw bytes (a str header must be ASCII for httpx to encode it);
        # the server decodes header bytes as latin-1, so the handler still sees
        # the non-ASCII str that used to crash the compare.
        headers = {
            b"X-Hub-Signature-256": b"sha256=\xff\xff",
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        }
        known = c.post("/forge/gh", content=body, headers=headers)
        unknown = c.post("/forge/nope", content=body, headers=headers)
        assert known.status_code == unknown.status_code == 401
        assert known.json() == unknown.json()


def test_an_unsigned_delivery_is_refused(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        _source(c)
        assert (
            c.post(
                "/forge/gh", content=b"{}", headers={"X-GitHub-Event": "push"}
            ).status_code
            == 401
        )


def test_the_signature_is_over_the_exact_bytes_received(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        _project_issue(c)
        body = json.dumps(_push("ATH-1 x")).encode()
        signature = _sign(source["secret"], body)
        # One byte of tampering after signing must invalidate it.
        tampered = body.replace(b"ATH-1", b"ATH-2")
        assert (
            c.post(
                "/forge/gh",
                content=tampered,
                headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "push"},
            ).status_code
            == 401
        )


def test_an_unauthenticated_delivery_never_reaches_the_json_parser(tmp_path):
    # Body that would fail JSON parsing AND fail signature check. If the parser
    # ran first this would be a 400; authentication first makes it a 401. That
    # ordering is why the handler takes a raw Request rather than declaring a
    # Pydantic body, which FastAPI would parse before the handler ran.
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        _source(c)
        resp = c.post(
            "/forge/gh",
            content=b"{not json at all",
            headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "push"},
        )
        assert resp.status_code == 401


def test_an_oversized_body_is_refused(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        huge = _push("ATH-1 " + "x" * (forge_events.MAX_BODY_BYTES + 1000))
        assert _deliver(c, "gh", "push", huge, source["secret"]).status_code == 413


def test_a_paused_source_is_refused_without_impugning_its_credential(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        _project_issue(c)
        c.put(
            f"/event-sources/{source['id']}/enabled",
            json={"enabled": False},
            headers=H1,
        )
        # 403, not 401: the signature was good. The answer is "not right now".
        assert (
            _deliver(c, "gh", "push", _push("ATH-1 x"), source["secret"]).status_code
            == 403
        )


# --- the endpoint: the unauthenticated route is rate limited -----------------


def test_a_delivery_burst_is_rate_limited(tmp_path):
    # The forge route is the ONE endpoint with no actor to charge, so it charges
    # the anonymous per-IP limiter itself — otherwise enumeration probes and
    # 512KB-body HMAC work are free. Charged BEFORE the body read, so even valid
    # deliveries 429 once the budget is spent.
    app = create_app(tmp_path / "burst.db", anon_rate_limit_per_minute=2)
    with TestClient(app) as c:
        _seed_operator(tmp_path / "burst.db")
        source = _source(c)
        _project_issue(c)
        for _ in range(2):
            assert (
                _deliver(
                    c, "gh", "push", _push("ATH-1 x"), source["secret"]
                ).status_code
                == 202
            )
        limited = _deliver(c, "gh", "push", _push("ATH-1 x"), source["secret"])
        assert limited.status_code == 429
        assert limited.json() == {"detail": "anonymous rate limit exceeded"}
        assert 1 <= int(limited.headers["retry-after"]) <= 60


def test_a_burst_against_an_unknown_or_paused_source_is_rate_limited(tmp_path):
    # The limiter is charged BEFORE the source lookup too: hammering an unknown
    # (or paused) source name 429s instead of doing a database read plus a
    # stand-in-secret HMAC per attempt.
    app = create_app(tmp_path / "burst2.db", anon_rate_limit_per_minute=2)
    with TestClient(app) as c:
        _seed_operator(tmp_path / "burst2.db")
        source = _source(c)
        c.put(
            f"/event-sources/{source['id']}/enabled",
            json={"enabled": False},
            headers=H1,
        )
        # Unknown source: 401 (indistinguishable from a bad signature) ...
        assert (
            _deliver(c, "nosuch", "push", _push("ATH-1 x"), "whatever").status_code
            == 401
        )
        # ... paused source: 403 — and the THIRD attempt, whichever it names, is
        # a 429 that never touches the database.
        assert (
            _deliver(c, "gh", "push", _push("ATH-1 x"), source["secret"]).status_code
            == 403
        )
        assert (
            _deliver(c, "gh", "push", _push("ATH-1 x"), source["secret"]).status_code
            == 429
        )


def test_the_forge_limit_is_off_by_default(tmp_path):
    # Same default as every other anonymous path: with no anon limit configured,
    # deliveries are unthrottled so a busy forge is never refused by a limit the
    # operator never set.
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        _project_issue(c)
        for _ in range(6):
            assert (
                _deliver(
                    c, "gh", "push", _push("ATH-1 x"), source["secret"]
                ).status_code
                == 202
            )


def test_registering_a_source_is_admin_only_and_the_secret_is_shown_once(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = c.post(
            "/users",
            json={"email": "b@e.com", "name": "Bob", "password": "pw"},
            headers=H1,
        ).json()
        assert (
            c.post(
                "/event-sources",
                json={"name": "x"},
                headers={"X-Athena-Actor": str(member["id"])},
            ).status_code
            == 403
        )

        source = _source(c)
        assert source["secret"].startswith("evtsec_")
        # Readable exactly once. A listing that echoed it would turn every admin
        # read into a credential disclosure.
        listed = c.get("/event-sources", headers=H1).json()
        assert [row["name"] for row in listed] == ["gh"]
        assert "secret" not in listed[0]


def test_source_crud_requires_an_admin_scoped_token_not_just_the_role(tmp_path):
    # WHY: a bearer token's SCOPES bound what the admin holding it may do — the
    # same boundary every parallel admin surface (webhooks, security, approvals,
    # automation, users) enforces through require_admin. Before this fix the
    # event-source routes checked only the admin ROLE, so an admin's read-scoped
    # token could register a source and walk off with its evtsec_ signing secret.
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)  # registered by the header-trusted admin (control)
        raw = c.post(
            "/tokens", json={"name": "ro", "scopes": ["read"]}, headers=H1
        ).json()["token"]
        bearer = {"Authorization": f"Bearer {raw}"}

        refused = [
            c.get("/event-sources", headers=bearer),
            c.post("/event-sources", json={"name": "sneaky"}, headers=bearer),
            c.put(
                f"/event-sources/{source['id']}/enabled",
                json={"enabled": False},
                headers=bearer,
            ),
            c.delete(f"/event-sources/{source['id']}", headers=bearer),
        ]
        assert [r.status_code for r in refused] == [403, 403, 403, 403]
        assert all(r.json()["detail"] == "token scope required: admin" for r in refused)
        # Nothing landed: no second source, the original still enabled and present.
        listed = c.get("/event-sources", headers=H1).json()
        assert [row["name"] for row in listed] == ["gh"]
        assert listed[0]["enabled"] is True
        # The parallel admin surface already refused this token the same way —
        # this test mirrors that boundary onto the forge routes.
        assert (
            c.post(
                "/webhooks", json={"url": "http://localhost:9/x"}, headers=bearer
            ).status_code
            == 403
        )


def test_hardened_mode_ignores_the_spoofable_actor_header(tmp_path, monkeypatch):
    # WHY: TRUST_ACTOR_HEADER defaults OFF in production (conftest re-enables it
    # to model the trusted local box). Registering a source hands out a credential
    # that writes history, so in hardened mode the management routes must refuse
    # the spoofable header outright — not mint a secret for it.
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)  # registered while the suite's trusted-box flag is on
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)

        assert (
            c.post("/event-sources", json={"name": "spoofed"}, headers=H1).status_code
            == 401
        )
        assert c.get("/event-sources", headers=H1).status_code == 401
        assert (
            c.put(
                f"/event-sources/{source['id']}/enabled",
                json={"enabled": False},
                headers=H1,
            ).status_code
            == 401
        )
        assert c.delete(f"/event-sources/{source['id']}", headers=H1).status_code == 401


def test_hardened_mode_still_trusts_the_signature_not_the_header(tmp_path, monkeypatch):
    # WHY: the delivery route is unauthenticated BY DESIGN — its credential is the
    # HMAC over the exact body, checked before parsing. Hardened mode must not
    # break a real delivery, and a spoofed actor header must not substitute for
    # the signature.
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        issue = _project_issue(c)
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)

        # No actor header at all: a correctly signed delivery still lands.
        landed = _deliver(c, "gh", "push", _push("fix ATH-1"), source["secret"])
        assert landed.status_code == 202
        assert landed.json()["issues"] == [issue["id"]]

        # A spoofed header alongside a wrong signature is the same flat refusal.
        body = json.dumps(_push("fix ATH-1")).encode()
        refused = c.post(
            "/forge/gh",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign("wrong-secret", body),
                "X-GitHub-Event": "push",
                "X-Athena-Actor": "1",
            },
        )
        assert refused.status_code == 401
        assert refused.json()["detail"] == "invalid signature"


def test_an_unknown_source_kind_cannot_be_registered(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        # Closed at registration, so a request can never arrive for a parser that
        # does not exist.
        assert (
            c.post(
                "/event-sources", json={"name": "x", "kind": "gitlab"}, headers=H1
            ).status_code
            == 422
        )


def test_a_duplicate_source_name_is_refused(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        _source(c)
        assert (
            c.post("/event-sources", json={"name": "gh"}, headers=H1).status_code == 409
        )


# --- the landing rule -------------------------------------------------------


def test_an_event_naming_a_real_issue_lands_as_imported_history(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        issue = _project_issue(c)
        resp = _deliver(
            c, "gh", "push", _push("ATH-1 fix the redirect"), source["secret"]
        )
        assert resp.status_code == 202
        assert resp.json()["landed"] == 1

        events = c.get(
            f"/activity?target_kind=issue&target_id={issue['id']}", headers=H1
        ).json()
        landed = next(e for e in events if e["verb"] == "forge_commit")
        # Foreign history: something Athena was TOLD, not something it did.
        assert landed["imported_at"] is not None
        # And belonging to no Athena run, so it can never be spliced into a
        # replay.
        assert landed["run_id"] is None
        assert "gh:" in landed["detail"]


def test_a_forge_event_cannot_be_undone(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        issue = _project_issue(c)
        _deliver(c, "gh", "push", _push("ATH-1 x"), source["secret"])
        events = c.get(
            f"/activity?target_kind=issue&target_id={issue['id']}", headers=H1
        ).json()
        landed = next(e for e in events if e["verb"] == "forge_commit")
        refused = c.post(f"/activity/{landed['id']}/undo", headers=H1)
        # 0041's guarantee, inherited rather than re-derived: undo refuses
        # imported events, so a forge cannot be reversed into a native write.
        assert refused.status_code == 422
        assert refused.json()["code"] == "undo_imported_event"


def test_a_forge_event_does_not_move_the_issue(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        issue = _project_issue(c)
        before = c.get(f"/issues/{issue['id']}", headers=H1).json()
        _deliver(
            c,
            "gh",
            "pull_request",
            {
                "action": "closed",
                "pull_request": {
                    "number": 1,
                    "title": "ATH-1 done",
                    "merged": True,
                    "html_url": "https://github.com/o/r/pull/1",
                },
            },
            source["secret"],
        )
        after = c.get(f"/issues/{issue['id']}", headers=H1).json()
        # A merged PR is a claim, not a state transition. Athena integrates with
        # a forge; it does not take instructions from one.
        assert after["status"] == before["status"]


def test_an_event_naming_nothing_real_is_counted_not_stored(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        _project_issue(c)
        resp = _deliver(
            c,
            "gh",
            "push",
            {
                "ref": "refs/heads/main",
                "commits": [
                    {"message": "encode as UTF-8 per ISO-8601", "url": "u"},
                    {"message": "ATH-999 was never allocated", "url": "u"},
                ],
            },
            source["secret"],
        )
        # Authentic, understood, and about nothing this workspace tracks. Landing
        # it nowhere would be silent; storing it somewhere would be noise.
        assert resp.status_code == 202
        assert resp.json() == {
            "source": "gh",
            "event": "push",
            "landed": 0,
            "unmatched": 2,
            "issues": [],
        }
        health = c.get("/event-sources", headers=H1).json()[0]
        assert health["unmatched_count"] == 2
        assert health["accepted_count"] == 0


def test_one_delivery_can_land_on_several_issues(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        first = _project_issue(c)
        second = c.post(
            "/issues",
            json={"title": "Second", "project_id": 1},
            headers=H1,
        ).json()
        resp = _deliver(
            c, "gh", "push", _push("ATH-1 and ATH-2 together"), source["secret"]
        )
        assert sorted(resp.json()["issues"]) == sorted([first["id"], second["id"]])


def test_a_delivery_is_bounded_in_how_many_issues_it_can_touch(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        _project_issue(c)
        for n in range(forge_events.MAX_ISSUES_PER_DELIVERY + 3):
            c.post(
                "/issues",
                json={"title": f"Issue {n}", "project_id": 1},
                headers=H1,
            )
        keys = " ".join(
            f"ATH-{n}" for n in range(1, forge_events.MAX_ISSUES_PER_DELIVERY + 4)
        )
        resp = _deliver(c, "gh", "push", _push(keys), source["secret"]).json()
        # A payload naming a dozen issues is a mass rename or an attempt to spray
        # the trail; the overflow is counted so the operator still sees it.
        assert resp["landed"] == forge_events.MAX_ISSUES_PER_DELIVERY
        assert resp["unmatched"] > 0


def test_the_same_fact_lands_once_even_when_branch_and_message_agree(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        issue = _project_issue(c)
        _deliver(
            c,
            "gh",
            "push",
            _push("ATH-1 fix it", ref="refs/heads/ATH-1-fix"),
            source["secret"],
        )
        events = c.get(
            f"/activity?target_kind=issue&target_id={issue['id']}", headers=H1
        ).json()
        assert len([e for e in events if e["verb"] == "forge_commit"]) == 1


def test_deleting_a_source_keeps_the_history_it_landed(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        issue = _project_issue(c)
        _deliver(c, "gh", "push", _push("ATH-1 x"), source["secret"])
        assert c.delete(f"/event-sources/{source['id']}", headers=H1).status_code == 204
        events = c.get(
            f"/activity?target_kind=issue&target_id={issue['id']}", headers=H1
        ).json()
        # Those events happened and were authentic when recorded. Revoking a
        # credential is not a reason to rewrite the trail.
        assert any(e["verb"] == "forge_commit" for e in events)
        # And the revoked secret no longer works.
        assert (
            _deliver(c, "gh", "push", _push("ATH-1 y"), source["secret"]).status_code
            == 401
        )


def test_the_source_lifecycle_is_audited(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        _bootstrap(c)
        source = _source(c)
        c.put(
            f"/event-sources/{source['id']}/enabled",
            json={"enabled": False},
            headers=H1,
        )
        events = c.get(
            f"/activity?target_kind=event_source&target_id={source['id']}", headers=H1
        ).json()
        verbs = {e["verb"] for e in events}
        assert {"registered_event_source", "paused_event_source"} <= verbs
        # The credential is never written to the trail.
        assert all(source["secret"] not in (e["detail"] or "") for e in events)


# --- rendering --------------------------------------------------------------


def test_a_forge_url_links_only_on_a_registered_host(tmp_path):
    from athena.web.render import render_forge_detail

    detail = "gh: commit — https://github.com/o/r/commit/abc"
    linked = str(render_forge_detail(detail, {"github.com"}))
    assert '<a href="https://github.com/o/r/commit/abc"' in linked
    assert 'rel="noopener noreferrer nofollow"' in linked

    # A URL on a host nobody registered stays inert: otherwise anyone holding a
    # source secret could plant an arbitrary outbound link on an issue's trail.
    other = str(render_forge_detail("gh: see https://evil.example/x", {"github.com"}))
    assert "<a " not in other
    # And with no sources registered at all, nothing links.
    assert "<a " not in str(render_forge_detail(detail, set()))


def test_forge_detail_escapes_before_it_links():
    from athena.web.render import render_forge_detail

    rendered = str(
        render_forge_detail(
            "gh: <script>alert(1)</script> https://github.com/x", {"github.com"}
        )
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert '<a href="https://github.com/x"' in rendered


def test_the_issue_trail_renders_a_forge_event_as_a_link(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        source = _source(c)
        issue = _project_issue(c)
        _deliver(c, "gh", "push", _push("ATH-1 fix it"), source["secret"])
    with TestClient(app) as browser:
        browser.post("/login", data={"email": "a@e.com", "password": "pw"})
        html = browser.get(f"/aegis/issues/{issue['id']}").text
        # "reported" — the trail says this is something Athena was told.
        assert "reported" in html
        assert '<a href="https://github.com/o/r/commit/abc"' in html


def test_forge_detail_url_matching_is_linear_and_keeps_punctuation_out(tmp_path):
    """WHY: the URL regex ended with a character class that was a SUBSET of the
    repeated one, so every character could be matched by either part and a long
    run backtracked quadratically — a polynomial-ReDoS shape on text an outside
    system supplied (a forge detail is stranger-controlled). One unambiguous
    quantifier plus an explicit tail trim renders identically in linear time."""
    import time

    from athena.web.render import _URL_RE, render_forge_detail

    hosts = ["forge.example"]
    # Sentence punctuation stays OUT of the href but stays ON the page.
    rendered = str(render_forge_detail("see https://forge.example/a/b.", hosts))
    assert 'href="https://forge.example/a/b"' in rendered
    assert rendered.endswith(".")
    assert "/a/b." not in rendered.split('href="')[1].split('"')[0]
    # A registered host inside parens links without swallowing the bracket.
    wrapped = str(render_forge_detail("(https://forge.example/x)", hosts))
    assert 'href="https://forge.example/x"' in wrapped and wrapped.endswith(")")
    # An unregistered host is still inert text.
    assert "<a" not in str(render_forge_detail("https://other.example/y", hosts))

    # Linear, not quadratic: quadrupling the input must not square the time.
    def scan(size: int) -> float:
        payload = "https://" + "a" * size + " "
        start = time.perf_counter()
        _URL_RE.findall(payload)
        return time.perf_counter() - start

    scan(4000)  # warm up the pattern cache
    small, large = scan(4000), scan(16000)
    # 4x the input; quadratic would be ~16x. Generous bound so a loaded CI box
    # cannot flake it, while still failing loudly on a re-introduced blowup.
    assert large < max(small * 8, 0.05), (small, large)
