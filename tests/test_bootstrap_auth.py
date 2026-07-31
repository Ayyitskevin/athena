"""Tests for the bootstrap-authentication hardening (pre-hosting).

The contract these encode:

- The X-Athena-Actor header is NOT trusted by default. An unconfigured instance
  exposed to the network must reject a spoofed identity header — only a real
  bearer token (or an explicitly enabled header on a trusted box) authenticates.
- First-run bootstrap requires a separate high-entropy credential from process
  configuration. Empty configuration, a missing header, and a wrong header all
  refuse with the same 401 as a post-bootstrap anonymous request.
- The actor header can be explicitly enabled to mint the first user's first token.
- After bootstrap, user management (create/list/show) requires authentication,
  so an exposed instance can't be enumerated or have accounts minted anonymously.

The suite-wide conftest enables the header by default (it models a trusted local
box); tests here that assert the locked-down default flip it back OFF explicitly.
"""

from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient
import pytest

from athena import config
from athena.core import oidc_flow
from athena.core import (
    activity,
    db,
    deployment,
    passwords,
    user_commands,
    users,
    users_api,
)
from athena.main import create_app

BOOTSTRAP_TOKEN = "test-bootstrap-token-0000000000000001"
BOOTSTRAP_HEADERS = {users_api.BOOTSTRAP_TOKEN_HEADER: BOOTSTRAP_TOKEN}


@pytest.fixture(autouse=True)
def bootstrap_enabled_for_unrelated_tests(monkeypatch):
    """Override conftest's injection: this module tests the real credential gate."""
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", BOOTSTRAP_TOKEN)


def _bootstrap_user(client, email="boot@e.com", name="Boot") -> int:
    """Create the first user with the configured one-time credential."""
    r = client.post(
        "/users",
        json={"email": email, "name": name},
        headers=BOOTSTRAP_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- the locked-down default ---------------------------------------------------


def test_default_rejects_actor_header_spoof(tmp_path, monkeypatch):
    # WHY (load-bearing): with the header untrusted, claiming to be user 1 proves
    # nothing. A write authenticated only by X-Athena-Actor must be rejected.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "spoof.db")
    with TestClient(app) as client:
        uid = _bootstrap_user(client)
        spoof = client.post(
            "/issues",
            json={"title": "as someone else"},
            headers={"X-Athena-Actor": str(uid)},
        )
        assert spoof.status_code == 401


def test_explicit_enable_allows_actor_header(tmp_path, monkeypatch):
    # WHY: the header path is not removed — on a trusted box you may enable it
    # (the bootstrap mode). Then the same write authenticates and succeeds.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
    app = create_app(tmp_path / "enabled.db")
    with TestClient(app) as client:
        uid = _bootstrap_user(client)
        ok = client.post(
            "/issues",
            json={"title": "on a trusted box"},
            headers={"X-Athena-Actor": str(uid)},
        )
        assert ok.status_code == 201
        assert ok.json()["created_by"] == uid


# --- first-run bootstrap is credentialed --------------------------------------


def test_bootstrap_is_disabled_when_the_token_is_unconfigured(tmp_path, monkeypatch):
    # WHY (load-bearing): the default empty value must close the administrator
    # grant. Starting an empty database on a reachable interface cannot make the
    # first network caller its owner.
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    app = create_app(tmp_path / "disabled.db")
    with TestClient(app) as client:
        response = client.post(
            "/users",
            json={"email": "attacker@e.com", "name": "Attacker"},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}

    conn = db.connect(tmp_path / "disabled.db")
    try:
        assert users.count_users(conn) == 0
        assert activity.list_activity(conn, limit=10) == []
    finally:
        conn.close()


def test_supported_launcher_bootstrap_requires_a_persistent_admin_password(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "BOOTSTRAP_PASSWORD_REQUIRED", True)
    app = create_app(tmp_path / "password-required.db")
    with TestClient(app) as client:
        refused = client.post(
            "/users",
            json={"email": "admin@e.com", "name": "Admin"},
            headers=BOOTSTRAP_HEADERS,
        )
        whitespace_refused = client.post(
            "/users",
            json={
                "email": "admin@e.com",
                "name": "Admin",
                "password": " \t ",
            },
            headers=BOOTSTRAP_HEADERS,
        )
        accepted = client.post(
            "/users",
            json={
                "email": "admin@e.com",
                "name": "Admin",
                "password": "persistent-password",
            },
            headers=BOOTSTRAP_HEADERS,
        )

    assert refused.status_code == 422
    assert refused.json() == {
        "detail": "athena-serve bootstrap requires an administrator password"
    }
    assert whitespace_refused.status_code == 422
    assert whitespace_refused.json() == refused.json()
    assert accepted.status_code == 201
    assert accepted.json()["role"] == "admin"


def test_supported_body_floor_carries_maximally_escaped_bootstrap_password_to_login(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "BOOTSTRAP_PASSWORD_REQUIRED", True)
    password = "&" * passwords.MAX_PASSWORD_BYTES
    app = create_app(
        tmp_path / "encoded-password.db",
        max_request_body_bytes=deployment.MIN_SUPPORTED_REQUEST_BODY_BYTES,
    )
    with TestClient(app) as client:
        created = client.post(
            "/users",
            json={
                "email": "admin@example.com",
                "name": "Admin",
                "password": password,
            },
            headers=BOOTSTRAP_HEADERS,
        )
        logged_in = client.post(
            "/login",
            data={"email": "admin@example.com", "password": password},
            follow_redirects=False,
        )

    assert created.status_code == 201, created.text
    assert logged_in.status_code == 303, logged_in.text


def test_bootstrap_rejects_a_password_outside_the_supported_envelope(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "BOOTSTRAP_PASSWORD_REQUIRED", True)
    app = create_app(tmp_path / "oversized-password.db")
    with TestClient(app) as client:
        response = client.post(
            "/users",
            json={
                "email": "admin@example.com",
                "name": "Admin",
                "password": "x" * (passwords.MAX_PASSWORD_BYTES + 1),
            },
            headers=BOOTSTRAP_HEADERS,
        )

    assert response.status_code == 422
    assert "x" * (passwords.MAX_PASSWORD_BYTES + 1) not in response.text
    assert response.json() == {
        "detail": f"password must be at most {passwords.MAX_PASSWORD_BYTES} UTF-8 bytes"
    }
    conn = db.connect(tmp_path / "oversized-password.db")
    try:
        assert users.count_users(conn) == 0
    finally:
        conn.close()


def test_bootstrap_rejects_a_password_the_browser_input_cannot_reproduce(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "BOOTSTRAP_PASSWORD_REQUIRED", True)
    db_path = tmp_path / "line-break-password.db"
    secret = "line\nbreak-secret"
    app = create_app(db_path)
    with TestClient(app) as client:
        response = client.post(
            "/users",
            json={
                "email": "admin@example.com",
                "name": "Admin",
                "password": secret,
            },
            headers=BOOTSTRAP_HEADERS,
        )

    assert response.status_code == 422
    assert secret not in response.text
    assert response.json() == {
        "detail": "password must not contain carriage returns or line feeds"
    }
    conn = db.connect(db_path)
    try:
        assert users.count_users(conn) == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "body",
    (
        {"email": "", "name": "Admin", "password": "persistent-password"},
        {"email": " \t ", "name": "Admin", "password": "persistent-password"},
        {"email": "admin", "name": "Admin", "password": "persistent-password"},
        {
            "email": " admin@example.com ",
            "name": "Admin",
            "password": "persistent-password",
        },
        {"email": "admin@example.com", "name": "", "password": "persistent-password"},
        {
            "email": "admin@example.com",
            "name": " \t ",
            "password": "persistent-password",
        },
    ),
)
def test_bootstrap_rejects_an_identity_the_login_contract_cannot_address(
    tmp_path, monkeypatch, body
):
    monkeypatch.setattr(config, "BOOTSTRAP_PASSWORD_REQUIRED", True)
    db_path = tmp_path / "blank-identity.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        response = client.post("/users", json=body, headers=BOOTSTRAP_HEADERS)

    assert response.status_code == 422
    conn = db.connect(db_path)
    try:
        assert users.count_users(conn) == 0
    finally:
        conn.close()


def test_bootstrap_missing_and_wrong_tokens_share_the_normal_401(tmp_path):
    # WHY: a failed bootstrap credential must not disclose whether a token is
    # configured or whether the database is still empty.
    app = create_app(tmp_path / "wrong.db")
    body = {"email": "attacker@e.com", "name": "Attacker"}
    with TestClient(app) as client:
        missing = client.post("/users", json=body)
        wrong = client.post(
            "/users",
            json=body,
            headers={
                users_api.BOOTSTRAP_TOKEN_HEADER: "wrong-token-00000000000000000000"
            },
        )
        correct = client.post("/users", json=body, headers=BOOTSTRAP_HEADERS)

    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json() == {"detail": "authentication required"}
    assert correct.status_code == 201
    assert correct.json()["role"] == users.ADMIN_ROLE


def test_bootstrap_duplicate_headers_fail_closed_in_either_order(tmp_path):
    # WHY: intermediaries disagree about whether duplicate headers are preserved,
    # combined, or first/last-value-wins. Accepting either interpretation could
    # turn one request into different credentials at different hops.
    app = create_app(tmp_path / "duplicate-header.db")
    body = {"email": "attacker@e.com", "name": "Attacker"}
    wrong = "wrong-token-00000000000000000000"
    with TestClient(app) as client:
        correct_first = client.post(
            "/users",
            json=body,
            headers=[
                (users_api.BOOTSTRAP_TOKEN_HEADER, BOOTSTRAP_TOKEN),
                (users_api.BOOTSTRAP_TOKEN_HEADER, wrong),
            ],
        )
        wrong_first = client.post(
            "/users",
            json=body,
            headers=[
                (users_api.BOOTSTRAP_TOKEN_HEADER, wrong),
                (users_api.BOOTSTRAP_TOKEN_HEADER, BOOTSTRAP_TOKEN),
            ],
        )

    for response in (correct_first, wrong_first):
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication required"}
    conn = db.connect(tmp_path / "duplicate-header.db")
    try:
        assert users.count_users(conn) == 0
    finally:
        conn.close()


def test_bootstrap_idempotency_rejection_does_not_reveal_state_or_token(tmp_path):
    # WHY: the retry middleware cannot bind a receipt to a not-yet-created actor.
    # Reject the key explicitly, but make the response independent of database
    # state and whether the bootstrap credential is missing, wrong, or correct.
    app = create_app(tmp_path / "bootstrap-idempotency.db")
    body = {"email": "admin@e.com", "name": "Admin"}
    keyed = {"Idempotency-Key": "bootstrap-attempt"}
    wrong = {
        **keyed,
        users_api.BOOTSTRAP_TOKEN_HEADER: "wrong-token-00000000000000000000",
    }
    correct = {**keyed, **BOOTSTRAP_HEADERS}
    with TestClient(app) as client:
        empty_responses = [
            client.post("/users", json=body, headers=keyed),
            client.post("/users", json=body, headers=wrong),
            client.post("/users", json=body, headers=correct),
        ]
        assert _bootstrap_user(client, "admin@e.com", "Admin") == 1
        populated_responses = [
            client.post(
                "/users",
                json={"email": "blocked@e.com", "name": "Blocked"},
                headers=headers,
            )
            for headers in (keyed, wrong, correct)
        ]

    expected = {"detail": "authentication required"}
    for response in [*empty_responses, *populated_responses]:
        assert response.status_code == 401
        assert response.json() == expected
    conn = db.connect(tmp_path / "bootstrap-idempotency.db")
    try:
        assert users.count_users(conn) == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM idempotency_keys "
                "WHERE idempotency_key = 'bootstrap-attempt'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_bootstrap_body_validation_is_credential_independent(tmp_path):
    # WHY: malformed input must not reach the administrator grant, and its schema
    # refusal must not become a bootstrap-token or database-state oracle.
    app = create_app(tmp_path / "malformed-body.db")
    invalid_body = {"email": "missing-name@example.com"}
    with TestClient(app) as client:
        responses = [
            client.post("/users", json=invalid_body),
            client.post(
                "/users",
                json=invalid_body,
                headers={
                    users_api.BOOTSTRAP_TOKEN_HEADER: (
                        "wrong-token-00000000000000000000"
                    )
                },
            ),
            client.post("/users", json=invalid_body, headers=BOOTSTRAP_HEADERS),
        ]

    assert {response.status_code for response in responses} == {422}
    assert responses[0].json() == responses[1].json() == responses[2].json()
    conn = db.connect(tmp_path / "malformed-body.db")
    try:
        assert users.count_users(conn) == 0
    finally:
        conn.close()


def test_bootstrap_credential_guesses_use_the_anonymous_rate_limit(tmp_path):
    # WHY: adding a special bootstrap credential must not create an unbounded
    # password-guessing path. It remains an anonymous request until it succeeds.
    app = create_app(
        tmp_path / "bootstrap-rate-limit.db",
        anon_rate_limit_per_minute=1,
    )
    body = {"email": "admin@e.com", "name": "Admin"}
    with TestClient(app) as client:
        wrong = client.post(
            "/users",
            json=body,
            headers={
                users_api.BOOTSTRAP_TOKEN_HEADER: ("wrong-token-00000000000000000000")
            },
        )
        bounded = client.post("/users", json=body, headers=BOOTSTRAP_HEADERS)

    assert wrong.status_code == 401
    assert bounded.status_code == 429
    assert bounded.json() == {"detail": "anonymous rate limit exceeded"}
    conn = db.connect(tmp_path / "bootstrap-rate-limit.db")
    try:
        assert users.count_users(conn) == 0
    finally:
        conn.close()


def test_bootstrap_matcher_treats_malformed_headers_as_failed_credentials():
    # WHY: compare_digest accepts only ASCII strings. A pathological header must
    # become a normal failed credential, never a 500.
    assert users_api._bootstrap_token_matches(None) is False
    assert users_api._bootstrap_token_matches("short") is False
    assert users_api._bootstrap_token_matches("é" * 32) is False
    assert users_api._bootstrap_token_matches("x" * 256) is False


def test_openapi_declares_the_bootstrap_header_without_making_it_globally_required(
    tmp_path,
):
    # WHY: client authors need the bootstrap credential to be discoverable, while
    # authenticated post-bootstrap user creation must not be forced to send it.
    operation = create_app(tmp_path / "openapi.db").openapi()["paths"]["/users"]["post"]
    matching = [
        parameter
        for parameter in operation["parameters"]
        if parameter["name"] == users_api.BOOTSTRAP_TOKEN_HEADER
    ]
    assert matching == [
        {
            "name": users_api.BOOTSTRAP_TOKEN_HEADER,
            "in": "header",
            "required": False,
            "schema": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "title": "X-Athena-Bootstrap-Token",
            },
        }
    ]


def test_bootstrap_token_grants_nothing_after_the_first_user(tmp_path):
    # WHY: the environment token is a one-time bootstrap credential, not a durable
    # administrator credential. Forgetting to restart must not let it add users.
    app = create_app(tmp_path / "one-time.db")
    with TestClient(app) as client:
        _bootstrap_user(client)
        response = client.post(
            "/users",
            json={"email": "second@e.com", "name": "Second"},
            headers=BOOTSTRAP_HEADERS,
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_first_user_bootstrap_works_with_header_off(tmp_path, monkeypatch):
    # WHY: bootstrap uses its own credential, not the spoofable actor-header
    # fallback. A fresh locked-down instance can create its first administrator
    # while actor-header trust remains off.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "first.db")
    with TestClient(app) as client:
        r = client.post(
            "/users",
            json={"email": "founder@e.com", "name": "Founder"},
            headers=BOOTSTRAP_HEADERS,
        )
        assert r.status_code == 201
        assert r.json()["id"] == 1


def test_concurrent_first_user_bootstrap_creates_exactly_one_admin(
    tmp_path, monkeypatch
):
    # WHY (load-bearing): the credentialed first-user exception is still a privilege
    # grant. Two valid requests may both read an empty database before either writes,
    # so eligibility and role selection must be rechecked under the write reservation.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    db_file = tmp_path / "bootstrap-race.db"
    app = create_app(db_file)

    real_bootstrap = user_commands.bootstrap_user
    both_observed_empty = threading.Barrier(2)

    def synchronized_bootstrap(*args, **kwargs):
        # Reaching this wrapper proves each route already took its unlocked empty-DB
        # branch. Release them together so the command lock is the deciding control.
        both_observed_empty.wait(timeout=5)
        return real_bootstrap(*args, **kwargs)

    monkeypatch.setattr(user_commands, "bootstrap_user", synchronized_bootstrap)

    with TestClient(app) as client:

        def create(email):
            return client.post(
                "/users",
                json={"email": email, "name": email},
                headers=BOOTSTRAP_HEADERS,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(create, "first-racer@e.com"),
                pool.submit(create, "second-racer@e.com"),
            ]
            responses = [future.result(timeout=10) for future in futures]

    assert sorted(response.status_code for response in responses) == [201, 401]
    assert {
        response.json()["detail"]
        for response in responses
        if response.status_code == 401
    } == {"authentication required"}

    conn = db.connect(db_file)
    try:
        durable_users = [
            dict(row)
            for row in conn.execute(
                "SELECT id, email, role FROM users ORDER BY id"
            ).fetchall()
        ]
        created_events = [
            event
            for event in activity.list_activity(conn, limit=10)
            if event["verb"] == user_commands.VERB_CREATED_USER
        ]
    finally:
        conn.close()

    assert len(durable_users) == 1
    assert durable_users[0]["role"] == "admin"
    assert durable_users[0]["email"] in {"first-racer@e.com", "second-racer@e.com"}
    assert len(created_events) == 1
    assert created_events[0]["actor_id"] == durable_users[0]["id"]
    assert created_events[0]["target_id"] == durable_users[0]["id"]


def test_concurrent_oidc_provisioning_cannot_consume_admin_bootstrap(
    tmp_path, monkeypatch
):
    # WHY: OIDC once provisioned a member through a separate connection after REST
    # observed an empty database. With the same email, that could make the pending
    # admin insert collide and strand the durable database with zero administrators.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    db_file = tmp_path / "bootstrap-oidc-race.db"
    app = create_app(db_file)

    real_bootstrap = user_commands.bootstrap_user

    def bootstrap_after_oidc_attempt(*args, **kwargs):
        oidc_conn = db.connect(db_file)
        try:
            with pytest.raises(
                oidc_flow.OidcError, match="administrator bootstrap is required"
            ):
                oidc_flow.provision_or_link(
                    oidc_conn,
                    issuer="https://identity.example.test",
                    claims={
                        "sub": "concurrent-member",
                        "email": "local-admin@e.com",
                        "email_verified": True,
                        "name": "OIDC Member",
                    },
                )
        finally:
            oidc_conn.close()
        return real_bootstrap(*args, **kwargs)

    monkeypatch.setattr(user_commands, "bootstrap_user", bootstrap_after_oidc_attempt)

    with TestClient(app) as client:
        response = client.post(
            "/users",
            json={"email": "local-admin@e.com", "name": "Local Admin"},
            headers=BOOTSTRAP_HEADERS,
        )

    assert response.status_code == 201, response.text
    assert response.json()["role"] == "admin"

    conn = db.connect(db_file)
    try:
        durable_users = [
            dict(row)
            for row in conn.execute(
                "SELECT email, role FROM users ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()

    assert durable_users == [{"email": "local-admin@e.com", "role": "admin"}]


def test_bootstrap_command_requires_a_top_level_write_transaction(tmp_path):
    # WHY: db.transaction uses only a savepoint when nested, so it cannot promise
    # that bootstrap's eligibility read acquired the writer reservation first.
    conn = db.connect(tmp_path / "nested-bootstrap.db")
    db.migrate(conn)
    try:
        conn.execute("BEGIN")
        with pytest.raises(RuntimeError, match="top-level transaction"):
            user_commands.bootstrap_user(
                conn, email="unsafe-admin@e.com", name="Unsafe Admin"
            )
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        conn.rollback()
        conn.close()


def test_bootstrap_then_enable_header_to_mint_first_token(tmp_path, monkeypatch):
    # WHY: the documented bootstrap flow end to end — create the first user, turn
    # the header on just long enough to mint that user's first bearer token, then
    # the token alone authenticates and the header can go back off.
    app = create_app(tmp_path / "flow.db")
    with TestClient(app) as client:
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        uid = _bootstrap_user(client, "admin@e.com", "Admin")

        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
        minted = client.post(
            "/tokens",
            json={"name": "bootstrap", "scopes": ["admin"]},
            headers={"X-Athena-Actor": str(uid)},
        )
        assert minted.status_code == 201
        raw = minted.json()["token"]

        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        ok = client.get("/tokens", headers={"Authorization": f"Bearer {raw}"})
        assert ok.status_code == 200


# --- user management requires auth after bootstrap ----------------------------


def test_second_user_create_requires_auth(tmp_path, monkeypatch):
    # WHY: once a user exists, anonymous account creation is the hole — an exposed
    # instance must not let a stranger add a user (and then bootstrap a token).
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "second.db")
    with TestClient(app) as client:
        _bootstrap_user(client)
        # No auth, and the header is untrusted: rejected.
        denied = client.post("/users", json={"email": "intruder@e.com", "name": "X"})
        assert denied.status_code == 401


def test_authenticated_user_management_works(tmp_path, monkeypatch):
    # WHY: with the header enabled (trusted box), an authenticated actor can fully
    # manage users — create a second, list, and show.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
    app = create_app(tmp_path / "manage.db")
    with TestClient(app) as client:
        _bootstrap_user(client)
        auth = {"X-Athena-Actor": "1"}

        second = client.post(
            "/users", json={"email": "colleague@e.com", "name": "Coll"}, headers=auth
        )
        assert second.status_code == 201

        listing = client.get("/users", headers=auth)
        assert listing.status_code == 200
        assert len(listing.json()) == 2

        show = client.get("/users/2", headers=auth)
        assert show.status_code == 200
        assert show.json()["email"] == "colleague@e.com"


def test_user_listing_requires_auth(tmp_path, monkeypatch):
    # WHY: don't let an exposed instance be enumerated anonymously.
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "enum.db")
    with TestClient(app) as client:
        _bootstrap_user(client)
        assert client.get("/users").status_code == 401
        assert client.get("/users/1").status_code == 401
