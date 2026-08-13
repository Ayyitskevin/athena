"""The demo seeds supervision state, so the five-minute tour shows the differentiator.

Athena's claim is that agents are first-class actors an operator can watch, bound, and
interrupt. Before this, `athena-demo` seeded a tracker and a wiki and nothing else —
so every Intervene and Trust surface on the tour rendered its empty state and the
product read as a small Notion. These tests hold the seed to two things: the
supervision surfaces are genuinely populated, and every seeded state is TRUE (demo.py's
standing rule is that a demo which oversells is worse than one that is thin).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.aegis import fleet_attention, leases
from athena.core import activity, approvals, budgets, db, run_controls, workers
from athena.demo import (
    DEMO_EMAIL,
    DEMO_PASSWORD,
    DEMO_RUN_ID,
    DEMO_WORKER_KEY,
    DEMO_WORKER_NODE,
    seed_demo,
)
from athena.main import create_app


def _seeded(tmp_path):
    seeded = seed_demo(tmp_path / "demo.db")
    return seeded, db.connect(tmp_path / "demo.db")


def test_fleet_attention_card_has_non_zero_counts_naming_their_surfaces(tmp_path):
    """The dashboard card is the tour's first stop. Every count it shows must point at
    a surface that really has something on it."""
    _seeded_info, conn = _seeded(tmp_path)
    try:
        signals = {
            s["key"]: s for s in fleet_attention.build_attention(conn)["signals"]
        }
        assert signals["pending_approvals"]["count"] == 1
        assert signals["open_run_controls"]["count"] == 1
        # A claim whose run has an unanswered control is exactly what "needs
        # attention" means; the card and Mission Control run the same query.
        assert signals["claims_needing_attention"]["count"] == 1
        for key in (
            "pending_approvals",
            "open_run_controls",
            "claims_needing_attention",
        ):
            assert signals[key]["href"].startswith("/")
        # Nothing is inflated: the signals we did NOT seed stay honestly at zero.
        assert signals["failing_automation_rules"]["count"] == 0
        assert signals["failing_webhooks"]["count"] == 0
        assert signals["unanswered_kills"]["count"] == 0
    finally:
        conn.close()


def test_the_pending_approval_was_earned_by_a_real_refusal(tmp_path):
    """Not an inserted row: the operator gated Sol on issue.close, Sol attempted the
    close, and the command refused. The issue must therefore still be open."""
    seeded, conn = _seeded(tmp_path)
    try:
        pending = approvals.list_requests(conn, state="pending", limit=10)
        assert len(pending) == 1
        ask = pending[0]
        assert ask.action_kind == approvals.ACTION_ISSUE_CLOSE
        assert ask.requested_by == seeded["ids"]["sol"]
        assert ask.target_id == seeded["ids"]["command_issue"]
        assert ask.run_id == DEMO_RUN_ID

        # The gate is real, so the close did NOT happen.
        status = conn.execute(
            "SELECT status FROM issues WHERE id = ?",
            (seeded["ids"]["command_issue"],),
        ).fetchone()["status"]
        assert status != "done"
        # And the policy that produced the refusal is on the trail with the ask.
        verbs = {e["verb"] for e in activity.list_activity(conn, limit=200)}
        assert approvals.VERB_POLICY_SET in verbs
        assert approvals.VERB_REQUESTED in verbs
    finally:
        conn.close()


def test_the_run_control_is_open_and_unanswered(tmp_path):
    """Athena records the ask and the reply; it cannot signal a process. An unanswered
    control is the honest shape of intervention, and the tour should show one."""
    _seeded_info, conn = _seeded(tmp_path)
    try:
        assert run_controls.count_open(conn, now_stamp=run_controls.stamp(None)) == 1
    finally:
        conn.close()


def test_the_claim_lease_and_check_in_are_live(tmp_path):
    seeded, conn = _seeded(tmp_path)
    try:
        lease = leases.get_lease(conn, seeded["ids"]["command_issue"])
        assert lease is not None
        assert lease["active"] is True
        assert lease["holder_id"] == seeded["ids"]["sol"]
        assert seeded["supervision"]["checkin_run_id"] == DEMO_RUN_ID
    finally:
        conn.close()


def test_the_worker_registry_and_budget_are_populated(tmp_path):
    seeded, conn = _seeded(tmp_path)
    try:
        registry = workers.list_workers(conn, limit=workers.MAX_LIST_LIMIT)
        assert len(registry) == 1
        assert registry[0]["worker_key"] == DEMO_WORKER_KEY
        # Seeded through the real command, which demands a bearer credential — so a
        # registry row here proves the token path, not a hand-built actor dict.
        assert registry[0]["kill_state"] not in (
            workers.KILL_REQUESTED,
            workers.KILL_DEFIED,
        )

        budget = budgets.observed(conn, seeded["ids"]["sol"])
        assert budget is not None
        assert budget.action_limit == seeded["supervision"]["budget_action_limit"]
        # A ceiling nobody is near teaches nothing; the tour should show real usage
        # against it, and the seeded writes are what put it there.
        assert budget.action_used > 0
        assert budget.remaining > 0
    finally:
        conn.close()


def test_the_tour_surfaces_render_the_seeded_rows(tmp_path):
    """End to end over HTTP: the two surfaces the README tour tells a reviewer to
    answer each show their live row."""
    seeded, conn = _seeded(tmp_path)
    conn.close()
    app = create_app(seeded["db_path"])
    with TestClient(app) as client:
        # Sign in the way the tour tells a reviewer to, with the credentials the CLI
        # prints. The admin cockpit is session-gated, so this is also the proof that
        # the demo's printed login actually reaches the seeded supervision state.
        signin = client.post(
            "/login",
            data={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            follow_redirects=False,
        )
        assert signin.status_code in (302, 303)

        controls = client.get("/admin/run-controls")
        assert controls.status_code == 200
        assert "Narrow the scope" in controls.text
        assert DEMO_RUN_ID in controls.text

        agents = client.get("/admin/agents")
        assert agents.status_code == 200
        assert approvals.ACTION_ISSUE_CLOSE in agents.text
        # The registry renders a worker by its node label when it has one.
        assert DEMO_WORKER_NODE in agents.text

        dashboard = client.get("/aegis/dashboard")
        assert dashboard.status_code == 200
        assert "Approvals waiting on you" in dashboard.text
        assert "Run controls awaiting an agent" in dashboard.text
