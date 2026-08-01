"""The answerability REST API: the ask-and-answer ledger, read-only.

One endpoint, admin-only. The web agents page and the MCP tool render the same
projection (`core/answerability.py`); no adapter computes a number of its own.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from athena.core import answerability
from athena.core.deps import get_conn
from athena.core.identity import admin_actor

router = APIRouter(prefix="/fleet/answerability", tags=["core"])


class ControlCountsOut(BaseModel):
    open: int
    expired_unanswered: int
    completed: int
    declined: int


class KillCountsOut(BaseModel):
    told_to_stop: int
    confirmed: int


class ApprovalCountsOut(BaseModel):
    pending: int
    approved: int
    rejected: int


class AgentAnswerabilityOut(BaseModel):
    agent_id: int
    agent_name: str
    paused: bool
    run_controls: ControlCountsOut
    worker_kills: KillCountsOut
    approvals: ApprovalCountsOut
    # The agent's native events an undo has reversed — recorded corrections,
    # not a verdict on the agent.
    reversed_events: int


class AnswerabilityOut(BaseModel):
    schema_: str = Field(alias="schema")
    generated_at: str
    agents: list[AgentAnswerabilityOut]


@router.get("", response_model=AnswerabilityOut)
def answerability_ledger(
    agent_id: int | None = Query(
        None, ge=1, le=2**63 - 1, description="narrow the ledger to one agent user id"
    ),
    _actor: dict = Depends(admin_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    # Asks and answers per agent, zero-filled. Admin-only: the ledger names
    # every agent and how often the operator corrected each — fleet
    # intelligence, same bar as /security and /activity/chain.
    ledger = answerability.build_answerability(conn, agent_id=agent_id)
    if agent_id is not None and not ledger["agents"]:
        raise HTTPException(status_code=404, detail="no such agent")
    return ledger
