"""Where a new test starts: an app, its first user, more accounts, tokens, clients.

About a hundred test files once declared their own ``_bootstrap`` (and ``_app``,
``_agent``, ``_bearer``, ``_login``, ``_events``) with small, accidental
differences. New tests import these instead; ``tests/test_suite_conventions.py``
fails if the number of files declaring their own ``_bootstrap`` rises.

``tests/conftest.py`` already makes every app test-shaped: the trusted
``X-Athena-Actor`` header is on, ``POST /users`` carries the bootstrap credential,
password hashing is cheap and background loops are off. So the first user is a
plain ``POST /users``, and "act as user N" is a header.

Typical use::

    from support import ADMIN, bootstrap, make_app, mint_token, client_as

    def test_a_reader_cannot_write(tmp_path):
        app, db_file = make_app(tmp_path)
        with TestClient(app) as client:
            bootstrap(client)  # user 1, the admin; ADMIN acts as them
            reader = client_as(app, token=mint_token(client, scopes=["read"]))
            assert reader.post("/issues", json={"title": "x"}).status_code == 403
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core import activity, db
from athena.main import create_app

#: The password every helper gives a human account (agents have none).
PASSWORD = "pw"
ACTOR_HEADER = "X-Athena-Actor"


def as_user(user_id: int) -> dict[str, str]:
    """Headers that act as ``user_id`` through the trusted actor header."""
    return {ACTOR_HEADER: str(user_id)}


#: The first user, who is the admin. Most tests call this ``H1``.
ADMIN = as_user(1)


def make_app(tmp_path: Path, name: str = "athena.db") -> tuple[FastAPI, Path]:
    """A fresh app over its own database file; returns ``(app, db_file)``."""
    db_file = tmp_path / name
    return create_app(db_file), db_file


def bootstrap(
    client: TestClient,
    *,
    email: str = "a@e.com",
    name: str = "Ann",
    password: str | None = PASSWORD,
) -> dict[str, Any]:
    """Create the first user (id 1, always the admin) and return it.

    ``password=None`` creates a passwordless admin, which can still act through
    ``ADMIN`` but cannot sign in to the browser.
    """
    body: dict[str, Any] = {"email": email, "name": name}
    if password is not None:
        body["password"] = password
    response = client.post("/users", json=body)
    assert response.status_code == 201, response.text
    user = response.json()
    assert user["id"] == 1, "bootstrap() must create the first user"
    return user


def add_user(
    client: TestClient,
    email: str,
    *,
    name: str | None = None,
    role: str = "member",
    password: str | None = PASSWORD,
    headers: Mapping[str, str] = ADMIN,
) -> dict[str, Any]:
    """Create another human account (a member unless ``role`` says otherwise)."""
    body: dict[str, Any] = {
        "email": email,
        "name": name or email.split("@")[0],
        "role": role,
    }
    if password is not None:
        body["password"] = password
    response = client.post("/users", json=body, headers=dict(headers))
    assert response.status_code == 201, response.text
    return response.json()


#: Headers for the other two members of ``bootstrap_admin_creator_outsider()``.
CREATOR = as_user(2)
OUTSIDER = as_user(3)


def bootstrap_admin_creator_outsider(
    client: TestClient,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The access-control cast: admin (1), creator (2) and outsider (3).

    ``ADMIN``, ``CREATOR`` and ``OUTSIDER`` act as them; they sign in as
    ``admin@e.com``, ``c@e.com`` and ``o@e.com`` with ``PASSWORD``.
    """
    admin = bootstrap(client, email="admin@e.com", name="Admin")
    creator = add_user(client, "c@e.com", name="Creator")
    outsider = add_user(client, "o@e.com", name="Outsider")
    return admin, creator, outsider


def onboard_agent(
    client: TestClient,
    *,
    email: str = "sol@e.com",
    name: str = "Sol",
    scopes: Iterable[str] = ("read", "issue:write"),
    headers: Mapping[str, str] = ADMIN,
) -> dict[str, Any]:
    """Onboard an agent account with its first token, as the admin.

    Returns the ``POST /users/onboard_agent`` body: ``user``, ``token`` (with the
    raw ``token["token"]``) and ``mcp_config``. ``bearer()`` takes it as is.
    """
    response = client.post(
        "/users/onboard_agent",
        json={"email": email, "name": name, "scopes": list(scopes)},
        headers=dict(headers),
    )
    assert response.status_code == 201, response.text
    return response.json()


def mint_token(
    client: TestClient,
    *,
    scopes: Iterable[str],
    user_id: int = 1,
    name: str = "scoped",
) -> dict[str, Any]:
    """Mint a token with exactly ``scopes`` for ``user_id`` (``POST /tokens`` as them).

    Returns the created token, whose raw secret is ``["token"]``.
    """
    response = client.post(
        "/tokens",
        json={"name": name, "scopes": list(scopes)},
        headers=as_user(user_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def raw_token(token: str | Mapping[str, Any]) -> str:
    """The raw secret from a raw string, a minted token or an onboarded agent."""
    if isinstance(token, str):
        return token
    secret = token["token"]
    return secret if isinstance(secret, str) else secret["token"]


def bearer(token: str | Mapping[str, Any], *, run: str | None = None) -> dict[str, str]:
    """``Authorization`` headers for a token, optionally tagged with a run id."""
    headers = {"Authorization": f"Bearer {raw_token(token)}"}
    if run is not None:
        headers["X-Athena-Run"] = run
    return headers


def client_as(
    app: FastAPI,
    *,
    user_id: int | None = None,
    token: str | Mapping[str, Any] | None = None,
    run: str | None = None,
) -> TestClient:
    """A second client on ``app`` whose every request acts as one account.

    Give ``user_id`` (the actor header) or ``token`` (a bearer token), not both.
    Use it inside the ``with TestClient(app)`` block that started the app; it does
    not run the app's startup again.
    """
    if (user_id is None) == (token is None):
        raise ValueError("client_as() takes exactly one of user_id or token")
    if token is not None:
        headers = bearer(token, run=run)
    else:
        assert user_id is not None
        headers = as_user(user_id)
        if run is not None:
            headers["X-Athena-Run"] = run
    return TestClient(app, headers=headers)


def login(client: TestClient, email: str, password: str = PASSWORD) -> None:
    """Sign ``client`` in through the browser form and arm its CSRF header."""
    response = client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def events(db_file: Path, *verbs: str) -> list[dict[str, Any]]:
    """The whole activity trail, ungated and newest first; only ``verbs`` if given."""
    conn = db.connect(db_file)
    try:
        rows = activity.list_activity(conn, limit=1_000_000)
    finally:
        conn.close()
    return [row for row in rows if not verbs or row["verb"] in verbs]
