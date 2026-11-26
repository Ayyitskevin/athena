"""Assign an issue to a declared fleet seat over REST (decision 8).

The cockpit's "Assign to a seat" (Admin > Fleet, a chair on the floor) and this
route call one command, ``fleet_assign_commands.assign_issue_to_seat``: look the
seat up by its roster slug, delegate the issue to the seat's agent account and
make it the assignee in one transaction, then radio the seat over Buzz. The
route lives in ``workflows`` with its command, as the playbook route does. It
keeps the ``/issues/{issue_id}/...`` path because the resource is the issue.

Who may: a write role whose token holds ``issue:write`` (``admin`` includes it),
which is who the floor's chair form serves; then the issue commands' own gate
(creator, assignee, delegated contributor or admin) inside the transaction. An
issue the caller cannot see is 404 exactly as a missing one, and a seat whose
agent cannot see the issue's project is refused as an unknown account is. The
radio is optional: a ping that fails or is skipped never undoes the assign, and
the answer says what the radio did.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from athena.aegis import issue_commands, issue_etags, issues
from athena.aegis.rest_support import issue_command_http_error
from athena.core.deps import get_conn
from athena.core.identity import issue_write_actor
from athena.core.ids import RowIdPath
from athena.core.refusals import Refusal
from athena.core.request_models import RequestBody
from athena.workflows import fleet_assign_commands

router = APIRouter(prefix="/issues", tags=["workflows"])

logger = logging.getLogger("athena")

#: What a bearer caller is told when the radio or its receipt failed. The
#: underlying reason can carry a key file's path, an OS error or the Buzz CLI's
#: stderr: the operator's log keeps it, the API answer does not.
RADIO_FAILED_DETAIL = "the radio failed; the server log has the reason"
RECEIPT_FAILED_DETAIL = "the receipt was not recorded; the server log has the reason"


def _one_line(text: object) -> str:
    """A reason as one log line: the CLI's stderr can hold line breaks, and a
    forged line in the operator's log is worse than an escaped one."""
    return str(text).replace("\r", "\\r").replace("\n", "\\n")


def _without_failure_reasons(assigned: dict, issue_id: int) -> dict:
    """The command's answer with a failed radio's or receipt's own reason
    replaced by a fixed sentence, logged for the operator instead."""
    radio = dict(assigned["radio"])
    receipt = dict(assigned["receipt"])
    if radio.get("status") == "failed":
        logger.warning(
            "seat assign of issue %s: radio failed: %s",
            issue_id,
            _one_line(radio.get("detail")),
        )
        radio["detail"] = RADIO_FAILED_DETAIL
    if receipt.get("status") == "failed":
        logger.warning(
            "seat assign of issue %s: receipt failed: %s",
            issue_id,
            _one_line(receipt.get("detail")),
        )
        receipt["detail"] = RECEIPT_FAILED_DETAIL
    return {**assigned, "radio": radio, "receipt": receipt}


STATUS_BY_KIND: dict[str, int] = {
    "invalid": 422,
    "not_found": 404,
    "conflict": 409,
}
CODE_BY_KIND: dict[str, str] = {
    "invalid": "validation_error",
    "not_found": "not_found",
    "conflict": "conflict",
}


class SeatAssignmentIn(RequestBody):
    seat_slug: str = Field(
        max_length=40, description="the declared seat's slug, as the roster names it"
    )
    note: str = Field(
        default="",
        max_length=200,
        description="one line the radio ping carries to the seat; optional",
    )


class SeatRadioOut(BaseModel):
    # sent, skipped (no radio configured, or the seat has no Buzz key) or failed.
    status: str
    detail: str | None = None
    # Where the ping can be followed; null unless it landed with a receipt.
    permalink: str | None = None


class SeatRadioReceiptOut(BaseModel):
    # recorded: a radioed_assignment event ties the assign to its ping.
    status: Literal["recorded", "skipped", "failed"]
    detail: str | None = None
    activity_id: int | None = None
    permalink: str | None = None


class SeatAssignmentOut(BaseModel):
    issue_id: int
    issue_key: str
    seat: str
    seat_name: str
    agent_id: int = Field(description="the seat's account, now assignee and delegate")
    radio: SeatRadioOut
    receipt: SeatRadioReceiptOut


@router.post("/{issue_id}/assign-seat", response_model=SeatAssignmentOut)
def assign_issue_to_seat(
    issue_id: RowIdPath,
    payload: SeatAssignmentIn,
    response: Response,
    actor: dict = Depends(issue_write_actor),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Hand an issue to a declared fleet seat by its slug: the seat's agent
    account is delegated the issue and becomes its assignee, in one
    transaction, then the seat is radioed over Buzz. The radio never undoes
    the assign; `radio` and `receipt` say what it did. Needs a write role and
    `issue:write`, then the issue write gate (creator, assignee, delegated
    contributor or admin). An issue you cannot see is 404, like a missing one;
    an unknown seat, the operator's seat, a seat with no agent account, or a
    seat whose agent cannot see the issue's project is 422. The `ETag` header
    is the issue's new tag, for the next guarded write."""
    try:
        assigned = fleet_assign_commands.assign_issue_to_seat(
            conn,
            actor=actor,
            issue_id=issue_id,
            seat_slug=payload.seat_slug,
            note=payload.note,
        )
    except fleet_assign_commands.AssignError as exc:
        raise Refusal(
            status_code=STATUS_BY_KIND[exc.kind],
            detail=exc.detail,
            code=CODE_BY_KIND[exc.kind],
        ) from exc
    except issue_commands.IssueCommandError as exc:
        raise issue_command_http_error(exc) from exc
    # The assign changed the issue's assignee, so its tag moved (C9).
    current = issues.get_issue(conn, int(assigned["issue_id"]))
    assert current is not None
    response.headers["ETag"] = issue_etags.current_etag(conn, current)
    return _without_failure_reasons(assigned, issue_id)
