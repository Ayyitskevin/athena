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
- The token **scope** the operation needs, as the bearer requirement's role
  list (``[{bearer: ["issue:write"]}]``; ``admin`` satisfies any): from the
  scoped identity dependencies (``issue_write_actor``, ``docs_write_actor``,
  ``admin_actor``, ``token_management_actor``; ``personal_write_actor`` is any
  of the three write scopes, one requirement each), and from a
  ``require_admin``, a ``require_token_scope(..., tokens.<SCOPE>)``, or an
  ``if not token_has_scope(..., tokens.<SCOPE>): raise`` that is a top-level
  statement of the endpoint or a dependency, before any ``return``.
  A scope checked inside a branch is not a requirement of the whole route; the
  operation's 403 says a scope may be refused there.
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
  and 403 where a credential is read (the 403 names ``scope_denied`` and the
  scope where one is required), 400/409/429/503 where an ``Idempotency-Key`` is
  accepted, 400/412/431 where ``If-Match`` is read, 422 where FastAPI validates
  the request, 404 where the route's code (followed as for ``If-Match``) refuses
  with ``not_found`` or status 404, and 428 where it refuses with
  ``precondition_required`` or ``lease_generation_required``. The walk reads
  what the code can raise, not what reaches the wire, so it may declare more:
  ``POST /issues/bulk`` reports each item's refusal inside its 200, and still
  declares the 404 and 428 its items' command can raise. Every other refusal a
  route can answer is declared once, as the ``default`` response.

**When it is built.** Once per app (``AgentDocument``): ``athena-serve`` starts
the build in a thread as soon as its listener is bound, and a request that
arrives first waits for it; any other server builds it on the first request.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
import copy
from dataclasses import dataclass
import inspect
import logging
import textwrap
import threading
from typing import Any

from fastapi import FastAPI, routing
from fastapi.datastructures import DefaultPlaceholder
from fastapi.responses import JSONResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from athena.core import identity, tokens
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


_MISSING_CODES = frozenset({"not_found"})
_PRECONDITION_CODES = frozenset({"precondition_required", "lease_generation_required"})


def _first_text(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        return value if isinstance(value, str) else None
    return None


def _refusal_names(node: ast.AST, codes: frozenset[str], status: int) -> bool:
    """A call that refuses with one of ``codes`` (a command error's kind as its
    first argument, or ``code=``) or with ``status`` (``status_code=``, or a
    leading positional status as ``Refusal`` and ``HTTPException`` take it)."""
    if not isinstance(node, ast.Call):
        return False
    if _first_text(node) in codes:
        return True
    if (
        node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == status
    ):
        return True
    for keyword in node.keywords:
        value = keyword.value
        if not isinstance(value, ast.Constant):
            continue
        if keyword.arg == "code" and value.value in codes:
            return True
        if keyword.arg == "status_code" and value.value == status:
            return True
    return False


def _refuses_missing(node: ast.AST) -> bool:
    """A refusal that says the target does not exist: 404 ``not_found``."""
    return _refusal_names(node, _MISSING_CODES, 404)


def _refuses_precondition(node: ast.AST) -> bool:
    """A refusal that says a precondition is missing: 428."""
    return _refusal_names(node, _PRECONDITION_CODES, 428)


# The facts the document reads from a function's own body, gathered in one walk
# of its tree (``CodeReader.facts``).
_FACTS = (
    _compares_if_match,
    _assigns_success_etag,
    _refuses_missing,
    _refuses_precondition,
)

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
        self._facts: dict[Callable[..., Any], frozenset[Callable[..., Any]]] = {}
        self._calls: dict[Callable[..., Any], list[ast.Call]] = {}

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
            namespace = dict(getattr(function, "__globals__", {}))
            try:
                namespace.update(inspect.getclosurevars(function).nonlocals)
            except (TypeError, ValueError):
                pass
            found: list[Callable[..., Any]] = []
            for call in self._read(function)[1]:
                callee = _resolve(call.func, namespace)
                if _athena_function(callee) and callee not in found:
                    found.append(callee)
            self._callees[function] = found
        return self._callees[function]

    def facts(self, function: Callable[..., Any]) -> frozenset[Callable[..., Any]]:
        """Which of ``_FACTS`` ``function``'s own body holds."""
        return self._read(function)[0]

    def _read(
        self, function: Callable[..., Any]
    ) -> tuple[frozenset[Callable[..., Any]], list[ast.Call]]:
        """One walk of ``function``'s tree: the facts it holds and the calls it
        makes. Every fact is asked of every function the document follows, so
        each tree is walked once, not once per fact and once more for calls."""
        if function not in self._facts:
            tree = self.tree(function)
            found: set[Callable[..., Any]] = set()
            calls: list[ast.Call] = []
            for node in ast.walk(tree) if tree is not None else ():
                if isinstance(node, ast.Call):
                    calls.append(node)
                for predicate in _FACTS:
                    if predicate not in found and predicate(node):
                        found.add(predicate)
            self._facts[function] = frozenset(found)
            self._calls[function] = calls
        return self._facts[function], self._calls[function]

    def holds(
        self, function: Callable[..., Any], predicate: Callable[[ast.AST], bool]
    ) -> bool:
        """Whether ``function``'s own body holds a node ``predicate`` accepts."""
        if predicate in _FACTS:
            return predicate in self.facts(function)
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


def refuses_missing(route: Any, reader: CodeReader | None = None) -> bool:
    """Whether the route's code can refuse with 404 ``not_found``."""
    reader = reader or CodeReader()
    return any(reader.reaches(root, _refuses_missing) for root in _code_roots(route))


def refuses_precondition(route: Any, reader: CodeReader | None = None) -> bool:
    """Whether the route's code can refuse with 428 (a missing ``If-Match`` or
    lease generation)."""
    reader = reader or CodeReader()
    return any(
        reader.reaches(root, _refuses_precondition) for root in _code_roots(route)
    )


ANY_WRITE_SCOPE = (
    tokens.ISSUE_WRITE_SCOPE,
    tokens.DOCS_WRITE_SCOPE,
    tokens.ADMIN_SCOPE,
)
# The scoped identity dependencies, and the scope each requires. A tuple of
# more than one is any one of them.
_SCOPED_DEPENDENCIES: dict[Callable[..., Any], tuple[str, ...]] = {
    identity.issue_write_actor: (tokens.ISSUE_WRITE_SCOPE,),
    identity.docs_write_actor: (tokens.DOCS_WRITE_SCOPE,),
    identity.admin_actor: (tokens.ADMIN_SCOPE,),
    identity.token_management_actor: (tokens.ADMIN_SCOPE,),
    identity.personal_write_actor: ANY_WRITE_SCOPE,
}
_SCOPE_CONSTANTS = {
    name: value
    for name, value in vars(tokens).items()
    if name.endswith("_SCOPE") and value in tokens.SCOPES
}


def _call_of(statement: ast.stmt) -> ast.Call | None:
    """The call a statement makes as a whole: ``f(...)`` or ``x = f(...)``."""
    value = getattr(statement, "value", None)
    if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)) and isinstance(
        value, ast.Call
    ):
        return value
    return None


def _named_scope(node: ast.expr) -> str | None:
    """``tokens.<NAME>_SCOPE`` as the scope it names."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "tokens":
            return _SCOPE_CONSTANTS.get(node.attr)
    return None


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _checked_scope(call: ast.Call) -> str | None:
    """The scope a ``require_admin(...)`` or ``require_token_scope(..., tokens.X)``
    call requires, or None for any other call."""
    name = _called_name(call)
    if name == "require_admin":
        return tokens.ADMIN_SCOPE
    if name == "require_token_scope" and len(call.args) == 2:
        return _named_scope(call.args[1])
    return None


def _refused_scope(statement: ast.stmt) -> str | None:
    """The scope of ``if not token_has_scope(..., tokens.X): raise ...``, a
    check written out where ``require_token_scope`` would do."""
    if not isinstance(statement, ast.If) or statement.orelse:
        return None
    test = statement.test
    if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
        return None
    call = test.operand
    if not isinstance(call, ast.Call) or _called_name(call) != "token_has_scope":
        return None
    if len(call.args) != 2 or not all(
        isinstance(inner, ast.Raise) for inner in statement.body
    ):
        return None
    return _named_scope(call.args[1])


def _unconditional_scopes(function: Callable[..., Any], reader: CodeReader) -> set[str]:
    """The scopes ``function`` checks in top-level statements of its body that
    run before any ``return``: every request it handles past them has the scope."""
    tree = reader.tree(function)
    if tree is None or not tree.body:  # type: ignore[attr-defined]
        return set()
    definition = tree.body[0]  # type: ignore[attr-defined]
    if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    found: set[str] = set()
    for statement in definition.body:
        if any(isinstance(node, ast.Return) for node in ast.walk(statement)):
            break
        call = _call_of(statement)
        scope = _checked_scope(call) if call is not None else _refused_scope(statement)
        if scope is not None:
            found.add(scope)
    return found


def required_scopes(
    route: Any, reader: CodeReader | None = None
) -> tuple[str, ...] | None:
    """The token scope the route needs: one scope, several of which any one
    suffices (``personal_write_actor``), or None when any token will do.
    ``admin`` satisfies every scope, so a route that needs it needs only it."""
    reader = reader or CodeReader()
    required: set[str] = set()
    alternatives: tuple[str, ...] | None = None
    for call in _dependency_calls(route.dependant):
        scopes = _SCOPED_DEPENDENCIES.get(call)
        if scopes is None:
            continue
        if len(scopes) == 1:
            required |= set(scopes)
        else:
            alternatives = scopes
    for root in _code_roots(route):
        if getattr(root, "__module__", "") == identity.__name__:
            continue
        required |= _unconditional_scopes(root, reader)
    if tokens.ADMIN_SCOPE in required:
        return (tokens.ADMIN_SCOPE,)
    if len(required) > 1:
        # Two module scopes at once; only admin's token holds both by one scope.
        return tuple(sorted(required))
    if required:
        return tuple(required)
    return alternatives


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
    404: (
        "The target does not exist, or the caller cannot see it: a hidden target "
        "is answered exactly as a missing one."
    ),
    409: "The target's state, or this Idempotency-Key, conflicts with the request.",
    412: "If-Match no longer names the current ETag; read again and merge.",
    422: "The request failed validation; `detail` lists the problems.",
    428: (
        "A precondition this write needs is missing: `If-Match` with the "
        "target's current ETag (`precondition_required`), or the lease "
        "generation the caller holds (`lease_generation_required`)."
    ),
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
    scopes = required_scopes(route, reader) if auth is not None else None
    if scopes is None:
        bearer: list[dict[str, list[str]]] = [{BEARER_SCHEME: []}]
    elif len(scopes) > 1 and scopes == ANY_WRITE_SCOPE:
        bearer = [{BEARER_SCHEME: [scope]} for scope in scopes]
    else:
        bearer = [{BEARER_SCHEME: list(scopes)}]
    if auth == "required":
        operation["security"] = bearer
    elif auth == "optional":
        operation["security"] = [{}, *bearer]
    else:
        operation["security"] = []
    if auth is not None:
        statuses |= {401, 403}

    if refuses_missing(route, reader):
        statuses.add(404)
    if refuses_precondition(route, reader):
        statuses.add(428)

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
    if scopes is not None and responses.get("403") == _response_ref(403):
        responses["403"] = _scope_refusal(scopes)
    responses["default"] = _response_ref("default")
    return operation


def _scope_refusal(scopes: tuple[str, ...]) -> dict[str, Any]:
    """This operation's 403: the shared reasons, and the scope it is refused for."""
    named = " or ".join(f"`{scope}`" for scope in scopes)
    return {
        "description": (
            f"{_REFUSALS[403]} A token without {named} is refused with "
            "`scope_denied` (`admin` holds every scope). Codes registered for "
            "this status: " + ", ".join(f"`{code}`" for code in _codes_for(403)) + "."
        ),
        "content": {"application/json": {"schema": dict(_REFUSAL_REF)}},
    }


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
        "form posts are left out. Derived from the app once, at start; "
        "`/openapi.json` is the whole-app document. Each operation declares the "
        "bearer scheme where it reads a credential and the token scope it "
        "needs, `Idempotency-Key` where the middleware accepts one, `If-Match` "
        "and `ETag` where the route reads or sets them, the run headers on "
        "writes, and the Refusal body on its 4xx and 5xx answers, with 404 and "
        "428 where the route's code refuses with them."
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


_logger = logging.getLogger(__name__)


class AgentDocument:
    """The agent document of one app, built once.

    ``athena-serve`` calls ``start`` as soon as its listener is bound, so the
    build (seconds: FastAPI's own document, then the code reader) runs in a
    thread while the server answers; a request for the document that arrives
    first waits for it. A server that never calls ``start`` builds it on the
    first request."""

    def __init__(self, app: FastAPI, transport: Transport) -> None:
        self._app = app
        self._transport = transport
        self._document: dict[str, Any] | None = None
        self._lock = threading.Lock()

    @property
    def built(self) -> bool:
        return self._document is not None

    def get(self) -> dict[str, Any]:
        """The document, building it if no build has finished."""
        with self._lock:
            if self._document is None:
                self._document = build_agent_openapi(self._app, self._transport)
            return self._document

    def start(self) -> threading.Thread:
        """Build the document in a thread of its own; returns the thread."""

        def build() -> None:
            try:
                self.get()
            except Exception:
                # The first request builds it again and answers the error.
                _logger.exception("the agent OpenAPI document failed to build")

        thread = threading.Thread(target=build, name="agent-openapi", daemon=True)
        thread.start()
        return thread
