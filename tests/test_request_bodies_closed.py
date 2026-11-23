"""Every agent-facing JSON write refuses a field it does not declare (A9, F5).

Pydantic drops an undeclared field by default, so a misspelt or invented field
used to vanish while the write went ahead without it: ``POST /tokens`` naming
another user minted a token for the caller, and ``POST /projects`` sent with
``"visibility": "private"`` created a public project. Every request-body model
of a REST route an MCP write tool calls, and ``POST /tokens``, was closed first
(A9); F5 closed the rest (``core/request_models.RequestBody``).

The structural test walks the agent OpenAPI document (``/openapi-agent.json``,
built by ``agent_openapi.build_agent_openapi``): every JSON request body in it,
and every object schema reachable from one, must say
``additionalProperties: false``. A new open body anywhere an agent can send one
fails it, not only on routes an MCP tool calls today. The routes the MCP write
tools call (the map ``scripts/generate_agent_api.py`` derives) must all be in
that document, so the walk covers them. A field typed ``object`` on purpose (the
completion report's, which the command validates) is a property, not a model,
so the envelope around it is held while the field itself stays open.
"""

from __future__ import annotations

from collections.abc import Iterator
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from athena.agent_openapi import build_agent_openapi
from athena.core import db
from athena.main import AGENT_TRANSPORT, create_app
from athena.mcp.server import is_read_only

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import generate_agent_api  # noqa: E402

# Routes outside the MCP write map that are agent-facing all the same.
ALSO_CLOSED = [("POST", "/tokens")]
_TEMPLATE = re.compile(r"\{[^}]+\}")


def _shape(path: str) -> str:
    return _TEMPLATE.sub("{}", path.split("?")[0])


def mcp_write_routes() -> set[tuple[str, str]]:
    """Every (verb, path shape) an MCP write tool's client calls perform."""
    calls = generate_agent_api.client_method_calls()
    routes: set[tuple[str, str]] = set()
    for tool, methods in generate_agent_api.tool_client_methods().items():
        if is_read_only(tool):
            continue
        for method in methods:
            for call in calls.get(method, []):
                verb, path = call.split(" ", 1)
                # link_mention picks its base from the source kind at run time.
                for base in ("issues", "pages") if "{base}" in path else (None,):
                    concrete = path.replace("{base}", base) if base else path
                    routes.add((verb, _shape(concrete)))
    return routes


def _refs(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            yield ref.rsplit("/", 1)[-1]
        for value in schema.values():
            yield from _refs(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _refs(value)


def json_body_routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """Every (verb, path shape) in ``spec`` whose request body is JSON."""
    return {
        (verb.upper(), _shape(path))
        for path, operations in spec["paths"].items()
        for verb, operation in operations.items()
        if "application/json" in operation.get("requestBody", {}).get("content", {})
    }


def open_body_models(
    spec: dict[str, Any], routes: set[tuple[str, str]]
) -> dict[str, list[str]]:
    """``{model: [routes]}`` for each object model reachable from one of the
    routes' JSON request bodies that does not refuse unknown fields. A body
    declared inline, not as a component, is reported under ``<inline>``."""
    components = spec.get("components", {}).get("schemas", {})
    found: dict[str, list[str]] = {}
    for path, operations in spec["paths"].items():
        for verb, operation in operations.items():
            if (verb.upper(), _shape(path)) not in routes:
                continue
            body = operation.get("requestBody", {}).get("content", {})
            schema = body.get("application/json", {}).get("schema", {})
            if (
                schema.get("type") == "object"
                and schema.get("additionalProperties") is not False
            ):
                found.setdefault("<inline>", []).append(f"{verb.upper()} {path}")
            pending = list(_refs(body.get("application/json", {})))
            seen: set[str] = set()
            while pending:
                name = pending.pop()
                if name in seen:
                    continue
                seen.add(name)
                model = components[name]
                pending.extend(_refs(model))
                if (
                    model.get("type") == "object"
                    and model.get("additionalProperties") is not False
                ):
                    found.setdefault(name, []).append(f"{verb.upper()} {path}")
    return found


def test_every_json_write_body_in_the_agent_document_refuses_unknown_fields(
    tmp_path,
):
    app = create_app(tmp_path / "openapi.sqlite")
    spec = build_agent_openapi(app, AGENT_TRANSPORT)
    routes = json_body_routes(spec)
    documented = {
        (verb.upper(), _shape(path))
        for path, operations in spec["paths"].items()
        for verb in operations
    }
    # The walk is real: the document has the write bodies an agent sends, and
    # every route an MCP write tool calls (and POST /tokens) is in it.
    assert len(routes) > 60
    named = mcp_write_routes() | {(verb, _shape(path)) for verb, path in ALSO_CLOSED}
    assert len(named) > 40
    assert named <= documented, sorted(named - documented)
    assert open_body_models(spec, routes) == {}


class Inner(BaseModel):
    note: str


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner: Inner


class Permissive(BaseModel):
    name: str


def test_the_walk_fails_on_a_model_that_accepts_unknown_fields():
    """Negative self-test: a new permissive body, or a permissive model nested
    in a closed one, is reported."""
    app = FastAPI()

    @app.post("/things")
    def make(payload: Permissive) -> dict:
        return {}

    @app.post("/nested/{thing_id}")
    def nest(thing_id: int, payload: Closed) -> dict:
        return {}

    @app.post("/loose")
    def loose(payload: dict = Body(...)) -> dict:
        return {}

    spec = app.openapi()
    routes = json_body_routes(spec)
    assert routes == {("POST", "/things"), ("POST", "/nested/{}"), ("POST", "/loose")}
    assert open_body_models(spec, routes) == {
        "Permissive": ["POST /things"],
        "Inner": ["POST /nested/{thing_id}"],
        "<inline>": ["POST /loose"],
    }


def _token_rows(path) -> list[tuple[int, str]]:
    conn = db.connect(path)
    try:
        return [
            (row["user_id"], row["name"])
            for row in conn.execute("SELECT user_id, name FROM api_tokens ORDER BY id")
        ]
    finally:
        conn.close()


def test_a_token_request_naming_another_user_is_refused_and_mints_nothing(tmp_path):
    path = tmp_path / "tokens.sqlite"
    with TestClient(create_app(path)) as client:
        for email, name in (("a@example.com", "Ann"), ("b@example.com", "Bo")):
            created = client.post(
                "/users",
                json={"email": email, "name": name},
                headers={"X-Athena-Actor": "1"},
            )
            assert created.status_code == 201, created.text
        before = _token_rows(path)
        refused = client.post(
            "/tokens",
            json={"name": "for bo", "scopes": ["read"], "user_id": 2},
            headers={"X-Athena-Actor": "1"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "validation_error"
        assert [error["loc"] for error in refused.json()["detail"]] == [
            ["body", "user_id"]
        ]
        assert _token_rows(path) == before
        # The same request without the stray field mints for the caller only.
        minted = client.post(
            "/tokens",
            json={"name": "mine", "scopes": ["read"]},
            headers={"X-Athena-Actor": "1"},
        )
        assert minted.status_code == 201
        assert _token_rows(path) == [*before, (1, "mine")]


def test_a_claim_with_an_unknown_field_is_refused_and_takes_no_lease(tmp_path):
    with TestClient(create_app(tmp_path / "claim.sqlite")) as client:
        client.post(
            "/users",
            json={"email": "a@example.com", "name": "Ann"},
            headers={"X-Athena-Actor": "1"},
        )
        client.headers.update({"X-Athena-Actor": "1"})
        issue = client.post("/issues", json={"title": "t"}).json()
        etag = client.get(f"/issues/{issue['id']}").headers["etag"]
        refused = client.post(
            f"/issues/{issue['id']}/claim",
            json={"lease_secs": 60},
            headers={"If-Match": etag},
        )
        assert refused.status_code == 422, refused.text
        assert client.get(f"/issues/{issue['id']}/lease").json() is None


def test_a_project_or_space_create_with_an_unknown_field_creates_nothing(tmp_path):
    """FLEET-16's shape on the two container creates an agent sends: a field the
    body does not declare is a 422 that names it, and nothing is written. A space
    is not created private by its create body, so ``visibility`` there is such a
    field (``PUT /spaces/{id}/visibility`` sets it)."""
    path = tmp_path / "containers.sqlite"
    with TestClient(create_app(path)) as client:
        client.post(
            "/users",
            json={"email": "a@example.com", "name": "Ann"},
            headers={"X-Athena-Actor": "1"},
        )
        client.headers.update({"X-Athena-Actor": "1"})
        for route, body, stray in (
            ("/projects", {"name": "P", "key": "PRJ", "colour": "red"}, "colour"),
            (
                "/spaces",
                {"name": "S", "key": "SPC", "visibility": "private"},
                "visibility",
            ),
        ):
            refused = client.post(route, json=body)
            assert refused.status_code == 422, (route, refused.text)
            assert refused.json()["code"] == "validation_error"
            assert [error["loc"] for error in refused.json()["detail"]] == [
                ["body", stray]
            ]
        conn = db.connect(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0
        finally:
            conn.close()
