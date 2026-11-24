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
    profile: str | None = None,
) -> dict[str, Any]:
    """Onboard an agent account with its first token, as the admin.

    Returns the ``POST /users/onboard_agent`` body: ``user``, ``token`` (with the
    raw ``token["token"]``) and ``mcp_config``. ``bearer()`` takes it as is.
    ``profile`` is the MCP profile the printed config names (omitted: the seat
    profile the scopes suggest).
    """
    body: dict[str, Any] = {"email": email, "name": name, "scopes": list(scopes)}
    if profile is not None:
        body["profile"] = profile
    response = client.post("/users/onboard_agent", json=body, headers=dict(headers))
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


def index_search_without_entry_keys(monkeypatch: Any) -> None:
    """Index search as the code before the search entry map did (H2): delete a
    source's entry by kind and source id, insert, keep no map.

    For a test that writes rows into a database migrated only to an inventory
    older than the map's migration: today's ``search.index_document`` needs that
    table, and the code that ran at that schema did not."""
    from athena.core import search

    def index_document(conn, *, kind, source_id, commit):
        spec = search._SOURCE[kind]
        title_col, body_col = spec["title"], spec["body"]
        cols = body_col if title_col is None else f"{title_col}, {body_col}"
        row = conn.execute(
            f"SELECT {cols} FROM {spec['table']} WHERE id = ?", (source_id,)
        ).fetchone()
        conn.execute(
            "DELETE FROM search_index WHERE kind = ? AND source_id = ?",
            (kind, source_id),
        )
        if row is not None:
            conn.execute(
                "INSERT INTO search_index (kind, source_id, title, body) "
                "VALUES (?, ?, ?, ?)",
                (
                    kind,
                    source_id,
                    "" if title_col is None else row[title_col],
                    row[body_col],
                ),
            )
        if commit:
            conn.commit()

    monkeypatch.setattr(search, "index_document", index_document)


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
    edits a printed config does; onboarding with ``profile`` prints it instead."""
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


# --- a desk with every lane at its declared maximum ---------------------------------
#
# tests/test_desk_budget.py and tests/test_mcp_response_budget.py (F3) measure the
# MCP answers against a workspace where one seat's desk has every lane full, each
# text at the longest its write path declares. The numbers come from the code that
# declares them, so a lane that grows is measured at its new size.


def _desk_maxima() -> dict[str, int]:
    from athena.aegis import (
        desk,
        issue_drafts,
        lease_commands,
        ready_queue,
        review_request_projection,
        run_state,
        work_progress,
    )
    from athena.core import agent_notebook, run_control_commands

    return {
        "title_chars": issue_drafts.MAX_TITLE_CHARS,
        "ready": ready_queue.READY_LIMIT,
        "review_requests": review_request_projection.DESK_LIMIT,
        "run_controls": desk.CONTROLS_LIMIT,
        "run_control_payload_chars": run_control_commands.MAX_PAYLOAD_CHARS,
        "worker_kill_requests": desk.WORKER_STOPS_LIMIT,
        "claim_handoffs": desk.HANDOFFS_LIMIT,
        "leases": desk.LEASES_LIMIT,
        "questions": run_state.LANE_LIMIT,
        "waiting_on": work_progress.LANE_LIMIT,
        "notifications": desk.NOTIFICATIONS_LIMIT,
        "delegations": desk.DELEGATIONS_LIMIT,
        "notebook_lines": agent_notebook.NOTEBOOK_EXCERPT_LINES,
        "note_chars": lease_commands.MAX_CLAIM_YIELD_NOTE_CHARS,
        "attempted_work_chars": lease_commands.MAX_HANDOFF_ATTEMPTED_WORK_CHARS,
        "evidence_item_chars": lease_commands.MAX_HANDOFF_EVIDENCE_ITEM_CHARS,
        "evidence_json_chars": lease_commands.MAX_HANDOFF_EVIDENCE_JSON_CHARS,
        "question_chars": lease_commands.MAX_HANDOFF_BLOCKING_QUESTION_CHARS,
        "resume_chars": lease_commands.MAX_HANDOFF_RESUME_INSTRUCTIONS_CHARS,
    }


#: Characters per worker key (the heartbeat body's declared maximum) and per
#: notebook line, which no write path bounds.
WORKER_KEY_CHARS = 200
NOTEBOOK_LINE_CHARS = 2_000


def fill_desk_lanes(
    app: FastAPI,
    db_file: Path,
    *,
    admin: TestClient,
    seat: Mapping[str, Any],
    other: Mapping[str, Any],
    project_id: int,
) -> dict[str, Any]:
    """Fill every lane of ``seat``'s desk one row past its maximum, with texts at
    their longest: ready work, review requests, run controls, worker stop
    requests, claim handoffs on held leases, lapsed leases, open questions and
    the work held back behind them, delegations waiting on ``other``, unread
    notifications and the notebook.

    ``admin`` acts as the operator (an admin client). ``seat`` and ``other`` are
    onboarded agents; ``seat`` needs ``docs:write`` for its notebook. The caller
    turns the token rate limit off (``config.TOKEN_RATE_LIMIT_PER_MINUTE = 0``):
    this is several hundred requests. Returns the maxima used and the ids made."""
    from athena import config

    maxima = _desk_maxima()
    extra = 1  # one past the lane: a lane at its maximum is clipped, not short
    seat_id, other_id = seat["user"]["id"], other["user"]["id"]
    as_seat = TestClient(app, headers=bearer(seat))
    as_other = TestClient(app, headers=bearer(other))

    def ok(response, status: int = 200) -> Any:
        assert response.status_code == status, response.text[:500]
        return response.json()

    def title(tag: str) -> str:
        head = f"{tag} "
        return head + "t" * (maxima["title_chars"] - len(head))

    def issue(tag: str, *delegates: int) -> int:
        made = ok(
            admin.post("/issues", json={"title": title(tag), "project_id": project_id}),
            201,
        )["id"]
        for user_id in delegates:
            ok(admin.post(f"/issues/{made}/delegate", json={"user_id": user_id}), 201)
        return made

    def claim(client: TestClient, issue_id: int) -> str:
        tag = client.get(f"/issues/{issue_id}").headers["ETag"]
        held = client.post(
            f"/issues/{issue_id}/claim", json={}, headers={"If-Match": tag}
        )
        return ok(held, 201)["generation"]

    evidence_items = maxima["evidence_json_chars"] // (
        maxima["evidence_item_chars"] + 8
    )

    def yield_for_input(client: TestClient, issue_id: int) -> None:
        body = {
            "generation": claim(client, issue_id),
            "reason": "needs_input",
            "note": "n" * maxima["note_chars"],
            "attempted_work": "a" * maxima["attempted_work_chars"],
            "evidence": ["e" * maxima["evidence_item_chars"]] * evidence_items,
            "blocking_question": "q" * maxima["question_chars"],
            "resume_instructions": "r" * maxima["resume_chars"],
        }
        ok(client.post(f"/issues/{issue_id}/yield", json=body), 201)

    made: dict[str, list[int]] = {}
    made["ready"] = [
        issue(f"ready {n}", seat_id) for n in range(maxima["ready"] + extra)
    ]

    # Review requests: one candidate, by the operator, asked of the seat on
    # one review issue each.
    build = issue("build")
    tag = admin.get(f"/issues/{build}").headers["ETag"]
    held = ok(
        admin.post(f"/issues/{build}/claim", json={}, headers={"If-Match": tag}), 201
    )
    submitted = ok(
        admin.post(
            f"/issues/{build}/complete",
            json={
                "generation": held["generation"],
                "candidate_submission": {
                    "identity": {
                        "kind": "git",
                        "source": "owner/repository",
                        "revision": "a" * 40,
                    },
                    "expected_previous_candidate_id": None,
                },
            },
        )
    )
    candidate = submitted["candidate_submission"]["candidate_id"]
    made["review_requests"] = []
    for n in range(maxima["review_requests"] + extra):
        review = issue(f"review {n}", seat_id)
        ok(
            admin.post(
                f"/issues/{review}/review-requests",
                json={"candidate_id": candidate, "reviewer_id": seat_id},
            )
        )
        made["review_requests"].append(review)

    # Stop requests on the seat's workers: as many as one agent may register.
    for n in range(min(maxima["worker_kill_requests"], config.WORKER_MAX_PER_AGENT)):
        key = f"w{n}-" + "k" * (WORKER_KEY_CHARS - len(f"w{n}-"))
        worker = ok(as_seat.put("/workers/heartbeat", json={"worker_key": key}))
        ok(admin.post(f"/workers/{worker['id']}/kill"))

    # Run controls: one live steer per run, so one run each.
    for n in range(maxima["run_controls"] + extra):
        run_id = f"desk-max-run-{n}"
        ok(as_seat.put("/agent-runs/heartbeat", json={"run_id": run_id}))
        ok(
            admin.post(
                "/run-controls",
                json={
                    "run_id": run_id,
                    "kind": "steer",
                    "payload": "p" * maxima["run_control_payload_chars"],
                },
            ),
            201,
        )

    # Claim handoffs: the other agent yields with every text at its longest,
    # and the seat claims the issue. That fills the held-lease lane too.
    made["handed"] = []
    for n in range(max(maxima["claim_handoffs"], maxima["leases"]) + extra):
        handed = issue(f"handed {n}", other_id, seat_id)
        yield_for_input(as_other, handed)
        claim(as_seat, handed)
        made["handed"].append(handed)

    made["lapsed"] = []
    for n in range(maxima["leases"] + extra):
        lapsed = issue(f"lapsed {n}", seat_id)
        claim(as_seat, lapsed)
        made["lapsed"].append(lapsed)

    # The seat's own open questions, which also hold the work back (not_offered).
    made["asked"] = []
    for n in range(maxima["questions"] + extra):
        asked = issue(f"asked {n}", seat_id)
        yield_for_input(as_seat, asked)
        made["asked"].append(asked)

    # Work the seat delegated that its delegate has not picked up.
    made["waiting"] = []
    for n in range(maxima["waiting_on"] + extra):
        waiting = issue(f"waiting {n}")
        ok(admin.put(f"/issues/{waiting}/assignee", json={"assignee_id": seat_id}))
        ok(as_seat.post(f"/issues/{waiting}/delegate", json={"user_id": other_id}), 201)
        made["waiting"].append(waiting)

    notebook = ok(as_seat.post("/desk/notebook"), 201)["page"]
    lines = ["l" * NOTEBOOK_LINE_CHARS] * (maxima["notebook_lines"] + extra)
    ok(as_seat.patch(f"/pages/{notebook['id']}", json={"body": "\n".join(lines)}))

    conn = db.connect(db_file)
    try:
        for issue_id in made["lapsed"]:
            conn.execute(
                "UPDATE issue_leases SET expires_at = '2000-01-01 00:00:00' "
                "WHERE issue_id = ?",
                (issue_id,),
            )
        for issue_id in made["waiting"]:
            conn.execute(
                "UPDATE issue_contributors SET added_at = '2000-01-01 00:00:00' "
                "WHERE issue_id = ?",
                (issue_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return {"maxima": maxima, "issues": made, "candidate_id": candidate}
