"""The agent-facing OpenAPI document: the REST surface an agent's token can call.

``/openapi.json`` is FastAPI's whole-app document: every operation, browser
form posts included, and none of the headers Athena's middleware enforces. This
module derives a second document from the same app, served beside it at
``AGENT_OPENAPI_URL`` under the same access rule (public metadata, no
dependency), and leaves ``/openapi.json`` untouched (it works on a deep copy).

**Which operations.** Each route is classified from the code, never by path:

- *browser*: a route whose dependencies include ``_require_browser_session``,
  which ``main.py`` attaches to every router under ``web/`` (HTML pages, form
  posts, the cockpit's own JSON helpers) and to ``/docs`` and ``/redoc``; and
  a mount that serves static files;
- *agent*: any other API route, provided its response class is JSON and its
  request body, if any, is JSON or a multipart file upload;
- *document*: the two OpenAPI routes themselves (not operations).

Anything else — a route outside the browser routers that renders HTML or takes
a URL-encoded form — is *unclassified*, and ``tests/test_agent_openapi.py``
fails on it until someone decides which it is.

**What each operation declares**, each from the code that enforces it:

- ``security``: the bearer scheme is required when the route's dependency tree
  reaches ``identity.current_actor`` (or a dependency or the endpoint calls it
  directly), optional (``[{}, bearer]``) when it reaches only
  ``optional_actor``/``bootstrap_optional_actor``, and absent (``[]``) when it
  reaches neither (public metadata, signed machine callbacks). The
  ``authorization`` and ``X-Athena-Actor`` header parameters those dependencies
  add are removed: the first is the scheme, the second the legacy trusted-header
  mode that ``athena-serve`` refuses.
- ``Idempotency-Key``: on exactly the operations ``middleware
  .idempotency_key_accepted`` accepts (the middleware's own predicates).
- ``If-Match``: where the endpoint, a dependency, or an Athena function they
  call (followed four calls deep) compares a request header name for equality
  with ``"if-match"`` (the REST helpers that hand every If-Match line to the
  command); an ``If-Match`` the route already declares is kept as is.
- ``ETag`` response header on 2xx answers where the same walk finds an
  assignment to ``<response>.headers["ETag"]`` (the success path; refusals that
  carry an ETag build their own header dict and are not declared).
- ``X-Athena-Run``, ``X-Athena-Parent-Run`` and ``X-Athena-Fork-From-Event`` on
  writes (every method but GET/HEAD/OPTIONS): ``RunContextMiddleware`` reads them
  on every request and stamps them on each activity event the request records.
- ``Refusal`` (``detail`` and ``code``, ``code`` an enum of the registered REST
  refusal codes) on 4xx/5xx answers: 500 everywhere (the app-wide handler), 401
  and 403 where a credential is read, 400/409/429/503 where an
  ``Idempotency-Key`` is accepted, 400/412/431 where ``If-Match`` is read, 422
  where FastAPI validates the request. Every other refusal a route can answer is
  not derivable per route and is declared once, as the ``default`` response.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
import copy
from dataclasses import dataclass
import inspect
import textwrap
from typing import Any

from fastapi import FastAPI, routing
from fastapi.datastructures import DefaultPlaceholder
from fastapi.responses import JSONResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from athena.core import identity
from athena.core.refusal_codes import REFUSAL_CODES

AGENT_OPENAPI_URL = "/openapi-agent.json"

AGENT = "agent"
BROWSER = "browser"
DOCUMENT = "document"

BEARER_SCHEME = "bearerAuth"
REFUSAL_SCHEMA = "Refusal"
_REFUSAL_REF = {"$ref": f"#/components/schemas/{REFUSAL_SCHEMA}"}
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_API_BODY_MEDIA = frozenset({"application/json", "multipart/form-data"})
# Identity dependencies add these header parameters; the agent document states
# the bearer scheme instead, and the trusted actor header is refused by
# athena-serve (it only CLAIMS an id).
_IDENTITY_HEADERS = frozenset({"authorization", identity.ACTOR_HEADER.lower()})
_CALL_DEPTH = 4

# The headers, as reusable components. Names are spelled as HTTP clients send them.
_IDEMPOTENCY_KEY = "IdempotencyKey"
_IF_MATCH = "IfMatch"
# What each run header means, by the name RunContextMiddleware reads it under. A
# header the middleware reads and this table does not describe fails the build
# of the document (KeyError), rather than going undeclared.
_RUN_HEADER_MEANINGS = {
    b"x-athena-run": (
        "The run this write belongs to. Every activity event the request records "
        "is stamped with it; a run id in a server-reserved namespace "
        "(`automation:`, `icarus:`) is dropped. A run that belongs to another "
        "actor is refused with `run_binding_conflict`."
    ),
    b"x-athena-parent-run": (
        "The run that spawned this one (lineage), stamped beside `X-Athena-Run`."
    ),
    b"x-athena-fork-from-event": (
        "The activity event id after which this run forked from its parent."
    ),
}


@dataclass(frozen=True, slots=True)
class Transport:
    """The middleware's own facts the document declares.

    The composition root (``athena.main.AGENT_TRANSPORT``) builds this from
    ``athena.middleware`` and hands it in, so this module never imports the
    middleware: a middleware change must keep selecting the full test suite
    (``scripts/select_tests.py``), which it would not if a mapped module
    imported it."""

    browser_session: Callable[..., Any]
    idempotency_key_accepted: Callable[[str, str], bool]
    max_idempotency_key_bytes: int
    run_headers: tuple[bytes, ...]


def _spelled(header: bytes) -> str:
    """``b"x-athena-run"`` as clients spell it: ``X-Athena-Run``."""
    return "-".join(part.capitalize() for part in header.decode("ascii").split("-"))


def _run_parameters(transport: Transport) -> dict[str, tuple[str, str]]:
    """Component name -> (header name, description), one per run header."""
    return {
        _spelled(header).replace("-", ""): (
            _spelled(header),
            _RUN_HEADER_MEANINGS[header],
        )
        for header in transport.run_headers
    }


# --- classification -----------------------------------------------------------


def _dependency_calls(dependant: Any) -> set[Callable[..., Any]]:
    calls: set[Callable[..., Any]] = set()
    for dependency in getattr(dependant, "dependencies", []):
        call = getattr(dependency, "call", None)
        if call is not None:
            calls.add(call)
        calls |= _dependency_calls(dependency)
    return calls


def _response_class(route: Any) -> type:
    response_class = route.response_class
    if isinstance(response_class, DefaultPlaceholder):
        return response_class.value
    return response_class


def _body_media(route: Any) -> str | None:
    body = route.body_field
    if body is None:
        return None
    return getattr(body.field_info, "media_type", None)


def classify(
    route: Any, *, openapi_url: str | None, transport: Transport
) -> str | None:
    """``agent``, ``browser``, ``document``, or None when the code does not say."""
    original = getattr(route, "original_route", route)
    if not isinstance(original, routing.APIRoute):
        if isinstance(original, Mount) and isinstance(original.app, StaticFiles):
            return BROWSER
        if getattr(route, "path", None) in {openapi_url, AGENT_OPENAPI_URL}:
            return DOCUMENT
        return None
    if transport.browser_session in _dependency_calls(route.dependant):
        return BROWSER
    if not issubclass(_response_class(route), JSONResponse):
        return None
    media = _body_media(route)
    if media is not None and media not in _API_BODY_MEDIA:
        return None
    return AGENT


def route_contexts(app: FastAPI) -> Iterator[Any]:
    """Every route of the app, included routers flattened with their
    router-level dependencies (FastAPI's own iteration for OpenAPI)."""
    yield from routing.iter_route_contexts(app.routes)


# --- reading the code ---------------------------------------------------------


def _resolve(node: ast.expr, namespace: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = namespace.get(node.value.id)
        return getattr(owner, node.attr, None) if owner is not None else None
    return None


def _athena_function(value: Any) -> bool:
    return inspect.isfunction(value) and value.__module__.startswith("athena.")


def _compares_if_match(node: ast.AST) -> bool:
    """A header name compared for equality with ``if-match``: how the REST
    helpers collect every If-Match field line for the command to evaluate. A
    membership test (``"if-match" in request.headers``) is a route checking for
    the header in order to refuse it, as ready pickup does, and is not a read."""
    return (
        isinstance(node, ast.Compare)
        and all(isinstance(op, ast.Eq) for op in node.ops)
        and any(
            isinstance(side, ast.Constant) and side.value in (b"if-match", "if-match")
            for side in (node.left, *node.comparators)
        )
    )


def _assigns_success_etag(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    return any(
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == "ETag"
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "headers"
        and isinstance(target.value.value, ast.Name)
        for target in node.targets
    )


def _calls_named(node: ast.AST, names: frozenset[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in names


_REQUIRED_IDENTITY = frozenset({"current_actor"})
_OPTIONAL_IDENTITY = frozenset({"optional_actor", "bootstrap_optional_actor"})


class CodeReader:
    """Reads route code by its syntax tree. One instance per document build, so
    its caches never outlive the app whose functions they hold."""

    def __init__(self) -> None:
        self._trees: dict[Callable[..., Any], ast.AST | None] = {}
        self._callees: dict[Callable[..., Any], list[Callable[..., Any]]] = {}
        self._found: dict[tuple[Callable[..., Any], str], bool] = {}
        self._holds: dict[tuple[Callable[..., Any], Callable[..., Any]], bool] = {}

    def tree(self, function: Callable[..., Any]) -> ast.AST | None:
        if function not in self._trees:
            try:
                source = textwrap.dedent(inspect.getsource(function))
                self._trees[function] = ast.parse(source)
            except (OSError, TypeError, SyntaxError):
                self._trees[function] = None
        return self._trees[function]

    def callees(self, function: Callable[..., Any]) -> list[Callable[..., Any]]:
        """The Athena functions ``function`` calls by a name it can resolve."""
        if function not in self._callees:
            tree = self.tree(function)
            namespace = dict(getattr(function, "__globals__", {}))
            try:
                namespace.update(inspect.getclosurevars(function).nonlocals)
            except (TypeError, ValueError):
                pass
            found: list[Callable[..., Any]] = []
            for node in ast.walk(tree) if tree is not None else ():
                if isinstance(node, ast.Call):
                    callee = _resolve(node.func, namespace)
                    if _athena_function(callee) and callee not in found:
                        found.append(callee)
            self._callees[function] = found
        return self._callees[function]

    def holds(
        self, function: Callable[..., Any], predicate: Callable[[ast.AST], bool]
    ) -> bool:
        """Whether ``function``'s own body holds a node ``predicate`` accepts."""
        key = (function, predicate)
        if key not in self._holds:
            tree = self.tree(function)
            self._holds[key] = tree is not None and any(
                predicate(node) for node in ast.walk(tree)
            )
        return self._holds[key]

    def reaches(
        self, function: Callable[..., Any], predicate: Callable[[ast.AST], bool]
    ) -> bool:
        """Whether ``function``'s body, or an Athena function it calls by name
        (followed ``_CALL_DEPTH`` calls deep), holds a node ``predicate`` accepts."""
        key = (function, predicate.__name__)
        if key not in self._found:
            frontier = [function]
            seen: set[Callable[..., Any]] = set()
            result = False
            for _ in range(_CALL_DEPTH + 1):
                following: list[Callable[..., Any]] = []
                for current in frontier:
                    if current in seen:
                        continue
                    seen.add(current)
                    if self.holds(current, predicate):
                        result = True
                        break
                    following += self.callees(current)
                if result:
                    break
                frontier = following
            self._found[key] = result
        return self._found[key]


def _code_roots(route: Any) -> list[Callable[..., Any]]:
    """The endpoint and every dependency callable of a route."""
    roots = [route.endpoint]
    roots += [call for call in _dependency_calls(route.dependant) if callable(call)]
    return [root for root in roots if inspect.isfunction(root)]


def authentication(route: Any, reader: CodeReader | None = None) -> str | None:
    """``required``, ``optional`` or None, from the identity dependencies: the
    dependency tree, or a dependency or endpoint that calls one directly."""
    reader = reader or CodeReader()
    calls = _dependency_calls(route.dependant)
    own = [
        root
        for root in _code_roots(route)
        if getattr(root, "__module__", "") != identity.__name__
    ]

    def called(names: frozenset[str]) -> bool:
        return any(
            reader.holds(root, lambda node: _calls_named(node, names)) for root in own
        )

    if identity.current_actor in calls or called(_REQUIRED_IDENTITY):
        return "required"
    optional = {identity.optional_actor, identity.bootstrap_optional_actor}
    if calls & optional or called(_OPTIONAL_IDENTITY):
        return "optional"
    return None


def reads_if_match(route: Any, reader: CodeReader | None = None) -> bool:
    reader = reader or CodeReader()
    return any(reader.reaches(root, _compares_if_match) for root in _code_roots(route))


def sets_etag(route: Any, reader: CodeReader | None = None) -> bool:
    reader = reader or CodeReader()
    return any(
        reader.reaches(root, _assigns_success_etag) for root in _code_roots(route)
    )


# --- the document -------------------------------------------------------------


def _codes_for(status: int) -> list[str]:
    return [code for code, entry in REFUSAL_CODES.items() if status in entry.statuses]


def _refusal_schema() -> dict[str, Any]:
    codes = [
        code
        for code, entry in REFUSAL_CODES.items()
        if any(status >= 400 for status in entry.statuses)
    ]
    return {
        "title": REFUSAL_SCHEMA,
        "type": "object",
        "description": (
            "Every 4xx and 5xx JSON body: an unchanged `detail` and a registered "
            "`code` (core/refusal_codes.py). Branch on `code`, never on `detail`. "
            "Some refusals add structured fields beside the two."
        ),
        "required": ["detail", "code"],
        "properties": {
            "detail": {
                "description": (
                    "Human-readable; text for most refusals, a list of problems "
                    "for `validation_error`."
                )
            },
            "code": {"type": "string", "enum": codes},
        },
        "additionalProperties": True,
    }


# The refusal answers an operation can declare by status, shared as components.
# Which of them an operation lists is derived from its code (see _agent_operation).
_REFUSALS: dict[int | str, str] = {
    400: "The request or one of its headers is malformed.",
    401: "The credential is missing, unknown or revoked.",
    403: "The credential may not do this, or its account is paused.",
    409: "The target's state, or this Idempotency-Key, conflicts with the request.",
    412: "If-Match no longer names the current ETag; read again and merge.",
    422: "The request failed validation; `detail` lists the problems.",
    429: "A rate limit is exhausted; wait `Retry-After` seconds.",
    431: "The If-Match header is too large.",
    500: (
        "The server failed while handling the request (the app-wide handler); "
        "whether a write applied is unknown."
    ),
    503: "A dependency (the idempotency store) cannot answer now; retry later.",
    "default": (
        "Any other refusal this route can answer. Which statuses those are is not "
        "derivable per route; the body is always a Refusal."
    ),
}


def _response_name(status: int | str) -> str:
    return f"Refusal{str(status).capitalize()}"


def _response_ref(status: int | str) -> dict[str, str]:
    return {"$ref": f"#/components/responses/{_response_name(status)}"}


def _refusal_responses() -> dict[str, Any]:
    responses: dict[str, Any] = {}
    for status, why in _REFUSALS.items():
        description = why
        if isinstance(status, int):
            codes = _codes_for(status)
            if codes:
                description += " Codes registered for this status: " + ", ".join(
                    f"`{code}`" for code in codes
                )
                description += "."
        responses[_response_name(status)] = {
            "description": description,
            "content": {"application/json": {"schema": dict(_REFUSAL_REF)}},
        }
    return responses


def _with_refusal(response: dict[str, Any]) -> dict[str, Any]:
    """A route-declared error response, its body held to the Refusal shape."""
    response = dict(response)
    content = dict(response.get("content", {}))
    schema = content.get("application/json", {}).get("schema")
    content["application/json"] = {
        "schema": dict(_REFUSAL_REF)
        if schema is None
        else {"allOf": [dict(_REFUSAL_REF), schema]}
    }
    response["content"] = content
    return response


def _components(
    transport: Transport,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(parameters, headers, security schemes) the operations refer to."""
    key_limit = transport.max_idempotency_key_bytes
    parameters: dict[str, Any] = {
        _IDEMPOTENCY_KEY: {
            "name": "Idempotency-Key",
            "in": "header",
            "required": False,
            "description": (
                "A caller-chosen key that makes a retry of this write replay the "
                "first outcome instead of repeating it. Create it before the first "
                "attempt and reuse it only for an exact replay. Never put a "
                "credential or user content in it."
            ),
            "schema": {
                "type": "string",
                "minLength": 1,
                "maxLength": key_limit,
                "pattern": "^[\\x21-\\x7e]+$",
            },
        },
        _IF_MATCH: {
            "name": "If-Match",
            "in": "header",
            "required": False,
            "description": (
                "The target's current strong ETag (a read returns it as the "
                "`ETag` header and `_etag`). A stale value is refused with 412 "
                "`precondition_failed`; read again and merge."
            ),
            "schema": {"type": "string"},
        },
    }
    for component, (header, description) in _run_parameters(transport).items():
        parameters[component] = {
            "name": header,
            "in": "header",
            "required": False,
            "description": description,
            "schema": {"type": "string"},
        }
    headers = {
        "ETag": {
            "description": (
                "The strong entity tag of the representation returned; send it as "
                "`If-Match` on the next write."
            ),
            "schema": {"type": "string"},
        }
    }
    schemes = {
        BEARER_SCHEME: {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "An Athena API token (`ath_...`), sent as `Authorization: Bearer`. "
                "Its scopes bound what it may do; a paused or removed account is "
                "refused with 403 `account_paused`."
            ),
        }
    }
    return parameters, headers, schemes


def _parameter_ref(component: str) -> dict[str, str]:
    return {"$ref": f"#/components/parameters/{component}"}


def _declares(operation: dict[str, Any], name: str) -> bool:
    return any(
        parameter.get("in") == "header" and parameter.get("name", "").lower() == name
        for parameter in operation.get("parameters", [])
        if isinstance(parameter, dict)
    )


def _agent_operation(
    route: Any,
    operation: dict[str, Any],
    method: str,
    reader: CodeReader,
    transport: Transport,
) -> dict[str, Any]:
    operation = copy.deepcopy(operation)
    parameters = [
        parameter
        for parameter in operation.get("parameters", [])
        if not (
            parameter.get("in") == "header"
            and parameter.get("name", "").lower() in _IDENTITY_HEADERS
        )
    ]
    statuses: set[int] = {500}

    auth = authentication(route, reader)
    if auth == "required":
        operation["security"] = [{BEARER_SCHEME: []}]
    elif auth == "optional":
        operation["security"] = [{}, {BEARER_SCHEME: []}]
    else:
        operation["security"] = []
    if auth is not None:
        statuses |= {401, 403}

    if transport.idempotency_key_accepted(method, route.path_format):
        parameters.append(_parameter_ref(_IDEMPOTENCY_KEY))
        statuses |= {400, 409, 429, 503}

    if reads_if_match(route, reader):
        if not _declares(operation, "if-match"):
            parameters.append(_parameter_ref(_IF_MATCH))
        statuses |= {400, 412, 431}

    if method not in _SAFE_METHODS:
        for component in _run_parameters(transport):
            parameters.append(_parameter_ref(component))

    if parameters:
        operation["parameters"] = parameters
    else:
        operation.pop("parameters", None)

    responses = operation.setdefault("responses", {})
    if sets_etag(route, reader):
        for status, response in responses.items():
            if str(status).startswith("2"):
                response.setdefault("headers", {})["ETag"] = {
                    "$ref": "#/components/headers/ETag"
                }
    # FastAPI declares 422 wherever it validates the request; the body the
    # handler really sends is a Refusal, not FastAPI's HTTPValidationError.
    if "422" in responses:
        statuses.add(422)
        del responses["422"]
    for status in list(responses):
        if status[:1] in ("4", "5"):
            responses[status] = _with_refusal(responses[status])
    for status in sorted(statuses):
        responses.setdefault(str(status), _response_ref(status))
    responses["default"] = _response_ref("default")
    return operation


def _referenced_schemas(document: Any, schemas: dict[str, Any]) -> set[str]:
    """Names of every component schema reachable from ``document``."""
    prefix = "#/components/schemas/"
    found: set[str] = set()
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(prefix):
                name = ref[len(prefix) :]
                if name not in found and name in schemas:
                    found.add(name)
                    pending.append(schemas[name])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return found


def classified_routes(
    app: FastAPI, transport: Transport
) -> list[tuple[Any, str | None]]:
    """Every route of the app with its classification."""
    return [
        (route, classify(route, openapi_url=app.openapi_url, transport=transport))
        for route in route_contexts(app)
    ]


def build_agent_openapi(app: FastAPI, transport: Transport) -> dict[str, Any]:
    """The agent-facing OpenAPI document for ``app`` (see the module docstring)."""
    full = copy.deepcopy(app.openapi())
    reader = CodeReader()
    paths: dict[str, dict[str, Any]] = {}
    for route, kind in classified_routes(app, transport):
        if kind != AGENT or not route.include_in_schema:
            continue
        item = full.get("paths", {}).get(route.path_format, {})
        for method in sorted(route.methods or ()):
            operation = item.get(method.lower())
            if operation is None:
                continue
            paths.setdefault(route.path_format, {})[method.lower()] = _agent_operation(
                route, operation, method, reader, transport
            )

    parameters, headers, schemes = _components(transport)
    refusal_responses = _refusal_responses()
    schemas = dict(full.get("components", {}).get("schemas", {}))
    schemas[REFUSAL_SCHEMA] = _refusal_schema()
    ordered_paths = {
        path: {method: item[method] for method in _METHODS if method in item}
        for path, item in paths.items()
    }
    kept = _referenced_schemas(ordered_paths, schemas)
    info = dict(full.get("info", {}))
    info["title"] = f"{info.get('title', 'Athena')} agent API"
    info["description"] = (
        "The REST operations an agent's bearer token can call: browser pages and "
        "form posts are left out. Derived from the app at request time; "
        "`/openapi.json` is the whole-app document. Each operation declares the "
        "bearer scheme where it reads a credential, `Idempotency-Key` where the "
        "middleware accepts one, `If-Match` and `ETag` where the route reads or "
        "sets them, the run headers on writes, and the Refusal body on its 4xx "
        "and 5xx answers."
    )
    return {
        "openapi": full.get("openapi", "3.1.0"),
        "info": info,
        "paths": ordered_paths,
        "components": {
            "schemas": {name: schemas[name] for name in sorted(kept)},
            "responses": refusal_responses,
            "parameters": parameters,
            "headers": headers,
            "securitySchemes": schemes,
        },
    }
