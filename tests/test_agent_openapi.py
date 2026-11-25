"""The agent-facing OpenAPI document (B7): what an agent may call, and what it
must send and may receive, derived from the code that enforces it.

WHY: ``/openapi.json`` listed 393 operations, 173 of them browser pages and form
posts an agent's token can never use, and said nothing about the headers the
middleware enforces (``If-Match`` appeared three times). An agent generating a
client from it would call routes that answer with HTML, retry writes without an
``Idempotency-Key``, and branch on prose. ``/openapi-agent.json`` is the same app
seen through ``athena.agent_openapi``; these tests hold each declaration to the
mechanism it claims to describe, by assertion on known routes and on the rule.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import re
import socket
import sys
import threading
from typing import Any

from fastapi import Depends, FastAPI, Form
from fastapi.openapi.models import OpenAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
import jsonschema
import pytest
import uvicorn

from support import ADMIN, bearer, bootstrap, mint_token

from athena import agent_openapi, config, middleware, ops
from athena.agent_openapi import AGENT, BROWSER, DOCUMENT, AGENT_OPENAPI_URL
from athena.core import identity, tokens
from athena.core.refusal_codes import REFUSAL_CODES
from athena.main import AGENT_TRANSPORT, create_app

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import generate_agent_api  # noqa: E402

_TEMPLATE = re.compile(r"\{[^}]+\}")
_BEARER = [{"bearerAuth": []}]
_OPTIONAL = [{}, {"bearerAuth": []}]
_RUN_REFS = {
    "#/components/parameters/XAthenaRun",
    "#/components/parameters/XAthenaParentRun",
    "#/components/parameters/XAthenaForkFromEvent",
}
_KEY_REF = "#/components/parameters/IdempotencyKey"
_IF_MATCH_REF = "#/components/parameters/IfMatch"


def _required(security: list[dict[str, Any]]) -> bool:
    """A bearer requirement with no anonymous alternative, scoped or not."""
    return bool(security) and all(list(entry) == ["bearerAuth"] for entry in security)


def _shape(path: str) -> str:
    return _TEMPLATE.sub("{}", path.split("?")[0])


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[FastAPI, dict[str, Any], dict[str, Any]]:
    """One app, its whole-app document, and its agent document (no HTTP)."""
    app = create_app(tmp_path_factory.mktemp("agent-openapi") / "athena.db")
    full = copy.deepcopy(app.openapi())
    return app, full, agent_openapi.build_agent_openapi(app, AGENT_TRANSPORT)


def _operations(document: dict[str, Any]):
    for path, item in document["paths"].items():
        for method, operation in item.items():
            yield method.upper(), path, operation


def _operation(document: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    return document["paths"][path][method.lower()]


def _parameter_refs(operation: dict[str, Any]) -> set[str]:
    return {p["$ref"] for p in operation.get("parameters", []) if "$ref" in p}


def _header_names(operation: dict[str, Any]) -> set[str]:
    return {
        p["name"].lower()
        for p in operation.get("parameters", [])
        if p.get("in") == "header"
    }


def _route_index(app: FastAPI) -> dict[tuple[str, str], Any]:
    index = {}
    for route, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT):
        if kind == AGENT:
            for method in route.methods:
                index[(method, route.path_format)] = route
    return index


# --- which operations -----------------------------------------------------------


def test_every_route_is_classified_agent_browser_or_document(built):
    app, _, _ = built
    unclassified = [
        (sorted(getattr(route, "methods", None) or ()), getattr(route, "path", None))
        for route, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT)
        if kind is None
    ]
    assert unclassified == [], (
        "these routes are neither agent-facing nor browser by the rule in "
        f"athena/agent_openapi.py — decide which and say so in code: {unclassified}"
    )
    kinds = [kind for _, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT)]
    assert kinds.count(AGENT) > 150 and kinds.count(BROWSER) > 100
    assert kinds.count(DOCUMENT) == 2


def test_a_route_that_fits_neither_rule_is_unclassified():
    """The rule decides from the code, so a route outside the browser routers
    that renders HTML or takes a URL-encoded form is unclassified (and the test
    above fails) instead of silently joining either document."""
    app = FastAPI()

    @app.get("/page", response_class=HTMLResponse)
    def page() -> str:
        return "<p>hi</p>"

    @app.post("/form")
    def form(name: str = Form(...)) -> dict:
        return {}

    @app.post("/json")
    def json_write(payload: dict) -> dict:
        return {}

    kinds = {
        route.path: kind
        for route, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT)
        if route.path in {"/page", "/form", "/json"}
    }
    assert kinds == {"/page": None, "/form": None, "/json": AGENT}


def test_no_browser_route_is_in_the_document(built):
    app, full, agent = built
    browser = {
        (method, route.path_format)
        for route, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT)
        if kind == BROWSER and getattr(route, "include_in_schema", False)
        for method in route.methods
    }
    assert len(browser) > 100
    listed = {(method, path) for method, path, _ in _operations(agent)}
    assert listed & browser == set()
    # The browser surface is the web routers: none of their paths survive.
    assert not any(path.startswith("/aegis/") for _, path in listed)
    # Nothing in the document answers HTML or takes a form post.
    for method, path, operation in _operations(agent):
        body = operation.get("requestBody", {}).get("content", {})
        assert "application/x-www-form-urlencoded" not in body, (method, path)
        for response in operation["responses"].values():
            assert "text/html" not in response.get("content", {}), (method, path)
    # Every agent route the whole-app document lists is here, and nothing else.
    agent_routes = {
        (method, route.path_format)
        for route, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT)
        if kind == AGENT and route.include_in_schema
        for method in route.methods
    }
    assert listed == agent_routes
    whole = {(method, path) for method, path, _ in _operations(full)}
    assert listed <= whole and len(whole) == len(listed) + len(browser)


def test_every_route_an_mcp_tool_calls_is_in_the_document(built):
    _, _, agent = built
    calls = generate_agent_api.client_method_calls()
    called: set[tuple[str, str]] = set()
    for methods in generate_agent_api.tool_client_methods().values():
        for method in methods:
            for call in calls.get(method, []):
                verb, path = call.split(" ", 1)
                # link_mention picks its base from the source kind at run time.
                for base in ("issues", "pages") if "{base}" in path else (None,):
                    concrete = path.replace("{base}", base) if base else path
                    called.add((verb, _shape(concrete)))
    assert len(called) > 80, "the tool-to-route map is suspiciously small"
    documented = {(method, _shape(path)) for method, path, _ in _operations(agent)}
    assert called - documented == set()


# --- the bearer scheme ----------------------------------------------------------


def test_the_bearer_scheme_is_declared_where_a_credential_is_read(built):
    app, _, agent = built
    assert agent["components"]["securitySchemes"]["bearerAuth"] == {
        **agent["components"]["securitySchemes"]["bearerAuth"],
        "type": "http",
        "scheme": "bearer",
    }
    assert _operation(agent, "GET", "/desk")["security"] == _BEARER
    assert _operation(agent, "POST", "/issues")["security"] == [
        {"bearerAuth": ["issue:write"]}
    ]
    assert _operation(agent, "GET", "/issues")["security"] == _OPTIONAL
    assert _operation(agent, "POST", "/users")["security"] == _OPTIONAL  # bootstrap
    assert _operation(agent, "GET", "/healthz")["security"] == []
    assert _operation(agent, "POST", "/forge/{source_name}")["security"] == []
    # A dependency that calls current_actor itself, rather than through Depends.
    assert _required(_operation(agent, "GET", "/fleet/active-work")["security"])

    routes = _route_index(app)
    for method, path, operation in _operations(agent):
        assert "security" in operation, (method, path)
        # The scheme replaces the header parameters the identity dependency adds,
        # and the trusted actor header (refused by athena-serve) is not offered.
        assert not _header_names(operation) & {"authorization", "x-athena-actor"}
        calls = agent_openapi._dependency_calls(routes[(method, path)].dependant)
        if identity.current_actor in calls:
            assert _required(operation["security"]), (method, path)


def test_declared_security_matches_how_routes_answer_without_a_credential(
    tmp_path, monkeypatch, built
):
    """Behaviour, not code: every parameter-free GET that declares the bearer
    scheme required answers 401 to a caller who presents nothing, and every one
    that declares it optional or absent does not."""
    _, _, agent = built
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "security.db")
    checked = 0
    with TestClient(app) as client:
        for method, path, operation in _operations(agent):
            if method != "GET" or "{" in path:
                continue
            required = any(p.get("required") for p in operation.get("parameters", []))
            if required:
                continue
            status = client.get(path).status_code
            if _required(operation["security"]):
                assert status == 401, (path, status)
            else:
                assert status != 401, (path, status)
            checked += 1
    assert checked > 20


# --- Idempotency-Key ---------------------------------------------------------------


def test_idempotency_key_is_declared_exactly_where_the_middleware_takes_one(built):
    _, _, agent = built
    assert _KEY_REF in _parameter_refs(_operation(agent, "POST", "/issues"))
    assert _KEY_REF in _parameter_refs(_operation(agent, "PATCH", "/pages/{page_id}"))
    # A one-time secret is never replayed, so its create refuses the key.
    assert _KEY_REF not in _parameter_refs(_operation(agent, "POST", "/tokens"))
    # Signed machine callbacks own their retry semantics.
    assert _KEY_REF not in _parameter_refs(
        _operation(agent, "POST", "/forge/{source_name}")
    )
    assert _KEY_REF not in _parameter_refs(_operation(agent, "GET", "/issues"))
    for method, path, operation in _operations(agent):
        declared = _KEY_REF in _parameter_refs(operation)
        assert declared == middleware.idempotency_key_accepted(method, path), (
            method,
            path,
        )
        if declared:
            assert {"400", "409", "429", "503"} <= set(operation["responses"])


def _probe(method: str, path: str, key: bytes) -> tuple[str, Any]:
    """Drive the real IdempotencyMiddleware with one request and no credential:
    ("passed", None) when it hands the request on, else ("refused", code)."""
    reached: list[bool] = []

    async def downstream(scope, receive, send):
        reached.append(True)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    guard = middleware.IdempotencyMiddleware(
        downstream,
        db_path=":memory:",
        wait_seconds=0,
        lease_seconds=1,
        ttl_seconds=1,
        max_response_bytes=1,
    )
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"idempotency-key", key)],
        "state": {},
    }
    asyncio.run(guard(scope, receive, send))
    if reached:
        return "passed", None
    body = json.loads(b"".join(m.get("body", b"") for m in sent[1:]))
    return "refused", body["code"]


def test_the_idempotency_predicate_is_the_middlewares_own_answer(built):
    """For every write operation of the whole app (browser routes included), ask
    the middleware itself. It accepts a key when a valid key without a
    credential passes through to the route's identity gate AND a malformed key
    is refused as malformed (a signed callback passes both through unread)."""
    _, full, _ = built
    probed = 0
    for method, path, _ in _operations(full):
        if method in ("GET", "HEAD", "OPTIONS"):
            continue
        valid = _probe(method, path, b"retry-1")
        malformed = _probe(method, path, b"has space")
        accepted = valid == ("passed", None) and malformed == (
            "refused",
            "idempotency_key_invalid",
        )
        assert accepted == middleware.idempotency_key_accepted(method, path), (
            method,
            path,
            valid,
            malformed,
        )
        probed += 1
    assert probed > 150


# --- If-Match and ETag ------------------------------------------------------------


def test_if_match_is_declared_where_the_route_reads_it(built):
    app, _, agent = built
    for method, path in (
        ("PATCH", "/issues/{issue_id}"),
        ("PATCH", "/pages/{page_id}"),
        ("PUT", "/projects/{project_id}/policy"),
    ):
        operation = _operation(agent, method, path)
        assert _IF_MATCH_REF in _parameter_refs(operation), (method, path)
        assert {"400", "412", "431"} <= set(operation["responses"]), (method, path)
    # The claim declared its own If-Match; it is kept as declared. It is not
    # required of every call: a generation-only renewal omits it (A12).
    claim = _operation(agent, "POST", "/issues/{issue_id}/claim")
    declared = [p for p in claim["parameters"] if p.get("name") == "If-Match"]
    assert len(declared) == 1 and declared[0]["required"] is False
    assert "renewal" in declared[0]["description"]
    assert _IF_MATCH_REF not in _parameter_refs(claim)
    # Ready pickup checks for If-Match only to refuse it: not declared.
    claim_next = _operation(agent, "POST", "/issues/claim-next")
    assert "if-match" not in _header_names(claim_next)
    assert _IF_MATCH_REF not in _parameter_refs(claim_next)

    # Independently of the AST walk: the endpoints whose own source hands the
    # request's If-Match lines to a command are exactly the declared set.
    import inspect

    routes = _route_index(app)
    textual = set()
    for (method, path), route in routes.items():
        source = inspect.getsource(route.endpoint)
        if "if_match_values(request)" in source:
            textual.add((method, path))
    declared_set = {
        (method, path)
        for method, path, operation in _operations(agent)
        if "if-match" in _header_names(operation)
        or _IF_MATCH_REF in _parameter_refs(operation)
    }
    assert len(textual) >= 7
    assert declared_set == textual


def test_etag_is_declared_on_success_where_the_route_sets_it(tmp_path, built):
    _, _, agent = built
    for method, path in (("GET", "/issues/{ref}"), ("GET", "/pages/{page_id}")):
        success = _operation(agent, method, path)["responses"]["200"]
        assert success["headers"]["ETag"] == {"$ref": "#/components/headers/ETag"}
    assert "headers" not in _operation(agent, "GET", "/issues")["responses"]["200"]

    # And the declared header is really sent.
    app = create_app(tmp_path / "etag.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        actor = {"X-Athena-Actor": "1"}
        issue = client.post("/issues", json={"title": "t"}, headers=actor).json()
        read = client.get(f"/issues/{issue['id']}", headers=actor)
        assert read.status_code == 200 and read.headers["ETag"]
        listed = client.get("/issues", headers=actor)
        assert "ETag" not in listed.headers


# --- the run headers ----------------------------------------------------------------


def test_run_headers_are_declared_on_every_write_and_no_read(built):
    _, _, agent = built
    components = agent["components"]["parameters"]
    assert {components[name]["name"] for name in components} >= {
        "X-Athena-Run",
        "X-Athena-Parent-Run",
        "X-Athena-Fork-From-Event",
    }
    # The middleware's own header names, spelled as HTTP clients send them.
    assert {
        middleware.RUN_HEADER,
        middleware.PARENT_RUN_HEADER,
        middleware.FORK_FROM_EVENT_HEADER,
    } == {
        components[ref.rsplit("/", 1)[1]]["name"].lower().encode() for ref in _RUN_REFS
    }
    assert _RUN_REFS <= _parameter_refs(_operation(agent, "POST", "/issues"))
    for method, path, operation in _operations(agent):
        refs = _parameter_refs(operation)
        if method in ("GET", "HEAD", "OPTIONS"):
            assert not refs & _RUN_REFS, (method, path)
        else:
            assert _RUN_REFS <= refs, (method, path)


# --- the refusal body ---------------------------------------------------------------


def _resolve(document: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        parts = node["$ref"].removeprefix("#/").split("/")
        target: Any = document
        for part in parts:
            target = target[part]
        node = target
    return node


def _refusal_body(document: dict[str, Any], response: dict[str, Any]) -> bool:
    schema = _resolve(document, response)["content"]["application/json"]["schema"]
    refusal = {"$ref": "#/components/schemas/Refusal"}
    return schema == refusal or refusal in schema.get("allOf", [])


def test_every_error_response_carries_the_refusal_schema(built):
    _, _, agent = built
    refusal = agent["components"]["schemas"]["Refusal"]
    assert refusal["required"] == ["detail", "code"]
    assert set(refusal["properties"]["code"]["enum"]) == {
        code
        for code, entry in REFUSAL_CODES.items()
        if any(status >= 400 for status in entry.statuses)
    }
    for method, path, operation in _operations(agent):
        responses = operation["responses"]
        assert "default" in responses and "500" in responses, (method, path)
        for status, response in responses.items():
            if status == "default" or status[0] in "45":
                assert _refusal_body(agent, response), (method, path, status)
    # Where the statuses are derivable they are listed.
    issues = set(_operation(agent, "POST", "/issues")["responses"])
    assert {"401", "403", "409", "422", "429", "500", "503", "default"} <= issues
    assert set(_operation(agent, "GET", "/healthz")["responses"]) == {
        "200",
        "500",
        "default",
    }
    assert "HTTPValidationError" not in agent["components"]["schemas"]


def test_real_refusals_validate_against_the_declared_schema(tmp_path, built):
    _, _, agent = built
    schema = {
        **agent["components"]["schemas"]["Refusal"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
    }
    app = create_app(tmp_path / "refusals.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        unauthenticated = client.get("/desk")
        invalid = client.post("/issues", json={}, headers={"X-Athena-Actor": "1"})
        missing = client.get("/issues/999999")
    assert (unauthenticated.status_code, invalid.status_code, missing.status_code) == (
        401,
        422,
        404,
    )
    for response in (unauthenticated, invalid, missing):
        jsonschema.validate(response.json(), schema)


# --- the document is valid OpenAPI 3.1 ----------------------------------------------


def test_the_document_validates_as_openapi_3_1(built):
    _, _, agent = built
    assert agent["openapi"] == "3.1.0"
    # FastAPI's own model of the OpenAPI 3.1 document.
    OpenAPI.model_validate(agent)
    # Every schema is a valid JSON Schema 2020-12 (OpenAPI 3.1's dialect).
    for name, schema in agent["components"]["schemas"].items():
        jsonschema.Draft202012Validator.check_schema(schema)

    # Every reference resolves; operation ids are unique; path parameters are
    # declared.
    def refs(node):
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                yield node["$ref"]
            for value in node.values():
                yield from refs(value)
        elif isinstance(node, list):
            for value in node:
                yield from refs(value)

    for ref in set(refs(agent)):
        _resolve(agent, {"$ref": ref})
    ids = [operation["operationId"] for _, _, operation in _operations(agent)]
    assert len(ids) == len(set(ids))
    for method, path, operation in _operations(agent):
        declared = {
            p["name"] for p in operation.get("parameters", []) if p.get("in") == "path"
        }
        assert set(_TEMPLATE.findall(path)) == {f"{{{name}}}" for name in declared}, (
            method,
            path,
        )


# --- the route ----------------------------------------------------------------------


def _document_routes(app: FastAPI) -> dict[str, Any]:
    return {
        route.path: route
        for route in app.routes
        if getattr(route, "path", None) in {app.openapi_url, AGENT_OPENAPI_URL}
    }


def test_the_document_is_served_beside_openapi_json_under_its_access_rule(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path / "served.db")
    routes = _document_routes(app)
    whole, agent_route = routes[app.openapi_url], routes[AGENT_OPENAPI_URL]
    # The same kind of route, the same methods, no dependency: one access rule.
    assert type(agent_route) is type(whole)
    assert agent_route.methods == whole.methods == {"GET", "HEAD"}
    assert not hasattr(agent_route, "dependant") and not hasattr(whole, "dependant")
    assert agent_route.include_in_schema is False

    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    for anonymous_reads in (True, False):
        monkeypatch.setattr(config, "ANONYMOUS_READS", anonymous_reads)
        with TestClient(app) as client:
            served = client.get(AGENT_OPENAPI_URL)
            assert served.status_code == client.get("/openapi.json").status_code == 200
            assert client.head(AGENT_OPENAPI_URL).status_code == 200
    assert served.json() == agent_openapi.build_agent_openapi(app, AGENT_TRANSPORT)
    assert AGENT_OPENAPI_URL not in app.openapi()["paths"]


def test_openapi_json_is_unchanged_byte_for_byte(tmp_path):
    """The whole-app document is FastAPI's own, untouched by the agent one:
    the bytes it serves equal FastAPI's generator on the same routes, before
    and after the agent document is built and served."""
    app = create_app(tmp_path / "unchanged.db")
    assert type(app).openapi is FastAPI.openapi
    assert "openapi" not in vars(app)
    with TestClient(app) as client:
        before = client.get("/openapi.json").content
        client.get(AGENT_OPENAPI_URL)
        after = client.get("/openapi.json").content
    expected = JSONResponse(
        get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            routes=app.routes,
        )
    ).body
    assert before == after == expected


# --- scopes, 404 and 428 per operation (G5) -----------------------------------------


def _scopes(operation: dict[str, Any]) -> list[str]:
    return sorted(
        {scope for entry in operation["security"] for scope in entry.get(_SCHEME, [])}
    )


_SCHEME = "bearerAuth"


def _probe_url(path: str, operation: dict[str, Any]) -> str:
    """The path with every parameter naming nothing that exists."""
    types = {
        p["name"]: p.get("schema", {}).get("type")
        for p in operation.get("parameters", [])
        if p.get("in") == "path"
    }
    for name in _TEMPLATE.findall(path):
        bare = name[1:-1]
        path = path.replace(
            name, "999999" if types.get(bare) == "integer" else "no-such-thing"
        )
    return path


def test_each_operation_names_the_token_scope_it_needs(built):
    _, _, agent = built
    assert _scopes(_operation(agent, "POST", "/issues")) == ["issue:write"]
    assert _scopes(_operation(agent, "PATCH", "/pages/{page_id}")) == ["docs:write"]
    # Scoped by a check in the endpoint, and by one written out in a dependency.
    assert _scopes(_operation(agent, "POST", "/fleet/seats")) == ["admin"]
    assert _scopes(_operation(agent, "GET", "/fleet/active-work")) == ["admin"]
    # Any one of the write scopes: one requirement each.
    assert _operation(agent, "POST", "/filters")["security"] == [
        {_SCHEME: ["issue:write"]},
        {_SCHEME: ["docs:write"]},
        {_SCHEME: ["admin"]},
    ]
    # A read takes any token; an optional credential stays optional.
    assert _operation(agent, "GET", "/desk")["security"] == _BEARER
    assert _operation(agent, "GET", "/issues")["security"] == _OPTIONAL
    # The 403 of a scoped operation names the refusal and the scope.
    refused = _operation(agent, "POST", "/issues")["responses"]["403"]
    assert "`scope_denied`" in refused["description"]
    assert "`issue:write`" in refused["description"]
    assert _refusal_body(agent, refused)
    unscoped = _operation(agent, "GET", "/desk")["responses"]["403"]
    assert unscoped == {"$ref": "#/components/responses/Refusal403"}


def _scope_checked_in_endpoint(route: Any) -> bool:
    """Whether the route's scope is checked only in its endpoint's body."""
    reader = agent_openapi.CodeReader()
    calls = agent_openapi._dependency_calls(route.dependant)
    if calls & set(agent_openapi._SCOPED_DEPENDENCIES):
        return False
    return not any(
        agent_openapi._unconditional_scopes(call, reader)
        for call in calls
        if agent_openapi.inspect.isfunction(call)
    )


def _after_a_return(actor: dict, early: bool) -> dict:
    if early:
        return actor
    identity.require_admin(actor)
    return actor


def _in_a_branch(actor: dict, other: bool) -> dict:
    if other:
        identity.require_token_scope(actor, tokens.DOCS_WRITE_SCOPE)
    return actor


def _before_anything(actor: dict) -> dict:
    identity.require_token_scope(actor, tokens.DOCS_WRITE_SCOPE)
    return actor


def test_only_a_scope_every_request_meets_is_a_requirement():
    reader = agent_openapi.CodeReader()
    assert agent_openapi._unconditional_scopes(_before_anything, reader) == {
        "docs:write"
    }
    # Checked after a return some requests take, or inside a branch: not every
    # request needs it.
    assert agent_openapi._unconditional_scopes(_after_a_return, reader) == set()
    assert agent_openapi._unconditional_scopes(_in_a_branch, reader) == set()


def test_admin_holds_every_scope_so_a_route_needing_it_needs_only_it():
    app = FastAPI()

    @app.post("/both")
    def both(actor: dict = Depends(identity.issue_write_actor)) -> dict:
        identity.require_admin(actor)
        return {}

    (route,) = [r for r in app.routes if getattr(r, "path", None) == "/both"]
    assert agent_openapi.required_scopes(route) == ("admin",)


def test_declared_scopes_match_how_routes_refuse_a_read_token(tmp_path, built):
    """Behaviour, not code: a token holding only ``read`` is refused with
    ``scope_denied`` by every operation that declares a scope, and by no other,
    but for the two that check a scope only when the caller reads another
    account. A scope checked in the endpoint's body is reached after FastAPI
    validates the body, so an empty body answers 422 there first."""
    app, _, agent = built
    routes = _route_index(app)
    served = create_app(tmp_path / "scopes.db", token_rate_limit_per_minute=100_000)
    wrong, undeclared, checked = [], [], 0
    with TestClient(served) as client:
        bootstrap(client)
        read = bearer(mint_token(client, scopes=["read"]))
        for method, path, operation in _operations(agent):
            if method == "HEAD":
                continue
            body = {} if method in {"POST", "PUT", "PATCH"} else None
            answer = client.request(
                method, _probe_url(path, operation), headers=read, json=body
            )
            code = answer.json().get("code") if answer.status_code >= 400 else None
            if _scopes(operation):
                checked += 1
                in_body = _scope_checked_in_endpoint(routes[(method, path)])
                if code != "scope_denied" and not (
                    in_body and code == "validation_error"
                ):
                    wrong.append((method, path, answer.status_code, code))
            elif code == "scope_denied":
                undeclared.append((method, path))
    assert wrong == []
    assert sorted(undeclared) == [
        ("GET", "/users/{user_id}"),
        ("GET", "/users/{user_id}/budget"),
    ]
    assert checked > 100


def test_a_route_that_answers_404_declares_it(tmp_path, built):
    """Behaviour, not code: every operation that answers 404 for a target that
    does not exist declares 404, and only operations that can refuse so do."""
    _, _, agent = built
    served = create_app(tmp_path / "missing.db", token_rate_limit_per_minute=100_000)
    answered = 0
    with TestClient(served) as client:
        bootstrap(client)
        for method, path, operation in _operations(agent):
            if method == "HEAD" or "{" not in path:
                continue
            body = {} if method in {"POST", "PUT", "PATCH"} else None
            status = client.request(
                method, _probe_url(path, operation), headers=ADMIN, json=body
            ).status_code
            if status == 404:
                answered += 1
                assert "404" in operation["responses"], (method, path)
    assert answered > 90
    assert "404" in _operation(agent, "GET", "/issues/{ref}")["responses"]
    assert "404" in _operation(agent, "PATCH", "/issues/{issue_id}")["responses"]
    # Not every path parameter names a thing that may be missing: an unknown
    # idempotency key reads as unknown, and a signed callback's source refuses
    # its signature.
    assert "404" not in _operation(agent, "GET", "/idempotency/{key}")["responses"]
    assert "404" not in _operation(agent, "GET", "/healthz")["responses"]


def test_a_route_that_can_require_a_precondition_declares_428(tmp_path, built):
    _, _, agent = built
    for method, path in (
        ("PATCH", "/issues/{issue_id}"),
        ("POST", "/issues/{issue_id}/claim"),
        ("PUT", "/projects/{project_id}/policy"),
    ):
        responses = _operation(agent, method, path)["responses"]
        assert "428" in responses, (method, path)
        assert responses["428"] == {"$ref": "#/components/responses/Refusal428"}
    assert "428" not in _operation(agent, "GET", "/issues")["responses"]
    described = agent["components"]["responses"]["Refusal428"]["description"]
    assert "`precondition_required`" in described
    assert "`lease_generation_required`" in described

    # And the route does answer it.
    with TestClient(create_app(tmp_path / "precondition.db")) as client:
        bootstrap(client)
        project = client.post(
            "/projects", json={"name": "Ops", "key": "OPS"}, headers=ADMIN
        ).json()
        policy = client.put(
            f"/projects/{project['id']}/policy",
            json={"block_agent_closes_when_blocked": True},
            headers=ADMIN,
        )
    assert policy.status_code == 428, policy.text
    assert policy.json()["code"] == "precondition_required"


def test_the_facts_index_reads_each_tree_once(built):
    """The build asks four questions of every function it follows; each tree
    is walked for them once."""
    app, _, _ = built
    reader = agent_openapi.CodeReader()
    walked: list[Any] = []
    real = agent_openapi.ast.walk

    def counting(node):
        walked.append(node)
        return real(node)

    routes = [
        route
        for route, kind in agent_openapi.classified_routes(app, AGENT_TRANSPORT)
        if kind == AGENT
    ]
    agent_openapi.ast.walk = counting
    try:
        for route in routes[:40]:
            agent_openapi.reads_if_match(route, reader)
            agent_openapi.sets_etag(route, reader)
            agent_openapi.refuses_missing(route, reader)
            agent_openapi.refuses_precondition(route, reader)
    finally:
        agent_openapi.ast.walk = real
    trees = [node for node in walked if isinstance(node, agent_openapi.ast.Module)]
    assert trees and len(trees) == len({id(tree) for tree in trees})


# --- built once, at start (G5) ------------------------------------------------------


def test_the_document_is_built_once_and_served_from_then_on(tmp_path, monkeypatch):
    builds: list[int] = []
    real = agent_openapi.build_agent_openapi

    def counted(app, transport):
        builds.append(1)
        return real(app, transport)

    monkeypatch.setattr(agent_openapi, "build_agent_openapi", counted)
    app = create_app(tmp_path / "once.db")
    document = app.state.agent_openapi
    assert not document.built
    document.start().join(timeout=60)
    assert document.built and builds == [1]
    with TestClient(app) as client:
        first = client.get(AGENT_OPENAPI_URL)
        second = client.get(AGENT_OPENAPI_URL)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == document.get()
    assert builds == [1]


def test_a_request_during_the_build_waits_for_it(tmp_path, monkeypatch):
    gate = threading.Event()
    real = agent_openapi.build_agent_openapi
    builds: list[int] = []

    def slow(app, transport):
        builds.append(1)
        gate.wait(timeout=30)
        return real(app, transport)

    monkeypatch.setattr(agent_openapi, "build_agent_openapi", slow)
    app = create_app(tmp_path / "wait.db")
    thread = app.state.agent_openapi.start()
    answers: list[int] = []
    with TestClient(app) as client:
        asking = threading.Thread(
            target=lambda: answers.append(client.get(AGENT_OPENAPI_URL).status_code)
        )
        asking.start()
        asking.join(timeout=0.5)
        assert answers == []  # still waiting on the build
        gate.set()
        asking.join(timeout=60)
    thread.join(timeout=60)
    assert answers == [200] and builds == [1]


def test_a_build_that_fails_in_the_thread_is_retried_by_the_first_request(
    tmp_path, monkeypatch, caplog
):
    real = agent_openapi.build_agent_openapi
    calls: list[int] = []

    def flaky(app, transport):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(app, transport)

    monkeypatch.setattr(agent_openapi, "build_agent_openapi", flaky)
    app = create_app(tmp_path / "flaky.db")
    app.state.agent_openapi.start().join(timeout=60)
    assert not app.state.agent_openapi.built
    assert "failed to build" in caplog.text
    with TestClient(app) as client:
        assert client.get(AGENT_OPENAPI_URL).status_code == 200
    assert calls == [1, 1]


def test_athena_serve_starts_the_build_once_its_listener_is_bound(
    tmp_path, monkeypatch
):
    app = create_app(tmp_path / "serve.db", loops_after_bind=True)
    starts: list[bool] = []
    seen: list[bool] = []

    def start():
        starts.append(True)
        return threading.Thread(target=lambda: None)

    monkeypatch.setattr(app.state.agent_openapi, "start", start)

    async def serve_nothing(server) -> None:
        seen.append(bool(starts))

    monkeypatch.setattr(uvicorn.Server, "main_loop", serve_nothing)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    ops._serve_bound(app, host="127.0.0.1", port=port, lifespan="on")
    # Started once, once the listener was bound and before serving.
    assert starts == [True] and seen == [True]


def test_a_server_that_never_binds_never_starts_the_build(tmp_path, monkeypatch):
    app = create_app(tmp_path / "taken.db", loops_after_bind=True)
    starts: list[int] = []
    monkeypatch.setattr(app.state.agent_openapi, "start", lambda: starts.append(1))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(SystemExit):
            ops._serve_bound(
                app, host="127.0.0.1", port=taken.getsockname()[1], lifespan="on"
            )
    assert starts == []
