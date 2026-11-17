"""Declared exact identities stay strict and carry an explicit predecessor."""

import pytest
from pydantic import ValidationError

from athena.aegis.review_models import CandidateIdentity, CandidateSubmission


def test_explicit_first_submission_retains_nested_null():
    value = CandidateSubmission.model_validate(
        {
            "identity": {"kind": "git", "source": " owner/repo ", "revision": "a" * 40},
            "expected_previous_candidate_id": None,
        }
    )
    assert value.model_dump() == {
        "identity": {"kind": "git", "source": "owner/repo", "revision": "a" * 40},
        "expected_previous_candidate_id": None,
    }


@pytest.mark.parametrize("previous", [True, False, 0, -1, 2**63, "1", 1.0])
def test_predecessor_is_a_strict_signed64_id(previous):
    with pytest.raises(ValidationError):
        CandidateSubmission.model_validate(
            {
                "identity": {
                    "kind": "artifact",
                    "source": "file",
                    "revision": "a" * 64,
                },
                "expected_previous_candidate_id": previous,
            }
        )


def test_predecessor_must_be_explicit():
    with pytest.raises(ValidationError):
        CandidateSubmission.model_validate(
            {"identity": {"kind": "git", "source": "repo", "revision": "a" * 40}}
        )


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "branch"},
        {"revision": "main"},
        {"revision": "a" * 39},
        {"revision": "A" * 40},
        {"kind": "artifact", "revision": "a" * 40},
        {"source": " "},
        {"source": "a" * 201},
        {"source": "bad\x00source"},
        {"source": 123},
        {"extra": "no"},
    ],
)
def test_identity_refuses_ambiguous_or_unbounded_input(change):
    with pytest.raises(ValidationError):
        CandidateIdentity.model_validate(
            {"kind": "git", "source": "repo", "revision": "a" * 40, **change}
        )


@pytest.mark.parametrize("kind,length", [("git", 40), ("git", 64), ("artifact", 64)])
def test_supported_identity_positive_controls(kind, length):
    value = {"kind": kind, "source": "repo/file", "revision": "b" * length}
    assert CandidateIdentity.model_validate(value).model_dump() == value
