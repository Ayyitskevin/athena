"""Extract and run the examples in Athena's entry documents (A15b).

The machinery behind ``tests/test_doc_examples.py``; the tables that say what
each document needs (its workspace, its preconditions, its exclusions) live in
that test module, so this file holds no per-document knowledge.

Recognised forms, taken from what the five entry documents actually contain:

- a fenced ``http`` block: a request line ``METHOD /path``, header lines, a
  blank line, an optional JSON body;
- a fenced ``bash`` or ``sh`` block: each ``curl`` command in it is a request
  (``-X``, ``-H``, ``-d``; ``-d`` without ``-X`` is a POST; ``-f`` means it
  must succeed). Every other command in the block is a shell line, named in
  the test module's table of shell commands or the block fails;
- a fenced ``json`` block: the response to the request just before it, a
  response to a request the prose describes (a table entry), the arguments of
  a tool call (a table entry), or an ``mcpServers`` configuration;
- a fenced ``markdown`` block: text that names tool calls;
- an inline tool call: ``name(arguments)`` inside a code span, in prose or in
  a ``markdown`` block. Arguments are ``...`` (others elided), ``name``,
  ``name?`` (optional), ``name=value`` or a positional value; a value is a
  quoted string, an integer, ``true``/``false``, ``...`` or ``<placeholder>``.

A request's documented status is the first ``HTTP NNN`` in the prose after it
(after its response block, before the next block or heading); with none, any
2xx. Anything this module cannot read is a failure, never a skip.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shlex
from typing import Any

from fastapi.testclient import TestClient
import httpx
from mcp.server.fastmcp.exceptions import ToolError

from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"

_FENCE = re.compile(r"^```(\S*)\s*$")
_HEADING = re.compile(r"^#{1,6} ")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_CALL = re.compile(r"\b([a-z][a-z0-9_]*)\(([^()]*)\)")
_OPEN_CALL = re.compile(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\((?![^`]*\))")
_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
_REQUEST_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$")
_JSON_KEY = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')
_CODE_VALUE = re.compile(r'"code"\s*:\s*"([^"]+)"')
_ISSUE_PATH = re.compile(r"^/issues/(\d+)(?:/|$)")
_SHELL_VARIABLE = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\}?")
_PROFILE_NAMED = re.compile(
    r"`(seat\+knowledge|conductor|full|seat)`"
    r"|ATHENA_MCP_PROFILE=(seat\+knowledge|conductor|full|seat)\b"
    r"|\b(seat\+knowledge|conductor)\b"
    r"|\b(?:conductor/)?(full) profiles?\b"
)


# --- reading a document ----------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """One fenced block: its language, body and where it sits."""

    doc: str
    lang: str
    body: str
    line: int  # the opening fence, 1-based
    end: int  # the closing fence

    def label(self) -> str:
        first = next((ln for ln in self.body.splitlines() if ln.strip()), "")
        return f"{self.doc}:{self.line} ```{self.lang} {first.strip()[:60]}"


def fenced_blocks(doc: str, text: str) -> list[Block]:
    blocks: list[Block] = []
    lines = text.splitlines()
    opened: tuple[str, int] | None = None
    body: list[str] = []
    for number, line in enumerate(lines, start=1):
        fence = _FENCE.match(line)
        if opened is None:
            if fence:
                opened, body = (fence.group(1), number), []
            continue
        if line.strip() == "```":
            blocks.append(Block(doc, opened[0], "\n".join(body), opened[1], number))
            opened = None
        else:
            body.append(line)
    if opened is not None:
        raise ValueError(f"{doc}:{opened[1]}: a fence is never closed")
    return blocks


def prose_after(text: str, block: Block, blocks: list[Block]) -> str:
    """The prose between a block and the next block or heading."""
    lines = text.splitlines()
    following = [b.line for b in blocks if b.line > block.end]
    stop = min(following) - 1 if following else len(lines)
    window: list[str] = []
    for line in lines[block.end : stop]:
        if _HEADING.match(line):
            break
        window.append(line)
    return "\n".join(window)


def documented_status(prose: str) -> int | None:
    found = _HTTP_STATUS.search(prose)
    return int(found.group(1)) if found else None


# --- requests ----------------------------------------------------------------------


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    must_succeed: bool = False


def parse_http(body: str) -> Request:
    lines = body.splitlines()
    match = _REQUEST_LINE.match(lines[0].strip()) if lines else None
    if match is None:
        raise ValueError(f"an http block has no request line: {body[:80]!r}")
    headers: dict[str, str] = {}
    rest = lines[1:]
    while rest and rest[0].strip():
        name, _, value = rest.pop(0).partition(":")
        headers[name.strip()] = value.strip()
    payload = "\n".join(rest).strip()
    return Request(
        match.group(1),
        match.group(2),
        headers,
        json.loads(payload) if payload else None,
    )


def shell_commands(body: str) -> list[list[str]]:
    """The block's commands as argument lists, comments dropped, a pipeline cut
    at its first ``|`` and a list cut at ``&&``/``&`` (what follows is a command
    of its own)."""
    commands: list[list[str]] = []
    pending = ""
    for line in body.splitlines():
        pending = f"{pending}\n{line}" if pending else line
        if pending.rstrip().endswith("\\"):
            pending = pending.rstrip()[:-1]
            continue
        try:
            words = shlex.split(pending, comments=True)
        except ValueError:  # an open quote: the command continues
            continue
        pending = ""
        current: list[str] = []
        piped = False
        for word in words:
            if word in {"&&", "&", ";"}:
                if current:
                    commands.append(current)
                current, piped = [], False
            elif word == "|":
                piped = True
            elif not piped:
                current.append(word)
        if current:
            commands.append(current)
    if pending.strip():
        raise ValueError(f"an unterminated shell command: {pending[:80]!r}")
    return commands


def parse_curl(words: list[str], env: dict[str, str]) -> Request:
    def expand(value: str) -> str:
        def one(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise ValueError(f"curl reads ${name}, which no step provides")
            return env[name]

        return _SHELL_VARIABLE.sub(one, value)

    method = None
    url = None
    headers: dict[str, str] = {}
    data = None
    must_succeed = False
    remaining = list(words[1:])
    while remaining:
        word = remaining.pop(0)
        if word == "-X":
            method = remaining.pop(0)
        elif word == "-H":
            name, _, value = expand(remaining.pop(0)).partition(":")
            headers[name.strip()] = value.strip()
        elif word in {"-d", "--data"}:
            data = expand(remaining.pop(0))
        elif word.startswith("-") and not word.startswith("--"):
            must_succeed = must_succeed or "f" in word
            unknown = set(word[1:]) - {"s", "S", "f"}
            if unknown:
                raise ValueError(f"curl flag {word} is not one this harness reads")
        elif url is None:
            url = expand(word)
        else:
            raise ValueError(f"curl has a second URL: {word}")
    if url is None:
        raise ValueError("curl names no URL")
    parsed = httpx.URL(url)
    path = parsed.raw_path.decode("ascii")
    return Request(
        method or ("POST" if data is not None else "GET"),
        path,
        headers,
        json.loads(data) if data is not None else None,
        must_succeed,
    )


# --- tool calls ----------------------------------------------------------------------


@dataclass(frozen=True)
class Arg:
    name: str | None  # None for a positional value
    value: str | None  # the raw value text, None when only the name is shown
    optional: bool = False
    elided: bool = False  # a bare `...`: other arguments are not shown


@dataclass(frozen=True)
class Call:
    doc: str
    line: int
    tool: str
    text: str
    args: tuple[Arg, ...]
    paragraph: str

    def label(self) -> str:
        return f"{self.doc}:{self.line} `{self.text}`"


def _split_arguments(raw: str) -> list[str]:
    parts: list[str] = []
    depth, quote, current = 0, "", ""
    for char in raw:
        if quote:
            current += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "[{<":
            depth += 1
        elif char in "]}>":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def parse_arguments(raw: str) -> tuple[Arg, ...]:
    args: list[Arg] = []
    for part in _split_arguments(raw):
        if part in {"...", "…"}:
            args.append(Arg(None, None, elided=True))
        elif part.endswith("?") and _IDENTIFIER.fullmatch(part[:-1]):
            args.append(Arg(part[:-1], None, optional=True))
        elif _IDENTIFIER.fullmatch(part) and part not in {"true", "false"}:
            args.append(Arg(part, None))
        elif "=" in part and _IDENTIFIER.fullmatch(part.split("=", 1)[0].strip()):
            name, value = (piece.strip() for piece in part.split("=", 1))
            args.append(Arg(name, value))
        else:
            args.append(Arg(None, part))
    return tuple(args)


def _paragraphs(text: str, *, list_items: bool) -> Iterator[tuple[int, str]]:
    """(first line, text) of each paragraph; with ``list_items`` a line that
    starts a list item also starts a paragraph."""
    start, current = 0, []
    for number, line in enumerate(text.splitlines(), start=1):
        begins_item = list_items and line.lstrip().startswith("- ")
        if not line.strip() or begins_item:
            if current:
                yield start, "\n".join(current)
            current, start = ([line], number) if begins_item else ([], 0)
            continue
        if not current:
            start = number
        current.append(line)
    if current:
        yield start, "\n".join(current)


def tool_calls(doc: str, text: str, blocks: list[Block]) -> list[Call]:
    """Every inline tool call: code spans in prose and in ``markdown`` blocks.

    A code span that opens a snake_case call and never closes it fails, so a
    form this reader does not know cannot slip past it."""
    lines = text.splitlines()
    prose = list(lines)
    for block in blocks:
        for number in range(block.line - 1, block.end):
            prose[number] = ""
    sources = [("\n".join(prose), 0, False)]
    sources += [
        (block.body, block.line, True) for block in blocks if block.lang == "markdown"
    ]
    calls: list[Call] = []
    for source, offset, list_items in sources:
        for start, paragraph in _paragraphs(source, list_items=list_items):
            for span_line, span in _spans(paragraph):
                where = offset + start + span_line
                if _OPEN_CALL.search(span):
                    raise ValueError(f"{doc}:{where}: unreadable call in `{span}`")
                for match in _CALL.finditer(span):
                    name = match.group(1)
                    if name not in mcp_server.TOOL_SCOPES and "_" not in name:
                        continue  # a word before a parenthesis, not a tool
                    calls.append(
                        Call(
                            doc,
                            where,
                            name,
                            match.group(0),
                            parse_arguments(match.group(2)),
                            paragraph,
                        )
                    )
    return sorted(calls, key=lambda call: call.line)


def _spans(paragraph: str) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(paragraph.splitlines()):
        for match in _CODE_SPAN.finditer(line):
            yield number, match.group(1)


def profiles_named(paragraph: str) -> set[str]:
    named: set[str] = set()
    for match in _PROFILE_NAMED.finditer(paragraph):
        named.update(group for group in match.groups() if group)
    return named


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "boolean": (bool,),
    "number": (int, float),
    "array": (list,),
    "object": (dict,),
}


def literal(value: str) -> tuple[bool, Any]:
    """(is a concrete value, the value); placeholders are not concrete."""
    if value in {"...", "…"} or (value.startswith("<") and value.endswith(">")):
        return False, None
    if value in {"true", "false"}:
        return True, value == "true"
    if re.fullmatch(r"-?\d+", value):
        return True, int(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return True, value[1:-1]
    raise ValueError(f"cannot read the argument value {value!r}")


def schema_types(schema: dict[str, Any]) -> set[str]:
    """The JSON types a property allows; empty when one option is untyped (a
    nested model's ``$ref``), whose shape the tool checks when it runs."""
    options = schema.get("anyOf") or [schema]
    if any("type" not in option for option in options):
        return set()
    return {option["type"] for option in options}


def type_matches(value: Any, schema: dict[str, Any]) -> bool:
    allowed = schema_types(schema)
    if not allowed:
        return True
    for name in allowed:
        if name == "null" and value is None:
            return True
        expected = _JSON_TYPES.get(name, ())
        if isinstance(value, bool) and name in {"integer", "number"}:
            continue
        if isinstance(value, expected):
            return True
    return False


def check_call(call: Call, schema: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Problems with a call against the tool's input schema, and the concrete
    arguments it shows (name -> value)."""
    problems: list[str] = []
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    order = list(properties)
    shown: dict[str, Any] = {}
    named: set[str] = set()
    elided = any(arg.elided for arg in call.args)
    position = 0
    for arg in call.args:
        if arg.elided:
            continue
        name = arg.name
        if name is None:
            if position >= len(order):
                problems.append(f"positional value {arg.value} has no parameter")
                continue
            name = order[position]
        position += 1
        if name not in properties:
            problems.append(f"argument {name!r} is not in the tool's input schema")
            continue
        named.add(name)
        if arg.optional and name in required:
            problems.append(f"argument {name!r} is shown optional but is required")
        if arg.value is not None:
            try:
                concrete, value = literal(arg.value)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            if concrete:
                if not type_matches(value, properties[name]):
                    problems.append(
                        f"argument {name}={arg.value} is not of type "
                        f"{sorted(schema_types(properties[name]))}"
                    )
                shown[name] = value
    missing = sorted(required - named)
    if missing and not elided:
        problems.append(f"required arguments not shown: {missing}")
    return problems, shown


def check_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Problems with a JSON argument object against a tool's input schema:
    names and top-level types (the tool itself checks nested shapes when run)."""
    properties: dict[str, Any] = schema.get("properties", {})
    problems = [
        f"argument {name!r} is not in the tool's input schema"
        for name in arguments
        if name not in properties
    ]
    problems += [
        f"argument {name!r} is not of type {sorted(schema_types(properties[name]))}"
        for name, value in arguments.items()
        if name in properties and not type_matches(value, properties[name])
    ]
    return problems


# --- responses -----------------------------------------------------------------------


def all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.add(key)
            keys |= all_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            keys |= all_keys(inner)
    return keys


def check_status(status: int, expected: int | None) -> list[str]:
    if expected is not None and status != expected:
        return [f"status {status}, the document shows {expected}"]
    if expected is None and not 200 <= status < 300:
        return [f"status {status}, the document implies success"]
    return []


def check_shape(shown: str, body: Any) -> list[str]:
    """A documented response against a real one: every field name the document
    shows exists in the response, and a refusal ``code`` it shows is the code."""
    problems: list[str] = []
    missing = sorted(set(_JSON_KEY.findall(shown)) - all_keys(body))
    if missing:
        problems.append(f"fields the document shows and the response lacks: {missing}")
    code = _CODE_VALUE.search(shown)
    actual = body.get("code") if isinstance(body, dict) else None
    if code and code.group(1) != actual:
        problems.append(f"code {actual!r}, the document shows {code.group(1)!r}")
    return problems


# --- the workspace a document runs against -------------------------------------------


class Session:
    """An agent's MCP session: the tools one profile carries over its token."""

    def __init__(self, server: Any, profile: str) -> None:
        self.server = server
        self.profile = profile
        self.tools = {tool.name for tool in asyncio.run(server.list_tools())}

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        """(succeeded, the one JSON object the tool answered)."""
        try:
            content, _ = asyncio.run(self.server.call_tool(name, arguments))
        except ToolError as exc:
            return False, mcp_server.tool_failure(exc)
        return True, json.loads(content[0].text)


def session_for(app: Any, token: str, profile: str) -> Session:
    """The session athena-mcp would give this token under this profile: the
    scopes are probed the way ``main()`` probes them."""
    http = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    client = AthenaClient(client=http)
    scopes = mcp_server.probe_scopes(client)
    server = mcp_server.build_server(
        client, scopes=scopes, profile=mcp_server.PROFILES[profile]
    )
    return Session(server, profile)


@dataclass
class Workspace:
    """What one document's examples run against, and what its earlier steps left."""

    app: Any
    client: TestClient
    env: dict[str, str]  # the shell variables a curl reads; `<token>` is ATHENA_TOKEN
    admin_token: str | None = None
    session: Session | None = None
    issue_id: int | None = None
    last: httpx.Response | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    def bearer(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.env['ATHENA_TOKEN']}"}

    def as_admin(self) -> dict[str, str]:
        assert self.admin_token, "this workspace has no administrator token"
        return self.bearer(self.admin_token)


def substitute(request: Request, workspace: Workspace) -> Request:
    """Fill what the document shows as a placeholder or as a value from its own
    run: the bearer, the issue's current ETag and the caller's lease generation,
    each read as the caller."""
    headers = dict(request.headers)
    auth = headers.get("Authorization")
    if auth is not None and "<token>" in auth:
        headers["Authorization"] = auth.replace(
            "<token>", workspace.env["ATHENA_TOKEN"]
        )
    caller = {"Authorization": headers["Authorization"]} if auth is not None else {}
    issue = _ISSUE_PATH.match(request.path)
    if issue and "If-Match" in headers:
        current = workspace.client.get(f"/issues/{issue.group(1)}", headers=caller)
        headers["If-Match"] = current.headers.get("ETag", headers["If-Match"])
    body = request.body
    if issue and isinstance(body, dict) and "generation" in body:
        lease = workspace.client.get(f"/issues/{issue.group(1)}/lease", headers=caller)
        held = response_json(lease) if lease.status_code == 200 else None
        if isinstance(held, dict) and held.get("generation"):
            body = {**body, "generation": held["generation"]}
    return Request(request.method, request.path, headers, body, request.must_succeed)


def send(workspace: Workspace, request: Request) -> httpx.Response:
    kwargs: dict[str, Any] = {"headers": request.headers}
    if request.body is not None:
        kwargs["json"] = request.body
    response = workspace.client.request(request.method, request.path, **kwargs)
    workspace.last = response
    issue = _ISSUE_PATH.match(request.path)
    if issue:
        workspace.issue_id = int(issue.group(1))
    body = response_json(response)
    if isinstance(body, dict):
        _remember(body, workspace)
    return response


def _remember(body: dict[str, Any], workspace: Workspace) -> None:
    """Values a later step of the same document reads: the agent an onboarding
    created (its token and the MCP session its printed config describes), and
    the issue a create made."""
    if "mcp_config" in body:
        env = body["mcp_config"]["mcpServers"]["athena"]["env"]
        workspace.facts["mcp_config"] = body["mcp_config"]
        workspace.env["ATHENA_TOKEN"] = env["ATHENA_TOKEN"]
        workspace.session = session_for(
            workspace.app, env["ATHENA_TOKEN"], env["ATHENA_MCP_PROFILE"]
        )
    if {"id", "key", "title", "status"} <= set(body):
        workspace.issue_id = body["id"]


def response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


# --- the run -------------------------------------------------------------------------

Fingerprint = tuple[str, str]  # (document, text the block contains)


@dataclass
class Tables:
    """The per-document knowledge the test module owns (see its docstring)."""

    default_profile: dict[str, str]
    excluded: dict[Fingerprint, str]
    shell_commands: dict[str, str]
    preconditions: dict[Fingerprint, Callable[[Workspace], None]]
    provoked: dict[Fingerprint, Callable[[Workspace], httpx.Response]]
    arguments: dict[
        Fingerprint,
        Callable[[Workspace, dict[str, Any]], tuple[str, dict[str, Any]]],
    ]
    fillers: dict[str, Callable[[Workspace], Any]]


@dataclass
class Report:
    doc: str
    executed: list[str] = field(default_factory=list)
    validated: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    calls_run: list[str] = field(default_factory=list)
    calls_validated: list[str] = field(default_factory=list)
    shell_lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    blocks: int = 0
    calls: int = 0


def _matching(table: dict[Fingerprint, Any], block: Block) -> list[Fingerprint]:
    """The table entry for a block: the longest fingerprint the block contains
    (so "cmd --check" and "cmd" can name two blocks), or none."""
    keys = [key for key in table if key[0] == block.doc and key[1] in block.body]
    return sorted(keys, key=lambda key: len(key[1]), reverse=True)[:1]


@dataclass
class _Run:
    text: str
    blocks: list[Block]
    calls: list[Call]
    workspace: Workspace
    tables: Tables
    schemas: dict[str, Any]
    report: Report
    used: set[Fingerprint] = field(default_factory=set)
    answered: set[int] = field(default_factory=set)  # response blocks consumed


def run_document(
    doc: str, text: str, workspace: Workspace, tables: Tables, schemas: dict[str, Any]
) -> Report:
    """Run every example of one document in its order and report each one."""
    report = Report(doc)
    blocks = fenced_blocks(doc, text)
    try:
        calls = tool_calls(doc, text, blocks)
    except ValueError as exc:
        report.failures.append(str(exc))
        calls = []
    report.blocks, report.calls = len(blocks), len(calls)
    run = _Run(text, blocks, calls, workspace, tables, schemas, report)
    items: list[tuple[int, Block | Call]] = [(b.line, b) for b in blocks]
    items += [(c.line, c) for c in calls]
    for _, item in sorted(items, key=lambda pair: pair[0]):
        label = item.label()
        try:
            if isinstance(item, Call):
                _run_call(run, item)
            elif item.line not in run.answered:
                _run_block(run, item)
        except Exception as exc:  # noqa: BLE001 — every failure is reported, none skipped
            report.failures.append(f"{label}: {type(exc).__name__}: {exc}")
    tables_by_name = {
        "excluded": tables.excluded,
        "preconditions": tables.preconditions,
        "provoked": tables.provoked,
        "arguments": tables.arguments,
    }
    for name, table in tables_by_name.items():
        for key in table:
            if key[0] == doc and key not in run.used:
                report.failures.append(f"{name} entry {key} matches no block of {doc}")
    return report


def _run_block(run: _Run, block: Block) -> None:
    report, tables = run.report, run.tables
    label = block.label()
    excluded = _matching(tables.excluded, block)
    if excluded:
        run.used.update(excluded)
        report.excluded.append(f"{label} -- {tables.excluded[excluded[0]]}")
        return
    for key in _matching(tables.preconditions, block):
        run.used.add(key)
        tables.preconditions[key](run.workspace)
    if block.lang == "http":
        _run_requests(run, block, [parse_http(block.body)])
    elif block.lang in {"bash", "sh"}:
        requests = []
        for words in shell_commands(block.body):
            if words[0] == "curl":
                requests.append(parse_curl(words, run.workspace.env))
            elif words[0] in tables.shell_commands:
                report.shell_lines.append(f"{label}: {' '.join(words)[:72]}")
            else:
                report.failures.append(f"{label}: unknown shell command {words[0]!r}")
        if not requests:
            report.failures.append(f"{label}: a shell block with no request")
            return
        _run_requests(run, block, requests)
    elif block.lang == "json":
        _run_json(run, block)
    elif block.lang == "markdown":
        inside = [c for c in run.calls if block.line < c.line < block.end]
        if not inside:
            report.failures.append(f"{label}: a markdown block that names no tool call")
            return
        report.validated.append(f"{label} ({len(inside)} tool calls, each checked)")
    else:
        report.failures.append(f"{label}: a ```{block.lang} block no rule classifies")


def _response_block(run: _Run, block: Block) -> Block | None:
    """The json block directly after a request block (blank lines between)."""
    index = run.blocks.index(block)
    if index + 1 >= len(run.blocks):
        return None
    shown = run.blocks[index + 1]
    between = run.text.splitlines()[block.end : shown.line - 1]
    if shown.lang != "json" or any(line.strip() for line in between):
        return None
    return shown


def _run_requests(run: _Run, block: Block, requests: list[Request]) -> None:
    report, workspace = run.report, run.workspace
    shown = _response_block(run, block)
    expected = documented_status(prose_after(run.text, shown or block, run.blocks))
    for number, request in enumerate(requests):
        response = send(workspace, substitute(request, workspace))
        last = number == len(requests) - 1
        problems = check_status(response.status_code, expected if last else None)
        if request.must_succeed and response.status_code >= 400:
            problems.append(f"curl -f fails on status {response.status_code}")
        where = f"{block.label()}: {request.method} {request.path}"
        report.failures += [f"{where}: {p}: {response.text[:240]}" for p in problems]
    report.executed.append(block.label())
    if shown is not None and workspace.last is not None:
        run.answered.add(shown.line)
        problems = check_shape(shown.body, response_json(workspace.last))
        report.failures += [f"{shown.label()}: {p}" for p in problems]
        report.validated.append(f"{shown.label()} (response of line {block.line})")


def _run_json(run: _Run, block: Block) -> None:
    report, tables, workspace = run.report, run.tables, run.workspace
    label = block.label()
    provoked = _matching(tables.provoked, block)
    arguments = _matching(tables.arguments, block)
    if provoked:
        run.used.update(provoked)
        response = tables.provoked[provoked[0]](workspace)
        expected = documented_status(prose_after(run.text, block, run.blocks))
        problems = check_status(response.status_code, expected)
        problems += check_shape(block.body, response_json(response))
        report.failures += [f"{label}: {p}: {response.text[:240]}" for p in problems]
        report.validated.append(f"{label} (response of the request its prose names)")
        return
    if arguments:
        run.used.update(arguments)
        shown = json.loads(block.body)
        tool, full = tables.arguments[arguments[0]](workspace, shown)
        problems = check_arguments(shown, run.schemas[tool])
        report.failures += [f"{label}: {tool}: {p}" for p in problems]
        session = workspace.session
        if session is None or tool not in session.tools:
            report.failures.append(f"{label}: {tool} is not in the document's session")
            return
        ok, answer = session.call(tool, full)
        if not ok:
            report.failures.append(f"{label}: {tool} refused: {answer}")
        report.executed.append(f"{label} (run as {tool} arguments)")
        return
    try:
        parsed = json.loads(block.body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "mcpServers" in parsed:
        problems = check_mcp_config(parsed, workspace.facts.get("mcp_config"))
        report.failures += [f"{label}: {p}" for p in problems]
        report.validated.append(f"{label} (against the config Athena emitted)")
        return
    report.failures.append(f"{label}: a json block that no rule classifies")


def check_mcp_config(
    shown: dict[str, Any], emitted: dict[str, Any] | None
) -> list[str]:
    """A config the document shows against the one an earlier step received."""
    if emitted is None:
        return ["no earlier step of the document received an MCP config"]
    if set(shown) != set(emitted):
        return [f"top-level keys {sorted(shown)}, Athena emits {sorted(emitted)}"]
    mine, theirs = shown["mcpServers"]["athena"], emitted["mcpServers"]["athena"]
    problems: list[str] = []
    if set(mine) != set(theirs) or set(mine["env"]) != set(theirs["env"]):
        problems.append("its keys differ from the config Athena emitted")
    elif mine["env"]["ATHENA_MCP_PROFILE"] != theirs["env"]["ATHENA_MCP_PROFILE"]:
        problems.append(
            "its profile differs from the one Athena emitted for this token"
        )
    command = Path(mine.get("command", ""))
    if not command.is_absolute() or command.name != Path(theirs["command"]).name:
        problems.append("its command is not an absolute path to the same script")
    return problems


def _run_call(run: _Run, call: Call) -> None:
    report, workspace = run.report, run.workspace
    if call.tool not in run.schemas:
        report.failures.append(f"{call.label()}: no tool named {call.tool}")
        return
    problems, arguments = check_call(call, run.schemas[call.tool])
    profile = run.tables.default_profile[call.doc]
    named = profiles_named(call.paragraph)
    if not any(_carries(name, call.tool) for name in named | {profile}):
        problems.append(
            f"{call.tool} is not in the document's `{profile}` profile and its "
            f"paragraph names no profile that carries it (named: {sorted(named)})"
        )
    report.failures += [f"{call.label()}: {p}" for p in problems]
    session = workspace.session
    runnable = (
        not problems
        and session is not None
        and call.tool in session.tools
        and mcp_server.is_read_only(call.tool)
    )
    for arg in call.args if runnable else ():
        if arg.name is not None and arg.name not in arguments:
            filler = run.tables.fillers.get(arg.name)
            value = filler(workspace) if filler is not None else None
            if value is None:
                runnable = False
                break
            arguments[arg.name] = value
    if not runnable or session is None:
        report.calls_validated.append(call.label())
        return
    ok, answer = session.call(call.tool, arguments)
    if not ok:
        report.failures.append(f"{call.label()}: refused with {arguments}: {answer}")
    report.calls_run.append(f"{call.label()} with {arguments}")


def _carries(profile: str, tool: str) -> bool:
    members = mcp_server.PROFILES[profile]
    return members is None or tool in members


def input_schemas() -> dict[str, Any]:
    """Every registered tool's input schema, from the full surface."""

    class _NoServer:
        """Listing tools needs no Athena."""

    server = mcp_server.build_server(_NoServer())
    return {tool.name: tool.inputSchema for tool in asyncio.run(server.list_tools())}
