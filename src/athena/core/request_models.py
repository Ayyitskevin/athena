"""The one base for an agent-facing write's JSON body: unknown fields are refused.

Pydantic ignores a field a model does not declare, so a misspelt or invented
field used to vanish and the write went ahead without it: a token request that
named another user minted a token for the caller. A body model of a REST route
an MCP write tool calls (``scripts/generate_agent_api.py`` derives that map), and
``POST /tokens``, derives from :class:`RequestBody`, so such a field is a 422
that names it. ``tests/test_request_bodies_closed.py`` walks the OpenAPI
document and fails on such a model that still accepts unknown fields.

Only ``extra`` is set. Types are coerced exactly as before; a model that also
wants strict types says so itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RequestBody(BaseModel):
    """A write's JSON body that refuses fields it does not declare."""

    model_config = ConfigDict(extra="forbid")
