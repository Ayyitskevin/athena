"""Generate docs/AGENT_API.md — the MCP tool ↔ scope ↔ REST mapping.

The three agent transports (MCP tools, REST routes, and the docs describing
them) drift apart when any of them is maintained by hand. This generator
derives the one table OpenAPI cannot produce — which MCP tool needs which
token scope and which REST call it performs — from the code itself:

- the tool list and scopes come from ``athena.mcp.server.TOOL_SCOPES``
  (registration is fail-closed on that dict, so it cannot under-report);
- each tool's REST calls are read from the tool bodies in ``server.py``
  (which ``AthenaClient`` methods it calls) joined to the HTTP verb + path
  literals in ``client.py``;
- descriptions come from the registered FastMCP tools themselves.

Usage:
    python scripts/generate_agent_api.py            # rewrite docs/AGENT_API.md
    python scripts/generate_agent_api.py --check    # exit 1 if the doc drifted

``tests/test_agent_api_doc.py`` runs the check, so the gate fails when the
committed doc no longer matches the code.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "athena" / "mcp" / "server.py"
CLIENT = REPO / "src" / "athena" / "mcp" / "client.py"
DOC = REPO / "docs" / "AGENT_API.md"

HTTP_VERBS = {"get", "post", "put", "patch", "delete"}

# Presentation order and labels for the scope groups (sentinels included:
# server.py declares READ_ONLY = "read" and ANY_WRITE_SCOPE = "write").
SCOPE_ORDER = ["read", "issue:write", "docs:write", "write", "admin"]
SCOPE_HEADINGS = {
    "read": "Read tools — scope: `read`",
    "issue:write": "Aegis write tools — scope: `issue:write`",
    "docs:write": "Mentor write tools — scope: `docs:write`",
    "write": "Cross-module write tools — scope: any write scope",
    "admin": "Operator tools — scope: `admin`",
}


def _fstring_template(node: ast.AST) -> str | None:
    """Render a path literal ("/issues") or f-string (f"/issues/{ref}") as a
    template string; None if the node is neither."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif isinstance(piece, ast.FormattedValue):
                parts.append("{" + ast.unparse(piece.value) + "}")
        return "".join(parts)
    return None


def client_method_calls() -> dict[str, list[str]]:
    """Map each AthenaClient method to the HTTP calls its body performs,
    rendered as "VERB /path/{param}" strings."""
    tree = ast.parse(CLIENT.read_text())
    classes = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AthenaClient"
    ]
    if len(classes) != 1:
        raise SystemExit("client.py no longer defines exactly one AthenaClient")

    calls_by_method: dict[str, list[str]] = {}
    for method in classes[0].body:
        if not isinstance(method, ast.FunctionDef):
            continue
        found: list[str] = []
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # self._client.<verb>("/path", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr in HTTP_VERBS
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "_client"
                and node.args
            ):
                path = _fstring_template(node.args[0])
                if path is not None and path.startswith("/"):
                    found.append(f"{func.attr.upper()} {path}")
            # self._mutate(self._client.<verb>, "/path", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "_mutate"
                and len(node.args) >= 2
            ):
                verb_arg = node.args[0]
                if isinstance(verb_arg, ast.Attribute) and verb_arg.attr in HTTP_VERBS:
                    path = _fstring_template(node.args[1])
                    if path is not None and path.startswith("/"):
                        found.append(f"{verb_arg.attr.upper()} {path}")
        if found:
            calls_by_method[method.name] = found
    return calls_by_method


def tool_client_methods() -> dict[str, list[str]]:
    """Map each @tool / @mutation_tool function in server.py to the client
    methods its body calls (in first-seen order, deduplicated)."""
    tree = ast.parse(SERVER.read_text())
    tools: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(
            isinstance(d, ast.Name) and d.id in ("tool", "mutation_tool")
            for d in node.decorator_list
        ):
            continue
        methods: list[str] = []
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "client"
                and call.func.attr not in methods
            ):
                methods.append(call.func.attr)
        tools[node.name] = methods
    return tools


def registered_descriptions() -> dict[str, str]:
    """First docstring line of every registered tool, via the real registry."""
    sys.path.insert(0, str(REPO / "src"))
    from athena.mcp.server import build_server  # noqa: PLC0415 — after sys.path

    server = build_server(None, scopes=None)  # type: ignore[arg-type]
    try:
        registered = server._tool_manager.list_tools()  # noqa: SLF001
        pairs = [(t.name, t.description or "") for t in registered]
    except AttributeError:  # future mcp versions: fall back to the async API
        import asyncio  # noqa: PLC0415

        listed = asyncio.run(server.list_tools())
        pairs = [(t.name, t.description or "") for t in listed]
    summaries: dict[str, str] = {}
    for name, desc in pairs:
        first = (desc.strip().splitlines() or [""])[0].strip()
        if first and first[-1] not in ".!?:":
            first += " …"
        summaries[name] = first
    return summaries


def render() -> str:
    sys.path.insert(0, str(REPO / "src"))
    from athena.mcp.server import TOOL_SCOPES  # noqa: PLC0415

    methods = client_method_calls()
    tool_methods = tool_client_methods()
    descriptions = registered_descriptions()

    # Fail loud on any mismatch between the three sources: a tool the AST pass
    # found but TOOL_SCOPES does not know (or vice versa) means server.py
    # changed shape and this generator must be updated with it.
    ast_tools = set(tool_methods)
    scoped_tools = set(TOOL_SCOPES)
    if ast_tools != scoped_tools:
        missing = sorted(scoped_tools - ast_tools)
        extra = sorted(ast_tools - scoped_tools)
        raise SystemExit(
            f"tool inventory drifted: in TOOL_SCOPES only {missing}; "
            f"decorated only {extra}"
        )
    unregistered = sorted(scoped_tools - set(descriptions))
    if unregistered:
        raise SystemExit(f"tools not registered with FastMCP: {unregistered}")

    lines = [
        "# Agent API — MCP tools, scopes, and the REST calls behind them",
        "",
        "<!-- GENERATED FILE — do not edit by hand. -->",
        "<!-- Regenerate: python scripts/generate_agent_api.py -->",
        "<!-- Drift check: tests/test_agent_api_doc.py (runs in the gate) -->",
        "",
        "Every MCP tool goes through the REST API with the session's bearer",
        "token — never around it — so this table is a *mapping*, not a second",
        "surface. The full REST reference is the app's own `/openapi.json`",
        "(browse it at `/redoc`); this page adds the two columns OpenAPI",
        "cannot: which **token scope** each MCP tool requires, and which REST",
        "call(s) it performs.",
        "",
        "At runtime the tool list itself is scope-filtered: a session's token",
        "only sees the tools it can use ([RUNTIME_RECIPE.md](RUNTIME_RECIPE.md)),",
        "and `admin` implies every scope. A tool shown as *(client-side)*",
        "composes other calls or local state instead of one fixed route.",
        "",
    ]

    by_scope: dict[str, list[str]] = {s: [] for s in SCOPE_ORDER}
    for name in sorted(TOOL_SCOPES):
        by_scope[TOOL_SCOPES[name]].append(name)

    for scope in SCOPE_ORDER:
        names = by_scope[scope]
        if not names:
            continue
        lines += [f"## {SCOPE_HEADINGS[scope]}", ""]
        lines += ["| MCP tool | REST call(s) | Summary |", "|---|---|---|"]
        for name in names:
            rest_calls: list[str] = []
            for m in tool_methods[name]:
                rest_calls.extend(methods.get(m, []))
            rest = (
                "<br>".join(f"`{c}`" for c in dict.fromkeys(rest_calls))
                if rest_calls
                else "*(client-side)*"
            )
            summary = descriptions[name].replace("|", "\\|")
            lines.append(f"| `{name}` | {rest} | {summary} |")
        lines.append("")

    lines += [
        "---",
        "",
        f"*{len(TOOL_SCOPES)} tools. Generated from `mcp/server.py` "
        "(`TOOL_SCOPES` + tool bodies) and `mcp/client.py` (verb + path "
        "literals); the registration path is fail-closed, so a tool missing "
        "here cannot exist in the server either.*",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    content = render()
    if "--check" in sys.argv[1:]:
        current = DOC.read_text() if DOC.exists() else ""
        if current != content:
            sys.stderr.write(
                "docs/AGENT_API.md is stale — regenerate with "
                "`python scripts/generate_agent_api.py`\n"
            )
            return 1
        print("docs/AGENT_API.md matches the code")
        return 0
    DOC.write_text(content)
    print(f"wrote {DOC.relative_to(REPO)} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
