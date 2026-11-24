"""A claim renewed again and again with no recorded work is raised (C7,
FLEET-12).

Before: a holder that only renewed and heartbeated read as ``pinging`` and
nothing raised it. The issue stayed out of every other seat's reach for as
long as the holder kept asking, and neither the conductor that delegated it
nor the operator was told. Now each renewal's answer says how many renewals
have passed with no work of the holder's recorded on the issue; the renewal
that reaches the threshold records ``no_progress`` once and tells who answers
for the work; and the live claim carries the reason in attention.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, bearer, bootstrap, login, make_app, onboard_agent

from athena import config
from athena.aegis import fleet_work
from athena.core import db

VERB = "no_progress"


def _events(db_file, verb: str = VERB) -> list[dict]:
    conn = db.connect(db_file)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT id, actor_id, target_id, detail FROM activity "
                "WHERE verb = ? ORDER BY id",
                (verb,),
            )
        ]
    finally:
        conn.close()


def _told(db_file) -> list[int]:
    conn = db.connect(db_file)
    try:
        return [
            row["user_id"]
            for row in conn.execute(
                "SELECT n.user_id FROM notifications n "
                "JOIN activity a ON a.id = n.event_id WHERE a.verb = ? "
                "ORDER BY n.user_id",
                (VERB,),
            )
        ]
    finally:
        conn.close()


def _world(client: TestClient) -> dict:
    """A conductor delegates one issue to a seat, which claims it. A second
    agent is the assignee and a third only watches."""
    bootstrap(client)
    cora = onboard_agent(client, email="cora@e.com", name="Cora Conductor")
    sol = onboard_agent(client, email="sol@e.com", name="Sol Seat")
    ada = onboard_agent(client, email="ada@e.com", name="Ada Assignee")
    bo = onboard_agent(client, email="bo@e.com", name="Bo Watcher")
    made = client.post("/issues", json={"title": "slice"}, headers=bearer(cora))
    assert made.status_code == 201, made.text
    issue_id = made.json()["id"]
    assigned = client.put(
        f"/issues/{issue_id}/assignee",
        json={"assignee_id": ada["user"]["id"]},
        headers=ADMIN,
    )
    assert assigned.status_code == 200, assigned.text
    handed = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": sol["user"]["id"]},
        headers=bearer(cora),
    )
    assert handed.status_code == 201, handed.text
    watched = client.post(
        "/watches",
        json={"target_kind": "issue", "target_id": issue_id},
        headers=bearer(bo),
    )
    assert watched.status_code in (200, 201, 204), watched.text
    world = {"cora": cora, "sol": sol, "ada": ada, "bo": bo, "issue_id": issue_id}
    world["claim"] = _claim(client, world)
    return world


def _claim(client: TestClient, world: dict) -> dict:
    sol, issue_id = world["sol"], world["issue_id"]
    tag = client.get(f"/issues/{issue_id}", headers=bearer(sol)).headers["ETag"]
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        json={},
        headers={**bearer(sol), "If-Match": tag},
    )
    assert claimed.status_code == 201, claimed.text
    return claimed.json()


def _renew(client: TestClient, world: dict) -> dict:
    renewed = client.post(
        f"/issues/{world['issue_id']}/claim",
        json={"generation": world["claim"]["generation"]},
        headers=bearer(world["sol"]),
    )
    assert renewed.status_code in (200, 201), renewed.text
    return renewed.json()


def _comment(client: TestClient, world: dict, who: str) -> None:
    said = client.post(
        f"/issues/{world['issue_id']}/comments",
        json={"body": "where it stands"},
        headers=bearer(world[who]),
    )
    assert said.status_code == 201, said.text


def _claim_reasons(client: TestClient, who, issue_id: int) -> tuple[str, str] | None:
    headers = who if who is ADMIN else bearer(who)
    ranking = client.get(
        "/attention/ranking",
        params={"signals": "claim_needs_attention"},
        headers=headers,
    )
    assert ranking.status_code == 200, ranking.text
    for row in ranking.json()["items"]:
        if row["source_id"] == issue_id:
            return row["severity"], row["reason"]
    return None


def test_a_renewal_says_how_many_have_passed_with_no_work(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        counts = [_renew(client, world)["renewals_without_progress"] for _ in range(2)]
        # Another account's comment is not the holder making progress.
        _comment(client, world, "cora")
        counts.append(_renew(client, world)["renewals_without_progress"])
        # The holder's own work starts the count again.
        _comment(client, world, "sol")
        counts.append(_renew(client, world)["renewals_without_progress"])

    # A claim's answer has no count: nothing has been renewed.
    assert "renewals_without_progress" not in world["claim"]
    assert counts == [1, 2, 3, 1]


def test_the_renewal_that_reaches_the_threshold_tells_who_answers_once(tmp_path):
    assert config.NO_PROGRESS_RENEWALS == 3
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _renew(client, world)
        _renew(client, world)
        before = (_events(db_file), _told(db_file))
        _renew(client, world)
        at_threshold = (_events(db_file), _told(db_file))
        _renew(client, world)
        _renew(client, world)
        after = (_events(db_file), _told(db_file))

    assert before == ([], [])
    (event,) = at_threshold[0]
    sol_id = world["sol"]["user"]["id"]
    assert event["actor_id"] == sol_id and event["target_id"] == world["issue_id"]
    assert event["detail"] == (
        f"generation {world['claim']['generation']}; holder_id {sol_id}; "
        "renewals 3; holder Sol Seat"
    )
    # The delegating account and the assignee; not the holder, who read the
    # count on its own answer, and not an account that only watches.
    assert at_threshold[1] == sorted(
        [world["cora"]["user"]["id"], world["ada"]["user"]["id"]]
    )
    assert after == at_threshold


def test_work_then_another_run_of_renewals_raises_it_again(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        for _ in range(3):
            _renew(client, world)
        _comment(client, world, "sol")
        for _ in range(2):
            _renew(client, world)
        one = len(_events(db_file))
        _renew(client, world)
        two = len(_events(db_file))

    assert (one, two) == (1, 2)


def test_the_holder_is_not_told_when_it_is_also_the_assignee(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        assigned = client.put(
            f"/issues/{world['issue_id']}/assignee",
            json={"assignee_id": world["sol"]["user"]["id"]},
            headers=ADMIN,
        )
        assert assigned.status_code == 200, assigned.text
        for _ in range(3):
            _renew(client, world)

    assert _told(db_file) == [world["cora"]["user"]["id"]]


def test_the_live_claim_carries_the_reason_for_who_answers_and_the_operator(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _renew(client, world)
        _renew(client, world)
        before = _claim_reasons(client, ADMIN, world["issue_id"])
        _renew(client, world)
        raised = {
            name: _claim_reasons(client, who, world["issue_id"])
            for name, who in (
                ("operator", ADMIN),
                ("cora", world["cora"]),
                ("ada", world["ada"]),
                ("bo", world["bo"]),
            )
        }
        work = client.get("/fleet/active-work", headers=ADMIN).json()["items"]
        _comment(client, world, "sol")
        after = _claim_reasons(client, ADMIN, world["issue_id"])

    assert before is not None and VERB not in before[1].split("; ")
    for name in ("operator", "cora", "ada"):
        severity, reason = raised[name]
        assert VERB in reason.split("; "), (name, reason)
        # Alive and quiet is not yet a decision: it ranks with a missed
        # heartbeat, under every question and approval.
        assert severity == "medium"
    assert raised["bo"] is None
    (item,) = [w for w in work if w["issue"]["id"] == world["issue_id"]]
    assert item["lease"]["renewals_without_progress"] == 3
    assert VERB in item["attention_reasons"]
    assert after is not None and VERB not in after[1].split("; ")


@pytest.mark.parametrize(("threshold", "events"), [(0, 0), (1, 1), (5, 0)])
def test_the_threshold_is_the_operators_setting(
    tmp_path, monkeypatch, threshold, events
):
    monkeypatch.setattr(config, "NO_PROGRESS_RENEWALS", threshold)
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        counts = [_renew(client, world)["renewals_without_progress"] for _ in range(4)]
        reason = _claim_reasons(client, ADMIN, world["issue_id"])

    # The count is reported whatever the setting; 0 never raises.
    assert counts == [1, 2, 3, 4]
    assert len(_events(db_file)) == events
    assert reason is not None
    assert (VERB in reason[1].split("; ")) is (0 < threshold <= 4)


def test_an_expired_lease_is_raised_as_expired_not_as_quiet(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        for _ in range(3):
            _renew(client, world)
    conn = db.connect(db_file)
    try:
        later = fleet_work.build_active_work(
            conn, now=datetime.now(UTC) + timedelta(days=1)
        )
    finally:
        conn.close()

    (item,) = later["items"]
    assert "lease_expired" in item["attention_reasons"]
    assert VERB not in item["attention_reasons"]
    assert item["lease"]["renewals_without_progress"] == 3


def test_a_new_possession_starts_from_nothing(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        _renew(client, world)
        _renew(client, world)
        yielded = client.post(
            f"/issues/{world['issue_id']}/yield",
            json={
                "generation": world["claim"]["generation"],
                "reason": "capacity",
                "attempted_work": "nothing yet",
                "evidence": [],
                "blocking_question": "who takes it?",
                "resume_instructions": "start over",
            },
            headers=bearer(world["sol"]),
        )
        assert yielded.status_code == 201, yielded.text
        world["claim"] = _claim(client, world)
        again = _renew(client, world)["renewals_without_progress"]

    assert again == 1 and _events(db_file) == []


def test_the_issue_history_says_it_in_words(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        world = _world(client)
        for _ in range(3):
            _renew(client, world)
        login(client, "a@e.com")
        page = client.get(f"/aegis/issues/{world['issue_id']}").text

    assert "keeps renewing this lease with no recorded work" in page
    assert f">{VERB}<" not in page and f" {VERB} " not in page
