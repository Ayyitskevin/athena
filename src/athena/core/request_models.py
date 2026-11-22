"""The one base for an agent-facing write's JSON body: unknown fields are refused.

Pydantic ignores a field a model does not declare, so a misspelt or invented
field used to vanish and the write went ahead without it: a token request that
named another user minted a token for the caller, and a project sent as private
was created public. Every JSON request body in the agent OpenAPI document
(``/openapi-agent.json``) refuses such a field with a 422 that names it; a body
model gets that by deriving from :class:`RequestBody`.
``tests/test_request_bodies_closed.py`` walks that document and fails on a
body, or a model reachable from one, that still accepts unknown fields.

Only ``extra`` is set. Types are coerced exactly as before; a model that also
wants strict types says so itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RequestBody(BaseModel):
    """A write's JSON body that refuses fields it does not declare."""

    model_config = ConfigDict(extra="forbid")
