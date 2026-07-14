"""Agent run check-in REST API.

An external agent periodically refreshes one run's server-owned check-in row so
the operator can distinguish recent reporting from stale history. The caller's
authenticated identity and the server clock are authoritative: clients may name
only the run they are reporting, never an actor or timestamp.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, WithJsonSchema

from athena.core import agent_run_commands
from athena.core.deps import get_conn
from athena.core.identity import personal_write_actor

router = APIRouter(prefix="/agent-runs", tags=["core"])

RunId = Annotated[
    Any,
    # The command is the single validation boundary. Keeping the Pydantic field
    # opaque avoids FastAPI echoing a lone surrogate in RequestValidationError
    # (which Starlette cannot UTF-8 encode) while retaining an honest OpenAPI shape.
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "pattern": r"^[^\x00-\x1F\x7F]+$",
        }
    ),
]


class AgentRunHeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: RunId


class AgentRunHeartbeatOut(BaseModel):
    # Deliberately omit identity/token internals and raw server timing inputs. The
    # response says only which authenticated agent/run was refreshed and its
    # operator-facing reporting state.
    agent_id: int
    run_id: str
    first_seen_at: str
    last_seen_at: str
    age_seconds: int
    reporting_state: Literal["reporting_recently", "stale"]


@router.put("/heartbeat", response_model=AgentRunHeartbeatOut)
def heartbeat_agent_run(
    payload: AgentRunHeartbeatIn,
    actor: dict = Depends(personal_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Refresh the authenticated agent's run check-in using the server clock.

    This PUT intentionally does not use Athena's durable idempotency receipts:
    every repeated call is a fresh liveness observation and must reach the command.
    """
    try:
        return agent_run_commands.heartbeat(
            conn,
            actor=actor,
            run_id=payload.run_id,
        )
    except agent_run_commands.AgentRunCommandError as exc:
        status_code = {
            "unauthorized": 401,
            "forbidden": 403,
            "invalid": 422,
            "capacity": 409,
        }[exc.kind]
        raise HTTPException(status_code=status_code, detail=exc.detail) from exc
