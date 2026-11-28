"""The MCP SDK's private parts Athena reads are where Athena reads them.

WHY: the server shortens each tool's listed schema, refuses an argument a tool
does not take, and answers every call as one JSON object. FastMCP has no
public API for any of the three, so ``mcp/server.py`` reads four private
names. They were read in six places; an SDK release that moved one would have
failed hundreds of tests with no word on why. They are now read in two
functions (K2.3 of docs/plans/CONSOLIDATION_SPEC.md), and this test names
each one against the installed SDK.
"""

from __future__ import annotations

import inspect
from pathlib import Path
import re

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from athena.mcp import server as mcp_server

PACKAGE = Path(mcp_server.__file__).resolve().parent.parent
#: The names FastMCP does not promise. ``fn_metadata`` is public on the tool
#: record, but the record itself is only reachable through ``_tool_manager``.
PRIVATE = ("_tool_manager", "_mcp_server")


def test_the_sdk_still_has_each_private_name_athena_reads():
    probe = FastMCP("probe")

    @probe.tool()
    def echo(text: str) -> str:
        return text

    record = mcp_server.sdk_tool(probe, "echo")
    assert record is not None, "FastMCP._tool_manager.get_tool no longer finds a tool"
    assert mcp_server.sdk_tool(probe, "no_such_tool") is None
    assert isinstance(record.parameters, dict), "Tool.parameters is not the schema"
    model = record.fn_metadata.arg_model
    assert isinstance(model, type) and issubclass(model, BaseModel), (
        "Tool.fn_metadata.arg_model is not the pydantic argument model"
    )
    assert "arg_model" in type(record.fn_metadata).model_fields

    low_level = mcp_server.sdk_protocol_server(probe)
    assert isinstance(low_level.request_handlers, dict), (
        "FastMCP._mcp_server.request_handlers is not the handler table"
    )
    assert "validate_input" in inspect.signature(low_level.call_tool).parameters, (
        "the low-level call_tool registration no longer takes validate_input"
    )


def test_no_other_module_reads_the_sdks_private_names():
    reads: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in PRIVATE:
            for found in re.finditer(rf"\.{name}\b", text):
                line = text.count("\n", 0, found.start()) + 1
                reads.append(f"{path.relative_to(PACKAGE)}:{line} {name}")
    seam = {read.rsplit(" ", 1)[1] for read in reads}
    assert seam == set(PRIVATE), reads
    assert all(read.startswith("mcp/server.py:") for read in reads), reads
    assert len(reads) == len(PRIVATE), reads
