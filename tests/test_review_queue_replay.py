"""A retry repeats one historical completion; it never republishes its candidate.

All fixtures and domain writes use the real HTTP API with scoped bearer actors.
SQL is used only to observe exact lease, association and completion-event state.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

from fastapi.testclient import TestClient
import pytest

from athena.core import db, idempotency
from athena.main import create_app

ADMIN = {"X-Athena-Actor": "1"}


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def headers(agent, *, key=None, run=None):
    result = {
        "Authorization": f"Bearer {agent['token']['token']}",
        "X-Athena-Run": run or f"review-replay-{agent['user']['id']}",
    }
    if key is not None:
        result["Idempotency-Key"] = key
    return result


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "review-replay.db"
    with TestClient(create_app(path)) as client:
        checked(
            client.post(
                "/users", json={"email": "admin@example.invalid", "name": "Admin"}
            ),
            201,
        )
        actors = {}
        for name in ("builder", "reviewer"):
            actors[name] = checked(
                client.post(
                    "/users/onboard_agent",
                    json={
                        "email": f"{name}@example.invalid",
                        "name": name,
                        "scopes": ["read", "issue:write"],
                    },
                    headers=ADMIN,
                ),
                201,
            )
        project = checked(
            client.post(
                "/projects", json={"name": "Reviews", "key": "RPL"}, headers=ADMIN
            ),
            201,
        )
        items = {}
        for role in actors:
            items[role] = checked(
                client.post(
                    "/issues",
                    json={"title": role, "project_id": project["id"]},
                    headers=ADMIN,
                ),
                201,
            )
            checked(
                client.post(
                    f"/issues/{items[role]['id']}/delegate",
                    json={"user_id": actors[role]["user"]["id"]},
                    headers=ADMIN,
                ),
                201,
            )
    return {"path": path, "actors": actors, "items": items, "project": project}


def claim(client, board, role):
    item = board["items"][role]
    actor_headers = headers(board["actors"][role])
    current = client.get(f"/issues/{item['id']}", headers=actor_headers)
    checked(current)
    return checked(
        client.post(
            f"/issues/{item['id']}/claim",
            headers={**actor_headers, "If-Match": current.headers["ETag"]},
        ),
        201,
    )


def submission(previous=None, revision="a" * 40):
    return {
        "identity": {"kind": "git", "source": "owner/repository", "revision": revision},
        "expected_previous_candidate_id": previous,
    }


def submit(client, board, *, previous=None, revision="a" * 40):
    held = claim(client, board, "builder")
    return checked(
        client.post(
            f"/issues/{board['items']['builder']['id']}/complete",
            json={
                "generation": held["generation"],
                "candidate_submission": submission(previous, revision),
            },
            headers=headers(board["actors"]["builder"]),
        )
    )["candidate_submission"]


def prepare(client, board, mode):
    if mode == "submission":
        role = "builder"
        extension = {"candidate_submission": submission()}
    else:
        first = submit(client, board)
        role = "reviewer"
        extension = {"review_candidate_id": first["candidate_id"], "verdict": "PASS"}
    held = claim(client, board, role)
    return (
        f"/issues/{board['items'][role]['id']}/complete",
        {"generation": held["generation"], **extension},
        headers(board["actors"][role], key=f"one-{mode}"),
    )


def snapshot(path):
    conn = db.connect(path)
    try:
        result = {
            table: [
                dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")
            ]
            for table in ("issue_review_candidates", "issue_candidate_reviews")
        }
        result["leases"] = [
            dict(row)
            for row in conn.execute("SELECT * FROM issue_leases ORDER BY issue_id")
        ]
        result["completions"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM activity WHERE verb='claim_completed' ORDER BY id"
            )
        ]
        return result
    finally:
        conn.close()


def queue_item(client, board):
    packet = checked(
        client.get(
            f"/projects/{board['project']['id']}/review-queue",
            headers=headers(board["actors"]["reviewer"]),
        )
    )
    assert packet["visible_total"] == packet["returned"] == 1
    return packet["items"][0]


@pytest.mark.parametrize("mode", ["submission", "review"])
def test_same_key_replays_after_release_and_application_restart(board, mode):
    with TestClient(create_app(board["path"])) as client:
        path, body, actor_headers = prepare(client, board, mode)
        first = client.post(path, json=body, headers=actor_headers)
        checked(first)
        before = snapshot(board["path"])
        assert before["leases"] == []
        assert len(before["issue_review_candidates"]) == 1
        assert len(before["issue_candidate_reviews"]) == (mode == "review")
        assert len(before["completions"]) == (2 if mode == "review" else 1)
        again = client.post(path, json=body, headers=actor_headers)
        checked(again)
        assert again.headers["Idempotent-Replay"] == "true"
        assert again.content == first.content
        assert snapshot(board["path"]) == before
    # First app and its lifespan are stopped before the fresh app opens the DB.
    with TestClient(create_app(board["path"])) as restarted:
        replay = restarted.post(path, json=body, headers=actor_headers)
        checked(replay)
        assert replay.headers["Idempotent-Replay"] == "true"
        assert replay.content == first.content
        assert snapshot(board["path"]) == before


@pytest.mark.parametrize("mode", ["submission", "review"])
def test_historical_receipt_cannot_restore_a_superseded_candidate(board, mode):
    with TestClient(create_app(board["path"])) as client:
        path, body, actor_headers = prepare(client, board, mode)
        original = client.post(path, json=body, headers=actor_headers)
        checked(original)
        candidate_a = queue_item(client, board)["candidate"]["candidate_id"]
        candidate_b = submit(client, board, previous=candidate_a, revision="b" * 40)
        before = snapshot(board["path"])
        replay = client.post(path, json=body, headers=actor_headers)
        checked(replay)
        assert replay.headers["Idempotent-Replay"] == "true"
        assert replay.content == original.content
        assert snapshot(board["path"]) == before
        current = queue_item(client, board)
        assert current["candidate"]["candidate_id"] == candidate_b["candidate_id"]
        assert current["review_state"] == "awaiting_review"
        assert current["latest_review"] is None
        context = checked(
            client.get(
                f"/issues/{board['items']['builder']['id']}/work-context",
                headers=headers(board["actors"]["reviewer"]),
            )
        )
        history = context["review_tracking"]["recent_candidates"]["items"]
        assert [row["candidate"]["candidate_id"] for row in history] == [
            candidate_b["candidate_id"],
            candidate_a,
        ]
        assert [row["is_current"] for row in history] == [True, False]
        assert history[1]["review_state"] == (
            "reported_pass" if mode == "review" else "awaiting_review"
        )


@pytest.mark.parametrize("mode", ["submission", "review"])
def test_same_key_under_another_run_replays_and_writes_nothing(board, mode):
    """The key names the write, not the session (F14): a restarted session
    retries under a new run and gets the first answer."""
    with TestClient(create_app(board["path"])) as client:
        path, body, actor_headers = prepare(client, board, mode)
        first = client.post(path, json=body, headers=actor_headers)
        checked(first)
        before = snapshot(board["path"])
        restarted = {**actor_headers, "X-Athena-Run": "the-session-after-a-restart"}
        replay = client.post(path, json=body, headers=restarted)
        checked(replay)
        assert replay.content == first.content
        assert replay.headers["Idempotent-Replay"] == "true"
        assert snapshot(board["path"]) == before


@pytest.mark.parametrize("mode", ["submission", "review"])
@pytest.mark.parametrize("change", ["version", "generation"])
def test_same_key_cannot_change_version_or_generation(board, mode, change):
    with TestClient(create_app(board["path"])) as client:
        path, body, actor_headers = prepare(client, board, mode)
        first = client.post(path, json=body, headers=actor_headers)
        checked(first)
        before = snapshot(board["path"])
        changed = deepcopy(body)
        changed_headers = dict(actor_headers)
        if change == "generation":
            changed["generation"] = "f" * 32
        elif mode == "submission":
            changed["candidate_submission"]["identity"]["revision"] = "c" * 40
        else:
            changed["review_candidate_id"] += 1
        refused = client.post(path, json=changed, headers=changed_headers)
        assert refused.status_code == 409
        assert refused.json()["code"] == "idempotency_mismatch"
        assert snapshot(board["path"]) == before
        replay = client.post(path, json=body, headers=actor_headers)
        checked(replay)
        assert replay.content == first.content
        assert replay.headers["Idempotent-Replay"] == "true"
        assert snapshot(board["path"]) == before


@pytest.mark.parametrize("mode", ["submission", "review"])
@pytest.mark.parametrize("receipt", ["pending", "published"])
def test_simultaneous_duplicate_key_commits_one_association(
    board, mode, receipt, monkeypatch
):
    original_paused = threading.Event()
    release_original = threading.Event()
    publish = idempotency.complete

    def pause_original():
        original_paused.set()
        assert release_original.wait(timeout=30)

    def publish_receipt(*args, **kwargs):
        # Keep the first HTTP request open on either side of the real receipt commit.
        if receipt == "pending":
            pause_original()
        result = publish(*args, **kwargs)
        if receipt == "published":
            pause_original()
        return result

    with (
        TestClient(create_app(board["path"])) as owner,
        TestClient(create_app(board["path"], idempotency_wait_seconds=0)) as caller,
    ):
        path, body, actor_headers = prepare(owner, board, mode)
        before = snapshot(board["path"])
        monkeypatch.setattr(idempotency, "complete", publish_receipt)

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(owner.post, path, json=body, headers=actor_headers)
            try:
                assert original_paused.wait(timeout=30)
                duplicate = caller.post(path, json=body, headers=actor_headers)
            finally:
                release_original.set()
            original = first.result(timeout=30)

        checked(original)
        assert original.headers.get("Idempotent-Replay") is None
        if receipt == "pending":
            assert checked(duplicate, 409)["code"] == "idempotency_in_progress"
            assert duplicate.headers["Retry-After"] == "1"
            assert duplicate.headers.get("Idempotent-Replay") is None
            committed = snapshot(board["path"])
            duplicate = caller.post(path, json=body, headers=actor_headers)
            assert snapshot(board["path"]) == committed
        checked(duplicate)
        assert duplicate.headers["Idempotent-Replay"] == "true"
        assert duplicate.content == original.content
    after = snapshot(board["path"])
    assert after["leases"] == []
    changed_table = (
        "issue_review_candidates" if mode == "submission" else "issue_candidate_reviews"
    )
    assert len(after[changed_table]) == len(before[changed_table]) + 1
    assert len(after["completions"]) == len(before["completions"]) + 1


@pytest.mark.parametrize("mode", ["submission", "review"])
def test_receipt_publication_failure_keeps_committed_association_and_fences_retry(
    board, mode, monkeypatch
):
    with TestClient(create_app(board["path"]), raise_server_exceptions=False) as client:
        path, body, actor_headers = prepare(client, board, mode)
        before = snapshot(board["path"])

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic receipt publication failure")

        with monkeypatch.context() as patch:
            patch.setattr(idempotency, "complete", fail)
            response = client.post(path, json=body, headers=actor_headers)
        assert response.status_code == 500
        after = snapshot(board["path"])
        assert after["leases"] == []
        changed_table = (
            "issue_review_candidates"
            if mode == "submission"
            else "issue_candidate_reviews"
        )
        assert len(after[changed_table]) == len(before[changed_table]) + 1
        assert len(after["completions"]) == len(before["completions"]) + 1
        linked = after[changed_table][-1]
        assert linked["completion_event_id"] == after["completions"][-1]["id"]
        retry = client.post(path, json=body, headers=actor_headers)
        assert retry.status_code == 409
        assert retry.json()["code"] == "idempotency_indeterminate"
        assert snapshot(board["path"]) == after
        # A new key cannot repeat the old lease release or create another row.
        fresh = client.post(
            path,
            json=body,
            headers={**actor_headers, "Idempotency-Key": "reconciled-intent"},
        )
        assert fresh.status_code == 409
        assert snapshot(board["path"]) == after
        assert queue_item(client, board)["review_state"] == (
            "reported_pass" if mode == "review" else "awaiting_review"
        )
