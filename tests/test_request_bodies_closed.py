"""An agent-facing write refuses a field it does not declare (A9).

Pydantic drops an undeclared field by default, so a misspelt or invented field
used to vanish while the write went ahead without it: ``POST /tokens`` naming
another user minted a token for the caller. Every request-body model of a REST
route an MCP write tool calls, and ``POST /tokens``, now refuses unknown fields
(``core/request_models.RequestBody``).

The structural test walks the OpenAPI document: the routes come from the same
tool-to-route map ``scripts/generate_agent_api.py`` derives, and every object
schema reachable from such a body must say ``additionalProperties: false``. A
field typed ``object`` on purpose (the completion report's, which the command
validates) is a property, not a model, so the envelope around it is held while
the field itself stays open.
"""

from __future__ import annotations

from collections.abc import Iterator
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from athena.core import db
from athena.main import create_app
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


def open_body_models(
    spec: dict[str, Any], routes: set[tuple[str, str]]
) -> dict[str, list[str]]:
    """``{model: [routes]}`` for each object model reachable from one of the
    routes' JSON request bodies that does not refuse unknown fields."""
    components = spec.get("components", {}).get("schemas", {})
    found: dict[str, list[str]] = {}
    for path, operations in spec["paths"].items():
        for verb, operation in operations.items():
            if (verb.upper(), _shape(path)) not in routes:
                continue
            body = operation.get("requestBody", {}).get("content", {})
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


def test_every_agent_facing_write_body_refuses_unknown_fields(tmp_path):
    spec = create_app(tmp_path / "openapi.sqlite").openapi()
    routes = mcp_write_routes() | {(verb, _shape(path)) for verb, path in ALSO_CLOSED}
    documented = {
        (verb.upper(), _shape(path))
        for path, operations in spec["paths"].items()
        for verb in operations
    }
    # The map is real: every route it names exists, and it is not empty.
    assert len(routes) > 40
    assert routes <= documented, sorted(routes - documented)
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

    spec = app.openapi()
    routes = {("POST", "/things"), ("POST", "/nested/{}")}
    assert open_body_models(spec, routes) == {
        "Permissive": ["POST /things"],
        "Inner": ["POST /nested/{thing_id}"],
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
