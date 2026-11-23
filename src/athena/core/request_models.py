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

from pydantic import BaseModel, ConfigDict, Field


class RequestBody(BaseModel):
    """A write's JSON body that refuses fields it does not declare."""

    model_config = ConfigDict(extra="forbid")


class CoordinationRootSelector(BaseModel):
    """A ``coordination_root`` selector: one root of this instance's root
    catalog, named by the catalog's digest and the root's key. The operator
    hands it out through the qualification workflow (docs/ACTIVE_WORK.md); no
    read returns it. The claim command validates it again and resolves it; this
    model only closes the shape an agent may send."""

    model_config = ConfigDict(extra="forbid", strict=True)

    catalog_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="The SHA-256 of the operator catalog's exact bytes, as the operator gave it.",
    )
    root_key: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$",
        description="The catalog root's key, as the operator gave it.",
    )
    relative_prefix: str = Field(
        default="",
        max_length=256,
        description="Relative POSIX offset inside that root; empty for its top.",
    )
