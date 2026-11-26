"""A review request answers the reviewer's completion, and pickup names it (F2, D16).

FLEET-4, reproduced by the lead on the conductor recipe:

(a) The recipe links the review issue ``blocked_by`` the build issue, and the
    build stays open while it is under review. The desk showed one review request
    while ``claim_next_ready`` answered ``nothing_ready`` and said nothing about
    it; only a direct ``claim_issue`` reached the review.
(b) After the reviewer's own HOLD completion on the review issue and a second
    request by the conductor, the desk showed the request, a ready total of 0,
    and the review issue under ``not_offered: completed_by_you``. Pickup again
    answered ``nothing_ready``.

The amended rule (decision record
``docs/decisions/2026-11-22-review-request-answers.md``): the issue's newest
review request, naming the completer, made by another account after the
completion, native and about the source's current candidate, answers that
completion for that account and nobody else. It lifts no ``needs_input`` hold
and changes no blocker. And every pickup answer that claims nothing carries
``review_request``: the newest waiting request the desk shows, with the
``claim_issue`` call that takes its review issue, or null.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
import pytest
from support import ADMIN, bearer, bootstrap, make_app, onboard_agent

from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


class Board:
    """The operator (the conductor here, on REST), a builder, a reviewer and a
    second eligible account, in one project."""

    def __init__(self, client: TestClient, path):
        self.client = client
        self.path = path
        self.bo = onboard_agent(client, email="bo@e.com", name="Bo")
        self.rex = onboard_agent(client, email="rex@e.com", name="Rex")
        self.tia = onboard_agent(client, email="tia@e.com", name="Tia")
        self.project = ok(
            client.post("/projects", json={"name": "Rev", "key": "REV"}, headers=ADMIN),
            201,
        )["id"]
        self.candidates: list[int] = []

    def issue(self, title: str, *agents) -> int:
        made = ok(
            self.client.post(
                "/issues",
                json={"title": title, "project_id": self.project},
                headers=ADMIN,
            ),
            201,
        )
        for agent in agents:
            ok(
                self.client.post(
                    f"/issues/{made['id']}/delegate",
                    json={"user_id": agent["user"]["id"]},
                    headers=ADMIN,
                ),
                201,
            )
        return made["id"]

    def etag(self, agent, issue_id: int) -> str:
        return self.client.get(f"/issues/{issue_id}", headers=bearer(agent)).headers[
            "ETag"
        ]

    def claim(self, agent, issue_id: int) -> dict:
        return ok(
            self.client.post(
                f"/issues/{issue_id}/claim",
                json={},
                headers={**bearer(agent), "If-Match": self.etag(agent, issue_id)},
            ),
            201,
        )

    def complete(self, agent, issue_id: int, generation: str, **report) -> dict:
        return ok(
            self.client.post(
                f"/issues/{issue_id}/complete",
                json={"generation": generation, **report},
                headers=bearer(agent),
            )
        )

    def submit(self, build: int, revision: str) -> int:
        lease = self.claim(self.bo, build)
        previous = self.candidates[-1] if self.candidates else None
        done = self.complete(
            self.bo,
            build,
            lease["generation"],
            verdict="PASS",
            candidate_submission={
                "identity": {"kind": "git", "source": "rev/x", "revision": revision},
                "expected_previous_candidate_id": previous,
            },
        )
        self.candidates.append(done["candidate_submission"]["candidate_id"])
        return self.candidates[-1]

    def request(self, review: int, reviewer, headers=ADMIN) -> dict:
        return ok(
            self.client.post(
                f"/issues/{review}/review-requests",
                json={
                    "candidate_id": self.candidates[-1],
                    "reviewer_id": reviewer["user"]["id"],
                },
                headers=headers,
            )
        )

    def hold(self, review: int, reviewer) -> None:
        lease = self.claim(reviewer, review)
        self.complete(
            reviewer,
            review,
            lease["generation"],
            verdict="HOLD",
            review_candidate_id=self.candidates[-1],
        )

    def pickup(self, agent, key: str | None = None) -> dict:
        headers = bearer(agent)
        if key is not None:
            headers["Idempotency-Key"] = key
        return ok(self.client.post("/issues/claim-next", json={}, headers=headers))

    def views(self, agent) -> tuple[list[int], dict[int, str], list[int]]:
        """The ready rule as every surface reports it, asserting they agree:
        (ready ids, not_offered reasons, the desk's asked review issues)."""
        desk = ok(self.client.get("/desk", headers=bearer(agent)))
        lane = desk["work"]["ready"]
        ids = [row["issue"]["id"] for row in lane["items"]]
        alone = ok(self.client.get("/desk/ready", headers=bearer(agent)))
        query = ok(
            self.client.get("/issues", params={"q": "is:ready"}, headers=bearer(agent))
        )["issues"]
        assert [row["issue"]["id"] for row in alone["items"]] == ids
        assert lane["total"] == alone["total"] == len(ids)
        assert sorted(row["id"] for row in query) == sorted(ids)
        not_offered = {
            row["issue"]["id"]: row["reason"]
            for row in desk["work"]["waiting_on_operator"]["not_offered"]["items"]
        }
        asked = [
            row["review_issue_id"]
            for row in (desk["asks"].get("review_requests") or {}).get("items", [])
            if row["status"] == "requested"
        ]
        return ids, not_offered, asked


@pytest.fixture
def board(tmp_path):
    app, path = make_app(tmp_path, "review-ready.db")
    with TestClient(app) as client:
        bootstrap(client)
        yield Board(client, path), app


def _names(answer: dict, board: Board, review: int, request: dict) -> None:
    named = answer["review_request"]
    assert named is not None, answer
    assert named["request_id"] == request["request_id"]
    assert named["review_issue_id"] == review
    assert named["candidate_id"] == board.candidates[-1]
    assert named["pending_total"] == 1
    assert named["next"]["tool"] == "claim_issue"
    assert named["next"]["arguments"] == {
        "issue_id": review,
        "if_match": board.etag(board.rex, review),
    }


def test_a_blocked_review_is_named_by_pickup_and_its_call_claims_it(board):
    """Baseline (a): the recipe's own link. The blocker keeps its meaning, so
    pickup does not claim the review; it names it with the call that does."""
    board, _app = board
    build = board.issue("Build", board.bo)
    review = board.issue("Review", board.rex)
    ok(
        board.client.post(
            f"/issues/{review}/links",
            json={"target_ref": str(build), "relation": "blocked_by"},
            headers=ADMIN,
        ),
        201,
    )
    board.submit(build, "a" * 40)
    request = board.request(review, board.rex)

    ready, held, asked = board.views(board.rex)
    assert (ready, held, asked) == ([], {}, [review])

    answer = board.pickup(board.rex)
    assert answer["claimed"] is False and answer["reason"] == "nothing_ready"
    _names(answer, board, review, request)

    follow = answer["review_request"]["next"]
    lease = ok(
        board.client.post(
            f"/issues/{follow['arguments']['issue_id']}/claim",
            json={},
            headers={**bearer(board.rex), "If-Match": follow["arguments"]["if_match"]},
        ),
        201,
    )
    assert lease["holder_id"] == board.rex["user"]["id"]
    # Picked up: the desk no longer asks, and pickup holds rather than names.
    assert board.views(board.rex)[2] == []
    holding = board.pickup(board.rex)
    assert holding["reason"] == "already_holding"
    assert holding["review_request"] is None


def test_a_second_request_after_the_reviewers_hold_is_ready_for_it(board):
    """Baseline (b): after its own HOLD and a second request, the review issue
    was not_offered completed_by_you and pickup said nothing_ready."""
    board, _app = board
    build = board.issue("Build", board.bo)
    review = board.issue("Review", board.rex)
    board.submit(build, "a" * 40)
    board.request(review, board.rex)
    first = board.pickup(board.rex)
    assert first["claimed"] and first["issue"]["id"] == review
    board.complete(
        board.rex,
        review,
        first["lease"]["generation"],
        verdict="HOLD",
        review_candidate_id=board.candidates[-1],
    )
    # The HOLD is the newest act: the completion holds the issue back.
    assert board.views(board.rex) == ([], {review: "completed_by_you"}, [])

    board.submit(build, "b" * 40)
    board.request(review, board.rex)
    ready, held, asked = board.views(board.rex)
    assert (ready, held, asked) == ([review], {}, [review])

    # A newer candidate supersedes the request: the desk stops asking and the
    # completion holds again, on every surface.
    board.submit(build, "c" * 40)
    assert board.views(board.rex) == ([], {review: "completed_by_you"}, [])
    empty = board.pickup(board.rex)
    assert empty["reason"] == "nothing_ready" and empty["review_request"] is None

    board.request(review, board.rex)
    assert board.views(board.rex) == ([review], {}, [review])
    again = board.pickup(board.rex)
    assert again["claimed"] is True and again["issue"]["id"] == review


def test_a_request_answers_only_the_account_it_names_and_only_from_another(board):
    board, _app = board
    build = board.issue("Build", board.bo)
    review = board.issue("Review", board.rex, board.tia)
    board.submit(build, "a" * 40)
    board.hold(review, board.rex)
    assert board.views(board.rex) == ([], {review: "completed_by_you"}, [])

    # The conductor names Tia: Rex's hold stands.
    board.request(review, board.tia)
    assert board.views(board.rex) == ([], {review: "completed_by_you"}, [])
    assert board.views(board.tia) == ([review], {}, [review])

    # Rex asks itself: the desk shows the request, but it is not an answer.
    board.submit(build, "b" * 40)
    board.request(review, board.rex, headers=bearer(board.rex))
    assert board.views(board.rex) == ([], {review: "completed_by_you"}, [review])
    answer = board.pickup(board.rex)
    assert answer["reason"] == "nothing_ready"
    assert answer["review_request"]["review_issue_id"] == review

    # Another account asks Rex: that answers Rex's completion.
    board.submit(build, "c" * 40)
    board.request(review, board.rex)
    assert board.views(board.rex) == ([review], {}, [review])


def test_a_request_lifts_no_needs_input_hold(board):
    board, _app = board
    build = board.issue("Build", board.bo)
    review = board.issue("Review", board.rex, board.tia)
    board.submit(build, "a" * 40)
    lease = board.claim(board.tia, review)
    ok(
        board.client.post(
            f"/issues/{review}/yield",
            json={
                "generation": lease["generation"],
                "reason": "needs_input",
                "attempted_work": "Read the candidate.",
                "evidence": ["notes"],
                "blocking_question": "Which revision?",
                "resume_instructions": "Answer, then resume.",
            },
            headers=bearer(board.tia),
        ),
        201,
    )
    board.request(review, board.rex)
    ready, held, asked = board.views(board.rex)
    assert (ready, held, asked) == ([], {review: "awaiting_answer"}, [review])
    answer = board.pickup(board.rex)
    assert answer["reason"] == "nothing_ready"
    assert answer["review_request"]["review_issue_id"] == review


def test_a_request_the_reviewer_cannot_see_is_absent_everywhere(board):
    board, _app = board
    ok(
        board.client.post(
            f"/projects/{board.project}/members",
            json={"user_id": board.rex["user"]["id"]},
            headers=ADMIN,
        ),
        201,
    )
    ok(
        board.client.put(
            f"/projects/{board.project}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
    )
    ok(
        board.client.post(
            f"/projects/{board.project}/members",
            json={"user_id": board.bo["user"]["id"]},
            headers=ADMIN,
        ),
        201,
    )
    build = board.issue("Build", board.bo)
    review = board.issue("Review", board.rex)
    ok(
        board.client.post(
            f"/issues/{review}/links",
            json={"target_ref": str(build), "relation": "blocked_by"},
            headers=ADMIN,
        ),
        201,
    )
    board.submit(build, "a" * 40)
    board.request(review, board.rex)
    assert board.pickup(board.rex)["review_request"]["review_issue_id"] == review

    ok(
        board.client.delete(
            f"/projects/{board.project}/members/{board.rex['user']['id']}",
            headers=ADMIN,
        )
    )
    assert board.views(board.rex) == ([], {}, [])
    answer = board.pickup(board.rex)
    assert answer["reason"] == "nothing_ready" and answer["review_request"] is None


def test_a_replayed_pickup_returns_the_review_it_named(board):
    board, _app = board
    build = board.issue("Build", board.bo)
    review = board.issue("Review", board.rex)
    ok(
        board.client.post(
            f"/issues/{review}/links",
            json={"target_ref": str(build), "relation": "blocked_by"},
            headers=ADMIN,
        ),
        201,
    )
    board.submit(build, "a" * 40)
    board.request(review, board.rex)
    first = board.pickup(board.rex, key="rex-poll-1")
    board.claim(board.rex, review)
    # A receipt is historical: the replay names the request as it was.
    assert board.pickup(board.rex, key="rex-poll-1") == first
    assert board.pickup(board.rex, key="rex-poll-2")["review_request"] is None


def test_the_mcp_tool_names_the_review_as_rest_does(board):
    board, app = board
    build = board.issue("Build", board.bo)
    review = board.issue("Review", board.rex)
    ok(
        board.client.post(
            f"/issues/{review}/links",
            json={"target_ref": str(build), "relation": "blocked_by"},
            headers=ADMIN,
        ),
        201,
    )
    board.submit(build, "a" * 40)
    request = board.request(review, board.rex)
    with TestClient(app, headers=bearer(board.rex)) as transport:
        server = mcp_server.build_server(
            AthenaClient(client=transport),
            scopes=frozenset({"read", "issue:write"}),
            profile=mcp_server.PROFILES["seat"],
        )

        def call(name, arguments):
            content, _ = asyncio.run(server.call_tool(name, arguments))
            return json.loads(content[0].text)

        answer = call("claim_next_ready", {})
        assert answer["reason"] == "nothing_ready"
        _names(answer, board, review, request)
        follow = answer["review_request"]["next"]
        lease = call(follow["tool"], follow["arguments"])
        assert lease["holder_id"] == board.rex["user"]["id"]
