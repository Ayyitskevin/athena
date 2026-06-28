"""Idempotency-Key: replay the first response to a retried write.

Agent-native safety — an agent whose POST times out and is retried must not
double-create. When a POST carries an Idempotency-Key from an identifiable caller,
the first 2xx response is stored and replayed verbatim on a repeat. These pin the
replay (one create, identical body), per-key and per-identity scoping, the
no-header passthrough, that a non-2xx is NOT cached, and that a replay still carries
the security headers.
"""
from fastapi.testclient import TestClient

from athena import config
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "A"})
    client.post("/users", json={"email": "b@e.com", "name": "B", "role": "member"}, headers=H1)


def _ids(client):
    return sorted(i["id"] for i in client.get("/issues", headers=H1).json())


def test_same_key_replays_one_create(tmp_path):
    with TestClient(create_app(tmp_path / "replay.db")) as client:
        _bootstrap(client)
        key = {"Idempotency-Key": "make-issue-1", **H1}
        first = client.post("/issues", json={"title": "retry me"}, headers=key)
        second = client.post("/issues", json={"title": "retry me"}, headers=key)

        assert first.status_code == 201 and second.status_code == 201
        # The retry replays the first response verbatim, flagged as a replay.
        assert second.json() == first.json()
        assert second.headers.get("idempotent-replay") == "true"
        assert first.headers.get("idempotent-replay") is None
        # Exactly ONE issue was created.
        assert _ids(client) == [first.json()["id"]]
        # The replay still carries the security headers (it's inside harden_http).
        assert second.headers.get("x-content-type-options") == "nosniff"


def test_distinct_keys_create_distinctly(tmp_path):
    with TestClient(create_app(tmp_path / "distinct.db")) as client:
        _bootstrap(client)
        a = client.post("/issues", json={"title": "a"}, headers={"Idempotency-Key": "k1", **H1})
        b = client.post("/issues", json={"title": "b"}, headers={"Idempotency-Key": "k2", **H1})
        assert a.json()["id"] != b.json()["id"]
        assert _ids(client) == sorted([a.json()["id"], b.json()["id"]])


def test_no_header_is_normal(tmp_path):
    with TestClient(create_app(tmp_path / "nokey.db")) as client:
        _bootstrap(client)
        client.post("/issues", json={"title": "dup"}, headers=H1)
        client.post("/issues", json={"title": "dup"}, headers=H1)
        # No Idempotency-Key → two independent creates, untouched by the middleware.
        assert len(_ids(client)) == 2


def test_key_is_scoped_per_identity(tmp_path):
    with TestClient(create_app(tmp_path / "scope.db")) as client:
        _bootstrap(client)
        # The SAME key string from two different callers must not collide: each gets
        # its own create, and neither reads the other's stored response.
        mine = client.post("/issues", json={"title": "mine"}, headers={"Idempotency-Key": "shared", **H1})
        theirs = client.post("/issues", json={"title": "theirs"}, headers={"Idempotency-Key": "shared", **H2})
        assert mine.json()["id"] != theirs.json()["id"]
        assert theirs.headers.get("idempotent-replay") is None
        assert len(_ids(client)) == 2


def test_non_2xx_is_not_cached(tmp_path):
    with TestClient(create_app(tmp_path / "err.db")) as client:
        _bootstrap(client)
        key = {"Idempotency-Key": "first-failed", **H1}
        # A blank title is a 422 — a failed write, which must NOT be cached.
        bad = client.post("/issues", json={"title": "   "}, headers=key)
        assert bad.status_code == 422
        # Retrying the same key with a valid body actually creates (not a replay of
        # the error), because the failure was never stored.
        good = client.post("/issues", json={"title": "now valid"}, headers=key)
        assert good.status_code == 201
        assert good.headers.get("idempotent-replay") is None
        assert _ids(client) == [good.json()["id"]]


def test_unidentified_caller_is_passed_through(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path / "anon.db")) as client:
        _bootstrap(client)
        # With the actor header untrusted and no bearer token, there's no identity to
        # scope a key to — the request isn't deduped, it's just handled (401 here).
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        r = client.post("/issues", json={"title": "x"}, headers={"Idempotency-Key": "k"})
        assert r.status_code == 401
