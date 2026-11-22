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

Tests that play an agent over the MCP protocol (the contract agent, the fleet
trial) build its server with ``mcp_server_for``, connect with ``mcp_sessions``,
call tools through a metered ``McpAgent`` and run with ``run_agents``.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Coroutine, Iterable, Mapping
import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core import activity, db, refusal_codes
from athena.main import create_app

if TYPE_CHECKING:  # the MCP helpers import these lazily: most tests never need them
    from mcp.client.session import ClientSession
    from mcp.server.fastmcp import FastMCP

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


def lapse_lease(db_file: Path, issue_id: int) -> None:
    """The clock passes the issue's lease expiry: its stored ``expires_at`` moves
    into the past, as ``tests/test_desk.py`` lapses a lease. The app has no
    injectable clock, and a test never sleeps."""
    conn = db.connect(db_file)
    try:
        conn.execute(
            "UPDATE issue_leases SET expires_at = '2000-01-01 00:00:00' "
            "WHERE issue_id = ?",
            (issue_id,),
        )
        conn.commit()
    finally:
        conn.close()


# --- scripted agents over the MCP protocol, metered ---------------------------------
#
# tests/test_contract_agent.py (one seat, A16) and tests/test_fleet_trial.py (a
# conductor and three seats, F1) drive the real tool surface the way a model does
# and count what each call costs. These are the pieces both use.


def mcp_server_for(
    app: FastAPI,
    onboarded: Mapping[str, Any],
    *,
    run_id: str,
    profile: str | None = None,
) -> FastMCP:
    """The server ``athena-mcp`` builds from the config Athena emitted for an
    onboarded agent: its token, its profile, its probed scopes, a fixed run id.

    ``profile`` replaces the emitted ``ATHENA_MCP_PROFILE``, as an operator who
    sets up a conductor does (onboarding prints ``seat``)."""
    from athena.mcp import server as mcp_server
    from athena.mcp.client import AthenaClient

    env = dict(onboarded["mcp_config"]["mcpServers"]["athena"]["env"])
    if profile is not None:
        env[mcp_server.PROFILE_ENV] = profile
    client = AthenaClient(
        client=TestClient(app, headers=bearer(env["ATHENA_TOKEN"])), run_id=run_id
    )
    return mcp_server.build_server(
        client,
        scopes=mcp_server.probe_scopes(client),
        profile=mcp_server.profile_from_env(env),
    )


@contextlib.asynccontextmanager
async def mcp_sessions(*servers: FastMCP) -> AsyncIterator[list[ClientSession]]:
    """One connected client session per server, in order, over the protocol."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async with contextlib.AsyncExitStack() as stack:
        yield [
            await stack.enter_async_context(
                create_connected_server_and_client_session(
                    server._mcp_server  # noqa: SLF001 — the protocol server under test
                )
            )
            for server in servers
        ]


def run_agents(play: Coroutine[Any, Any, Any]) -> Any:
    """Run a scripted play; a failure inside the sessions' task groups is raised
    as itself, not wrapped in one-member exception groups."""
    try:
        return asyncio.run(play)
    except BaseExceptionGroup as group:
        failure: BaseException = group
        while isinstance(failure, BaseExceptionGroup) and len(failure.exceptions) == 1:
            failure = failure.exceptions[0]
        raise failure from None


@dataclass
class Refusal:
    agent: str
    tool: str
    answer: dict[str, Any]
    followed: bool = False


@dataclass
class Meter:
    """Tool calls: how many, which tools, the bytes of each answer's text (UTF-8,
    what the client receives as content), each answer over its tool's declared
    response budget, and each refusal."""

    calls: int = 0
    bytes_received: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    over_budget: list[str] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)


def assert_registered_refusal(answer: Mapping[str, Any]) -> None:
    """A refusal's code is in the registry, with a status the entry allows, and its
    ``next`` is the one the registry names."""
    code, status = answer.get("code"), answer.get("status_code")
    if status is None:
        assert code in refusal_codes.MCP_REFUSAL_CODES, answer
    else:
        assert code in refusal_codes.REFUSAL_CODES, answer
        assert status in refusal_codes.REFUSAL_CODES[code].statuses, answer
    assert answer.get("next") == refusal_codes.next_call(code), answer


class McpAgent:
    """One account's MCP session: it calls tools the way a model does, only tools
    its profile carries, and every call is metered."""

    def __init__(
        self,
        name: str,
        user_id: int,
        session: ClientSession,
        meter: Meter,
        tools: frozenset[str],
    ):
        self.name = name
        self.user_id = user_id
        self.session = session
        self.meter = meter
        self.tools = tools

    async def attempt(self, tool: str, **arguments: Any) -> tuple[bool, dict]:
        from athena.mcp import server as mcp_server

        assert tool in self.tools, f"{self.name} called {tool}, not in its profile"
        result = await self.session.call_tool(tool, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        self.meter.calls += 1
        self.meter.tools[tool] += 1
        self.meter.bytes_received += len(text.encode("utf-8"))
        budget = mcp_server.response_budget(tool)
        if budget is not None and len(text) > budget:
            self.meter.over_budget.append(f"{tool}: {len(text)} > {budget}")
        answer = json.loads(text)
        assert isinstance(answer, dict), f"{tool} answered {type(answer).__name__}"
        if result.isError:
            assert_registered_refusal(answer)
            self.meter.refusals.append(Refusal(self.name, tool, answer))
        return not result.isError, answer

    async def call(self, tool: str, **arguments: Any) -> dict:
        ok, answer = await self.attempt(tool, **arguments)
        assert ok, f"{self.name}: {tool}({arguments}) refused: {answer}"
        return answer

    async def refused(self, code: str, tool: str, **arguments: Any) -> dict:
        ok, answer = await self.attempt(tool, **arguments)
        assert not ok and answer.get("code") == code, (tool, arguments, answer)
        return answer

    async def follow(self, answer: dict, **arguments: Any) -> dict:
        """Make the call the answer's ``next`` names, with arguments its hint
        describes; it must succeed and must be the registry's call."""
        named = answer["next"]
        assert named == refusal_codes.next_call(answer["code"]), answer
        result = await self.call(named["tool"], **arguments)
        for refusal in self.meter.refusals:
            if refusal.answer is answer:
                refusal.followed = True
        return result
