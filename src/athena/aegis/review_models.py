"""Transport-neutral strict declarations for exact-version review tracking.

An identity is supplied by the actor; validation never fetches or verifies it.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from athena.aegis import report_credentials
from athena.core.ids import MAX_SQLITE_INTEGER

ReviewCandidateId = Annotated[int, Field(strict=True, ge=1, le=MAX_SQLITE_INTEGER)]


class StrictReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CandidateIdentity(StrictReviewModel):
    kind: Literal["git", "artifact"]
    source: Annotated[str, Field(min_length=1, max_length=200)]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ValueError("source contains an unsupported control character")
        value = value.strip()
        if not value:
            raise ValueError("source must not be blank")
        if report_credentials.credential_kind(value) is not None:
            raise ValueError("source contains a credential; remove it and retry")
        return value

    @model_validator(mode="after")
    def exact_revision(self) -> Self:
        if self.kind == "artifact" and len(self.revision) != 64:
            raise ValueError("artifact revision must be a lowercase SHA-256 digest")
        return self


class CandidateSubmission(StrictReviewModel):
    identity: CandidateIdentity
    expected_previous_candidate_id: ReviewCandidateId | None


class CandidateSubmissionReceipt(StrictReviewModel):
    candidate_id: ReviewCandidateId
    completion_event_id: ReviewCandidateId


class CandidateReviewReceipt(StrictReviewModel):
    review_id: ReviewCandidateId
    candidate_id: ReviewCandidateId
    completion_event_id: ReviewCandidateId
