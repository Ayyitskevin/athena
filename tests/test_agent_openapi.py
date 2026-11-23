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
import sys
from typing import Any

from fastapi import FastAPI, Form
from fastapi.openapi.models import OpenAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
import jsonschema
import pytest

from athena import agent_openapi, config, middleware
from athena.agent_openapi import AGENT, BROWSER, DOCUMENT, AGENT_OPENAPI_URL
from athena.core import identity
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
    assert _operation(agent, "POST", "/issues")["security"] == _BEARER
    assert _operation(agent, "GET", "/issues")["security"] == _OPTIONAL
    assert _operation(agent, "POST", "/users")["security"] == _OPTIONAL  # bootstrap
    assert _operation(agent, "GET", "/healthz")["security"] == []
    assert _operation(agent, "POST", "/forge/{source_name}")["security"] == []
    # A dependency that calls current_actor itself, rather than through Depends.
    assert _operation(agent, "GET", "/fleet/active-work")["security"] == _BEARER

    routes = _route_index(app)
    for method, path, operation in _operations(agent):
        assert "security" in operation, (method, path)
        # The scheme replaces the header parameters the identity dependency adds,
        # and the trusted actor header (refused by athena-serve) is not offered.
        assert not _header_names(operation) & {"authorization", "x-athena-actor"}
        calls = agent_openapi._dependency_calls(routes[(method, path)].dependant)
        if identity.current_actor in calls:
            assert operation["security"] == _BEARER, (method, path)


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
            if operation["security"] == _BEARER:
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
