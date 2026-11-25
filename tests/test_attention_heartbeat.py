"""U7: the attention queue surfaces decisions, not check-in noise.

The reviewer (COCKPIT-7, COCKPIT-9) ran about 55 live leases and found 54 of 54
fresh claims reading "needs attention": a claim with no heartbeat was
``high`` the instant it was taken. Twelve of the twenty "Now" rows were missed
check-ins, and the one pending approval sat at row five, under them. Rows were
labelled ``checkin_missing`` and ``issue #12``. A question the operator had
answered stayed ``critical`` and "waiting on you" until the agent came back.

What these tests hold:

- A tagged claim is not attention until its heartbeat is overdue: more than the
  stale threshold (``ATHENA_AGENT_RUN_STALE_SECONDS``) since the possession was
  acquired or since the run's last check-in, whichever is later. A renewal is
  not an acquisition, so renewing never silences a run that never reports. An
  untagged claim cannot report at all and is attention at once.
- A pending decision outranks a missed check-in: a claim whose only reasons are
  reporting reasons ranks ``medium``, under every pending approval (``high``).
- Rows in the cockpit use words, the issue key and its title.
- A ``needs_input`` question that a person other than the asker has answered
  since it was asked reads "answered, waiting on the agent". Another agent's
  act answers nothing for the operator's queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

from fastapi.testclient import TestClient

from athena import config
from athena.aegis import fleet_attention, fleet_work, run_state
from athena.core import db, users
from support import (
    ADMIN,
    add_user,
    as_user,
    bearer,
    bootstrap,
    lapse_lease,
    login,
    make_app,
    onboard_agent,
)

STALE = config.AGENT_RUN_STALE_SECONDS
_TS = "%Y-%m-%d %H:%M:%S"
REPORTING_CODES = ("checkin_missing", "checkin_stale", "run_untagged")


def _project(client) -> int:
    made = client.post(
        "/projects", json={"key": "OPS", "name": "Operations"}, headers=ADMIN
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _issue_for(client, agent, *, title, project_id=None) -> dict:
    body: dict = {"title": title}
    if project_id is not None:
        body["project_id"] = project_id
    issue = client.post("/issues", json=body, headers=ADMIN).json()
    client.post(
        f"/issues/{issue['id']}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    return issue


def _take(client, agent, issue, run_id, *, generation=None) -> dict:
    read = client.get(f"/issues/{issue['id']}", headers=bearer(agent))
    claimed = client.post(
        f"/issues/{issue['id']}/claim",
        json={} if generation is None else {"generation": generation},
        headers={**bearer(agent, run=run_id), "If-Match": read.headers["ETag"]},
    )
    assert claimed.status_code in (200, 201), claimed.text
    return claimed.json()


def _claim(client, agent, *, title, project_id=None, run: str | None = "auto"):
    issue = _issue_for(client, agent, title=title, project_id=project_id)
    run_id = f"run-{issue['id']}" if run == "auto" else run
    return issue, _take(client, agent, issue, run_id), run_id


def _beat(client, agent, run_id):
    beat = client.put(
        "/agent-runs/heartbeat", json={"run_id": run_id}, headers=bearer(agent)
    )
    assert beat.status_code == 200, beat.text


def _sql(db_file: Path, statement: str, params=()) -> None:
    conn = db.connect(db_file)
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _acquired_long_ago(db_file: Path, issue_id: int | None = None) -> None:
    """The clock passes: the claim (its `claimed` event and the lease row) was
    taken an hour ago. The app has no injectable clock, and a test never sleeps.

    One timestamp for both rows. Each statement used to read the clock itself,
    and when the two fell either side of a second the lease was a second newer
    than its `claimed` event: the claim then had no event at or after it, read
    as untagged, and the test waiting for a missed check-in failed (seen once
    in a full run)."""
    conn = db.connect(db_file)
    try:
        (then,) = conn.execute("SELECT datetime('now', '-1 hour')").fetchone()
    finally:
        conn.close()
    where = "" if issue_id is None else f" AND target_id = {int(issue_id)}"
    _sql(
        db_file,
        "UPDATE activity SET created_at = ? "
        f"WHERE verb = 'claimed' AND target_kind = 'issue'{where}",
        (then,),
    )
    lease_where = "" if issue_id is None else f" WHERE issue_id = {int(issue_id)}"
    _sql(db_file, f"UPDATE issue_leases SET claimed_at = ?{lease_where}", (then,))


def _acquired_at(db_file: Path, issue_id: int) -> datetime:
    conn = db.connect(db_file)
    try:
        stamp = conn.execute(
            "SELECT created_at FROM activity WHERE verb = 'claimed' "
            "AND target_kind = 'issue' AND target_id = ? ORDER BY id DESC LIMIT 1",
            (issue_id,),
        ).fetchone()["created_at"]
    finally:
        conn.close()
    return datetime.strptime(stamp, _TS).replace(tzinfo=UTC)


def _row(db_file: Path, issue_id: int, now: datetime | None = None) -> dict:
    conn = db.connect(db_file)
    try:
        work = fleet_work.build_active_work(conn, now=now)
    finally:
        conn.close()
    (row,) = [item for item in work["items"] if item["issue"]["id"] == issue_id]
    return row


def _ranking(db_file: Path, now: datetime | None = None, **kwargs) -> list[dict]:
    conn = db.connect(db_file)
    try:
        admin = users.get_user(conn, 1)
        assert admin is not None
        ranking = fleet_attention.build_attention_ranking(
            conn, actor=admin, now=now, limit=100, **kwargs
        )
        return fleet_attention.public_attention_ranking(conn, ranking, actor=admin)[
            "items"
        ]
    finally:
        conn.close()


# --- when a claim becomes attention -------------------------------------------


def test_a_fresh_claim_is_not_attention_until_its_heartbeat_is_overdue(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue, _, _ = _claim(client, agent, title="Fresh work")
        acquired = _acquired_at(db_file, issue["id"])

        fresh = _row(db_file, issue["id"], acquired + timedelta(seconds=STALE))
        assert fresh["run"]["reporting_state"] == "not_reported"  # the fact stays
        assert fresh["attention_state"] == "observed", fresh["attention_reasons"]
        assert not set(REPORTING_CODES) & set(fresh["attention_reasons"])

        overdue = _row(db_file, issue["id"], acquired + timedelta(seconds=STALE + 1))
        assert overdue["attention_state"] == "needs_attention"
        assert "checkin_missing" in overdue["attention_reasons"]


def test_renewing_never_resets_the_grace(tmp_path):
    """A holder that renews every minute and never checks in is attention once
    the grace since its acquisition has passed, however recently it renewed."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue, lease, run_id = _claim(client, agent, title="Renews, never reports")
        _acquired_long_ago(db_file, issue["id"])
        _take(client, agent, issue, run_id, generation=lease["generation"])

        row = _row(db_file, issue["id"])
        assert row["lease"]["claimed_at"] > _acquired_at(db_file, issue["id"]).strftime(
            _TS
        ), "the fixture needs the renewal to have moved claimed_at"
        assert row["attention_state"] == "needs_attention", row["attention_reasons"]
        assert "checkin_missing" in row["attention_reasons"]


def test_a_new_possession_restarts_the_grace(tmp_path):
    """A re-claim after the lease lapsed is a new acquisition: its grace starts
    again, even under a run whose last check-in is an hour old."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue, _, run_id = _claim(client, agent, title="Taken again")
        _beat(client, agent, run_id)
        _sql(
            db_file,
            "UPDATE agent_run_checkins SET first_seen_at = datetime('now', '-1 hour'), "
            "last_seen_at = datetime('now', '-1 hour')",
        )
        _acquired_long_ago(db_file, issue["id"])
        assert "checkin_stale" in _row(db_file, issue["id"])["attention_reasons"]

        lapse_lease(db_file, issue["id"])
        _take(client, agent, issue, run_id)
        again = _row(db_file, issue["id"], _acquired_at(db_file, issue["id"]))
        assert again["run"]["reporting_state"] == "stale"  # the check-in's own age
        assert again["attention_state"] == "observed", again["attention_reasons"]


def test_overdue_is_measured_from_the_later_of_acquisition_and_check_in(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue, _, run_id = _claim(client, agent, title="Checked in")
        _acquired_long_ago(db_file, issue["id"])
        _beat(client, agent, run_id)
        conn = db.connect(db_file)
        seen = conn.execute(
            "SELECT last_seen_at FROM agent_run_checkins WHERE run_id = ?", (run_id,)
        ).fetchone()["last_seen_at"]
        conn.close()
        last_seen = datetime.strptime(seen, _TS).replace(tzinfo=UTC)

        checked_in = _row(db_file, issue["id"], last_seen + timedelta(seconds=30))
        assert checked_in["attention_state"] == "observed"
        stale = _row(db_file, issue["id"], last_seen + timedelta(seconds=STALE + 1))
        assert "checkin_stale" in stale["attention_reasons"]


def test_an_untagged_claim_is_attention_at_once(tmp_path):
    """No run id, so no check-in can ever arrive: a configuration fact, not a
    late heartbeat. It is raised the moment the claim is taken, and ranks with
    the other reporting reasons, below every decision."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        issue, _, _ = _claim(client, agent, title="Untagged", run=None)
        row = _row(db_file, issue["id"], _acquired_at(db_file, issue["id"]))
        assert row["run"]["reporting_state"] == "untagged"
        assert row["attention_reasons"] == ["run_untagged"]
        (item,) = [i for i in _ranking(db_file) if i["source_id"] == issue["id"]]
        assert item["signal"] == "claim_needs_attention"
        assert item["severity"] == "medium"


def test_fresh_tagged_claims_do_not_reach_the_rollup_or_the_ranking(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        for n in range(5):
            _claim(client, agent, title=f"Fresh {n}")
        ranking = client.get("/attention/ranking", headers=ADMIN).json()
        claims = [r for r in ranking["items"] if r["signal"] == "claim_needs_attention"]
        assert claims == []
        conn = db.connect(db_file)
        try:
            card = fleet_attention.build_attention(conn)
        finally:
            conn.close()
        (count,) = [
            s for s in card["signals"] if s["key"] == "claims_needing_attention"
        ]
        assert count["count"] == 0


# --- the ranking ----------------------------------------------------------------


def _pending_approval(client, *, email="nyx@e.com", name="Nyx", title="Gated close"):
    gatekept = onboard_agent(client, email=email, name=name)
    client.put(
        f"/approvals/policies/{gatekept['user']['id']}",
        json={"action_kind": "issue.close"},
        headers=ADMIN,
    )
    gated = client.post("/issues", json={"title": title}, headers=bearer(gatekept))
    gated = gated.json()
    asked = client.patch(
        f"/issues/{gated['id']}", json={"status": "done"}, headers=bearer(gatekept)
    )
    assert asked.status_code == 202, asked.text
    return gated


def test_a_pending_approval_outranks_every_missed_check_in(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        for n in range(12):
            _claim(client, agent, title=f"Silent {n}")
        lapsed, _, _ = _claim(client, agent, title="Lapsed")
        _sql(
            db_file,
            "UPDATE issue_leases SET expires_at = datetime('now', '-1 second') "
            "WHERE issue_id = ?",
            (lapsed["id"],),
        )
        _pending_approval(client)

        # Every claim is long past its grace; the approval is the newest row.
        later = datetime.now(UTC) + timedelta(seconds=STALE * 10)
        items = _ranking(db_file, now=later)
        signals = [item["signal"] for item in items]
        approval = signals.index("pending_approval")
        missed = [
            n
            for n, item in enumerate(items)
            if item["signal"] == "claim_needs_attention"
            and set(item["reason"].split("; ")) <= set(REPORTING_CODES)
        ]
        assert len(missed) == 12
        assert approval < min(missed), signals
        assert {items[n]["severity"] for n in missed} == {"medium"}
        # Codes stay stable for API callers; only rank and timing moved.
        assert {items[n]["reason"] for n in missed} == {"checkin_missing"}
        # The grace is for heartbeats only: a lapsed lease (C4) is attention at
        # once and keeps its weight.
        (lapse,) = [item for item in items if item["source_id"] == lapsed["id"]]
        assert lapse["severity"] == "high"
        assert "lease_expired" in lapse["reason"]


# --- answered questions -----------------------------------------------------------


def _ask(client, agent, title, *, reason="needs_input", project_id=None):
    issue, lease, run_id = _claim(client, agent, title=title, project_id=project_id)
    yielded = client.post(
        f"/issues/{issue['id']}/yield",
        json={
            "generation": lease["generation"],
            "reason": reason,
            "attempted_work": "Read the code.",
            "evidence": ["notes"],
            "blocking_question": f"Which path for {title}?",
            "resume_instructions": "Pick a path.",
        },
        headers=bearer(agent, run=run_id),
    )
    assert yielded.status_code == 201, yielded.text
    return issue


def test_only_a_person_answers_a_question_for_the_operator(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        add_user(client, "m@e.com", name="Mel")  # id 2, a person
        agent = onboard_agent(client)
        other = onboard_agent(client, email="nyx@e.com", name="Nyx")

        own = _ask(client, agent, "Own comment")
        client.post(
            f"/issues/{own['id']}/comments",
            json={"body": "still waiting"},
            headers=bearer(agent),
        )
        plus_one = _ask(client, agent, "Another agent")
        client.post(
            f"/issues/{plus_one['id']}/comments",
            json={"body": "+1, no idea either"},
            headers=bearer(other),
        )
        taken = _ask(client, agent, "Another agent claims")
        client.post(
            f"/issues/{taken['id']}/delegate",
            json={"user_id": other["user"]["id"]},
            headers=ADMIN,
        )
        _take(client, other, taken, "nyx-run")
        back = _ask(client, agent, "Asked back")
        client.post(
            f"/issues/{back['id']}/comments",
            json={"body": "What do you mean by path?"},
            headers=as_user(2),
        )
        client.post(
            f"/issues/{back['id']}/comments",
            json={"body": "A or B? Still blocked."},
            headers=bearer(agent),
        )
        blocked = _ask(client, agent, "Blocked yield", reason="blocked")
        client.post(
            f"/issues/{blocked['id']}/comments", json={"body": "noted"}, headers=ADMIN
        )
        answered = _ask(client, agent, "Answered")
        made = client.post(
            f"/issues/{answered['id']}/comments",
            json={"body": "Path A."},
            headers=ADMIN,
        )
        assert made.status_code == 201, made.text

        by_issue = {
            item["source_id"]: item
            for item in _ranking(db_file, signals={"awaiting_input"})
        }
        for still_waiting in (own, plus_one, taken, back, blocked):
            item = by_issue[still_waiting["id"]]
            assert item["severity"] == "critical", still_waiting["title"]
            assert not item["reason"].startswith("answered"), still_waiting["title"]
            assert item["next_action"] == "operator-link", still_waiting["title"]
        done = by_issue[answered["id"]]
        assert done["signal"] == "awaiting_input"  # the code stays stable
        assert done["severity"] == "low"
        assert done["reason"].startswith("answered, waiting on the agent")
        assert done["next_action"] == "none"
        assert list(by_issue)[-1] == answered["id"], "an answered question ranks last"

        conn = db.connect(db_file)
        try:
            admin = users.get_user(conn, 1)
            lane = run_state.open_questions(conn, actor=admin, with_answers=True)
        finally:
            conn.close()
        answers = {q["issue"]["id"]: q["answered"] for q in lane["items"]}
        assert answers == {
            own["id"]: False,
            plus_one["id"]: False,
            taken["id"]: False,
            back["id"]: False,
            blocked["id"]: False,
            answered["id"]: True,
        }
        assert lane["answered_total"] == 1

        login(client, "a@e.com")
        page = client.get("/admin/agents", headers={"Accept": "text/html"}).text
        questions = page.split('id="questions"', 1)[1].split("</section>", 1)[0]
        assert "answered, waiting on the agent" in questions
        # The heading counts only what still waits on the operator.
        assert re.search(
            r'Questions waiting on you\s*<span class="chip" data-tone="accent">5<',
            questions,
        )


# --- the rendered rows --------------------------------------------------------------


def test_now_queue_rows_carry_words_the_issue_key_and_title(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client)
        project_id = _project(client)
        silent, _, _ = _claim(
            client, agent, title="Backfill status categories", project_id=project_id
        )
        untagged, _, _ = _claim(
            client, agent, title="Tighten palette CSRF", project_id=project_id, run=None
        )
        _acquired_long_ago(db_file)
        asked = _ask(client, agent, "Retention window")
        gatekept = onboard_agent(client, email="nyx@e.com", name="Nyx")
        client.put(
            f"/approvals/policies/{gatekept['user']['id']}",
            json={"action_kind": "issue.close"},
            headers=ADMIN,
        )
        gated = _issue_for(
            client, gatekept, title="Rotate the webhook secret", project_id=project_id
        )
        etag = client.get(f"/issues/{gated['id']}", headers=bearer(gatekept))
        closing = client.patch(
            f"/issues/{gated['id']}",
            json={"status": "done"},
            headers={**bearer(gatekept), "If-Match": etag.headers["ETag"]},
        )
        assert closing.status_code == 202, closing.text

        login(client, "a@e.com")
        page = client.get("/aegis/dashboard", headers={"Accept": "text/html"}).text
        queue = page.split("<h4>Now queue</h4>", 1)[1].split("</ol>", 1)[0]
        assert "OPS-1" in queue and "Backfill status categories" in queue
        assert "OPS-2" in queue and "Tighten palette CSRF" in queue
        assert "Retention window" in queue
        assert "missed its check-in" in queue
        assert "never tagged with a run" in queue
        for code in (*REPORTING_CODES, "claim_needs_attention", "awaiting_input"):
            assert code not in queue, code
        assert not re.search(r"issue #\d+", queue)
        assert f"/aegis/issues/{silent['id']}" in queue
        assert f"/aegis/issues/{untagged['id']}" in queue
        assert f"/aegis/issues/{asked['id']}" in queue
        # A claim row still links its issue's history, as before.
        assert f"/aegis/issues/{silent['id']}/history" in queue

        # The approval row names what it would act on (COCKPIT-7's own example).
        (approval,) = [
            li for li in queue.split("<li>")[1:] if "<strong>Approval</strong>" in li
        ]
        assert "OPS-3" in approval and "Rotate the webhook secret" in approval
        assert "Nyx asks to close the issue" in approval

        # So does the "oldest request waiting on you" lane.
        lane = page.split("Oldest request waiting on you", 1)[1].split("</li>", 1)[0]
        assert "OPS-3" in lane and "Rotate the webhook secret" in lane
        assert "issue.close on issue #" not in lane
