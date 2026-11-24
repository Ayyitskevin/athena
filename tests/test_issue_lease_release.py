"""An admin releases another holder's live lease from the issue page (U3).

COCKPIT-1: reassigning claimed work did not move it. The new agent was refused
``claim_held``, because the browser offered no way to release the old holder's
live lease; only REST could (read the lease, then complete it with its
generation). The release is one explicit act by an admin on one lease
(decision D12; ruling K1: nothing is swept), confirmed before it is sent, and
it goes through the SAME command REST uses, ``lease_commands.complete_claim``.

The command records an admin ending another account's lease as
``lease_released`` — naming the holder and the generation, told to the holder —
and never as ``claim_completed``: the holder finished nothing, so no reader of
completions (work progress, the project brief, the operator's "since", the
issue narrative, "you completed this") may count it.

The form carries the generation of the possession the page showed; the
command's generation fence decides. A possession replaced since the page was
drawn is refused even when its holder and second-resolution claim time match,
a renewal (same generation) still releases, a member's forged post is refused,
and a post about an issue the viewer cannot see reads as missing. Each refusal
records nothing.
"""

from __future__ import annotations

import html
import re

from fastapi.testclient import TestClient

from support import (
    ADMIN,
    add_user,
    bearer,
    bootstrap,
    events,
    lapse_lease,
    login,
    make_app,
    onboard_agent,
)

from athena.aegis import (
    issue_etags,
    issues,
    lease_commands,
    leases,
    operator_since,
    project_brief,
    work_progress,
)
from athena.core import db, users

_RELEASE_FORM = re.compile(
    r'<form method="post" action="/aegis/issues/(\d+)/lease/release"(.*?)</form>',
    re.S,
)


def _issue(client: TestClient, title: str, **fields) -> dict:
    response = client.post("/issues", json={"title": title, **fields}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def _delegate(client: TestClient, agent: dict, issue_id: int) -> None:
    response = client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=ADMIN,
    )
    assert response.status_code in (200, 201), response.text


def _try_claim(client: TestClient, agent: dict, issue_id: int, **body):
    read = client.get(f"/issues/{issue_id}", headers=bearer(agent))
    return client.post(
        f"/issues/{issue_id}/claim",
        json=body or None,
        headers={
            **bearer(agent, run=f"run-{agent['user']['id']}-{issue_id}"),
            "If-Match": read.headers["ETag"],
        },
    )


def _claim(client: TestClient, agent: dict, issue_id: int) -> dict:
    _delegate(client, agent, issue_id)
    claimed = _try_claim(client, agent, issue_id)
    assert claimed.status_code in (200, 201), claimed.text
    return claimed.json()


def _form_fields(page: str, issue_id: int) -> dict[str, str]:
    """The release form's hidden fields, as the browser would submit them."""
    match = _RELEASE_FORM.search(page)
    assert match is not None, "no release form on the page"
    assert int(match.group(1)) == issue_id
    fields = dict(
        re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)"', match[2])
    )
    return {name: html.unescape(value) for name, value in fields.items()}


def _release(client: TestClient, issue_id: int, generation: str):
    return client.post(
        f"/aegis/issues/{issue_id}/lease/release",
        data={"generation": generation},
        follow_redirects=False,
    )


def _lease_row(db_file, issue_id: int) -> dict | None:
    conn = db.connect(db_file)
    try:
        row = conn.execute(
            "SELECT holder_id, generation FROM issue_leases WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _set_claimed_at(db_file, issue_id: int, stamp: str) -> None:
    """Pin the lease's second-resolution claim time, so two possessions read
    alike on everything but their generation (the reviewer's same-second case)."""
    conn = db.connect(db_file)
    try:
        conn.execute(
            "UPDATE issue_leases SET claimed_at = ? WHERE issue_id = ?",
            (stamp, issue_id),
        )
        conn.commit()
    finally:
        conn.close()


def _lease_events(db_file) -> list[dict]:
    return events(db_file, "claim_completed", "lease_released")


def test_an_admin_releases_a_live_lease_and_the_next_agent_claims(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        admin = bootstrap(client)
        first = onboard_agent(client, email="sol@e.com", name="Sol Holder")
        second = onboard_agent(client, email="vega@e.com", name="Vega Next")
        issue = _issue(client, "reassigned while claimed")
        lease = _claim(client, first, issue["id"])
        _delegate(client, second, issue["id"])
        # The baseline the reviewer met: the new agent is refused while the old
        # holder's lease is live.
        refused = _try_claim(client, second, issue["id"])
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == lease_commands.CLAIM_HELD_CODE

        login(client, "a@e.com")
        page = client.get(f"/aegis/issues/{issue['id']}").text
        form = _RELEASE_FORM.search(page)
        assert form is not None
        # The destructive act asks first, naming whose lease it ends, that it is
        # a release and not completed work, and that the holder may reclaim.
        confirm = re.search(r'data-confirm="([^"]*)"', form[2])
        assert confirm is not None
        text = html.unescape(confirm[1])
        assert "Sol Holder" in text and "not as completed work" in text
        assert "may claim it again" in text
        assert 'class="button danger small"' in form[2]
        fields = _form_fields(page, issue["id"])
        # The form names the possession shown: its generation, nothing else.
        assert fields == {
            "csrf_token": fields["csrf_token"],
            "generation": lease["generation"],
        }

        released = client.post(
            f"/aegis/issues/{issue['id']}/lease/release",
            data=fields,
            follow_redirects=False,
        )
        assert released.status_code == 303, released.text
        assert released.headers["location"].startswith(f"/aegis/issues/{issue['id']}")
        after = client.get(released.headers["location"]).text
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)

        # The lease is gone and the next agent takes it.
        assert _lease_row(db_file, issue["id"]) is None
        taken = _try_claim(client, second, issue["id"])
        assert taken.status_code in (200, 201), taken.text
        assert taken.json()["holder_name"] == "Vega Next"

    assert "Released Sol Holder" in after
    # The History reads a release, never a completion.
    assert "released another account's lease" in after
    assert "completed the claim" not in after
    # One release, by the admin, naming the holder and the generation; no
    # completion is recorded for work the holder did not finish.
    recorded = _lease_events(db_file)
    assert [row["verb"] for row in recorded] == ["lease_released"]
    assert recorded[0]["actor_id"] == admin["id"]
    assert recorded[0]["target_id"] == issue["id"]
    assert recorded[0]["detail"] == (
        f"generation {lease['generation']}; holder_id {first['user']['id']}; "
        "holder Sol Holder"
    )
    conn = db.connect(db_file)
    try:
        facts = conn.execute(
            "SELECT generation FROM activity_event_facts WHERE event_id = ?",
            (recorded[0]["id"],),
        ).fetchone()
        told = conn.execute(
            "SELECT user_id FROM notifications WHERE event_id = ?",
            (recorded[0]["id"],),
        ).fetchall()
    finally:
        conn.close()
    assert facts["generation"] == lease["generation"]
    # The holder is told who ended its possession.
    assert first["user"]["id"] in {row["user_id"] for row in told}


def test_a_release_is_not_read_as_a_completion(tmp_path):
    """The four readers of completions the reviewer named, plus the inbox's
    "you completed this", see the admin's release as no completion at all."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        project = client.post(
            "/projects", json={"name": "Ops", "key": "OPS"}, headers=ADMIN
        ).json()
        issue = _issue(client, "abandoned work", project_id=project["id"])
        lease = _claim(client, agent, issue["id"])
        login(client, "a@e.com")
        watched = client.post(
            f"/aegis/issues/{issue['id']}/watch", follow_redirects=False
        )
        assert watched.status_code == 303
        assert _release(client, issue["id"], lease["generation"]).status_code == 303
        history = client.get(f"/aegis/issues/{issue['id']}/history").text
        inbox = client.get("/inbox").text

    conn = db.connect(db_file)
    try:
        admin = users.get_user(conn, 1)
        last_work = work_progress.last_work_at(conn, [(issue["id"], None)])
        since = operator_since.build_since(conn, actor=admin)
        brief = project_brief.build_project_brief(
            conn, project_id=project["id"], actor=admin
        )
    finally:
        conn.close()
    assert last_work[(issue["id"], None)] is None  # no work was recorded
    completions = next(k for k in since["kinds"] if k["key"] == "completions")
    assert completions["count"] == 0 and since["completions"] == []
    assert brief is not None and brief["latest_completions"]["items"] == []
    assert (
        "released another account" in history and "completed the claim" not in history
    )
    assert "you completed this" not in inbox


def test_a_member_forged_post_is_refused_and_records_nothing(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com", name="Mo Member")
        agent = onboard_agent(client, name="Sol Holder")
        # The member is the assignee: they can modify the issue and read the
        # lease, but releasing another holder's lease is an admin's act.
        issue = _issue(client, "the member's own issue")
        assigned = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": member["id"]},
            headers=ADMIN,
        )
        assert assigned.status_code == 200, assigned.text
        lease = _claim(client, agent, issue["id"])
        # The generation is not a secret: the member reads it over REST.
        read = client.get(
            f"/issues/{issue['id']}/lease",
            headers={"X-Athena-Actor": str(member["id"])},
        )
        assert read.json()["generation"] == lease["generation"]

        login(client, "m@e.com")
        page = client.get(f"/aegis/issues/{issue['id']}").text
        assert "Sol Holder" in page  # the member does see who holds it
        assert "/lease/release" not in page
        forged = _release(client, issue["id"], lease["generation"])

    assert forged.status_code == 403, forged.text
    assert "Only an admin may release another holder" in forged.text
    assert _lease_row(db_file, issue["id"]) == {
        "holder_id": agent["user"]["id"],
        "generation": lease["generation"],
    }
    assert _lease_events(db_file) == []


def test_a_new_possession_in_the_same_second_is_refused(tmp_path):
    """The holder settles and reclaims after the page was drawn, and the new
    lease carries the same holder and the same second-resolution claim time.
    Only the generation tells them apart; the release is refused."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "reclaimed within the second")
        first = _claim(client, agent, issue["id"])
        login(client, "a@e.com")
        fields = _form_fields(
            client.get(f"/aegis/issues/{issue['id']}").text, issue["id"]
        )
        done = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": first["generation"]},
            headers=bearer(agent),
        )
        assert done.status_code == 200, done.text
        second = _try_claim(client, agent, issue["id"]).json()
        _set_claimed_at(db_file, issue["id"], first["claimed_at"])
        stale = client.post(
            f"/aegis/issues/{issue['id']}/lease/release",
            data=fields,
            follow_redirects=False,
        )

    assert second["generation"] != first["generation"]
    assert stale.status_code == 409, stale.text
    assert "changed since this page was loaded" in stale.text
    assert _lease_row(db_file, issue["id"]) == {
        "holder_id": agent["user"]["id"],
        "generation": second["generation"],
    }
    # Only the holder's own completion; no release was written.
    assert [(r["verb"], r["actor_id"]) for r in _lease_events(db_file)] == [
        ("claim_completed", agent["user"]["id"])
    ]


def test_a_renewal_between_render_and_post_still_releases(tmp_path):
    """Renewing keeps the possession (same generation) and moves its claim
    time; the lease the admin confirmed is still the one held."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "renewed under the operator's eyes")
        lease = _claim(client, agent, issue["id"])
        login(client, "a@e.com")
        fields = _form_fields(
            client.get(f"/aegis/issues/{issue['id']}").text, issue["id"]
        )
        renewed = _try_claim(client, agent, issue["id"], generation=lease["generation"])
        assert renewed.status_code in (200, 201), renewed.text
        _set_claimed_at(db_file, issue["id"], "2099-01-01 00:00:00")
        released = client.post(
            f"/aegis/issues/{issue['id']}/lease/release",
            data=fields,
            follow_redirects=False,
        )

    assert renewed.json()["generation"] == lease["generation"]
    assert released.status_code == 303, released.text
    assert _lease_row(db_file, issue["id"]) is None
    assert [r["verb"] for r in _lease_events(db_file)] == ["lease_released"]


def test_a_lease_that_changed_inside_the_request_is_refused(tmp_path, monkeypatch):
    """A possession replaced between the request's arrival and the command's
    write lock is refused by the command's fence, not ended."""
    app, db_file = make_app(tmp_path)
    original = lease_commands.complete_claim
    retaken: dict = {}

    def lease_moves_first(conn, **kwargs):
        issue_id = kwargs["issue_id"]
        held = leases.get_lease(conn, issue_id)
        holder = users.get_user(conn, held["holder_id"])
        original(conn, actor=holder, issue_id=issue_id, generation=held["generation"])
        etag = issue_etags.current_etag(conn, issues.get_issue(conn, issue_id))
        retaken.update(
            lease_commands.claim_issue(
                conn, actor=holder, issue_id=issue_id, if_match=[etag]
            )
        )
        return original(conn, **kwargs)

    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "raced inside one request")
        original_generation = _claim(client, agent, issue["id"])["generation"]
        login(client, "a@e.com")
        fields = _form_fields(
            client.get(f"/aegis/issues/{issue['id']}").text, issue["id"]
        )
        monkeypatch.setattr(lease_commands, "complete_claim", lease_moves_first)
        raced = client.post(
            f"/aegis/issues/{issue['id']}/lease/release",
            data=fields,
            follow_redirects=False,
        )

    assert raced.status_code == 409, raced.text
    assert "changed since this page was loaded" in raced.text
    assert retaken["generation"] != original_generation
    assert _lease_row(db_file, issue["id"]) == {
        "holder_id": agent["user"]["id"],
        "generation": retaken["generation"],
    }
    assert [(r["verb"], r["actor_id"]) for r in _lease_events(db_file)] == [
        ("claim_completed", agent["user"]["id"])
    ]


def test_a_lease_on_a_hidden_issue_is_not_reachable(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        add_user(client, "out@e.com", name="Outsider")
        project = client.post(
            "/projects", json={"name": "Private", "key": "PVT"}, headers=ADMIN
        )
        assert project.status_code == 201, project.text
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "private work", project_id=project.json()["id"])
        lease = _claim(client, agent, issue["id"])
        hidden = client.put(
            f"/projects/{project.json()['id']}/visibility",
            json={"visibility": "private"},
            headers=ADMIN,
        )
        assert hidden.status_code == 200, hidden.text
        login(client, "out@e.com")
        probe = _release(client, issue["id"], lease["generation"])

    # Missing, not forbidden: the probe learns nothing about the issue or holder.
    assert probe.status_code == 404, probe.text
    # The issue routes' own "missing" answer, from the visibility gate.
    assert "Issue not found." in probe.text
    assert "Sol Holder" not in probe.text
    assert _lease_row(db_file, issue["id"]) == {
        "holder_id": agent["user"]["id"],
        "generation": lease["generation"],
    }
    assert _lease_events(db_file) == []


def test_a_release_carries_no_completion_report(tmp_path):
    """An admin may not write a completion report for work another account
    held: over REST the release with a report is refused, and nothing changes."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        issue = _issue(client, "an admin may not report for the holder")
        lease = _claim(client, agent, issue["id"])
        refused = client.post(
            f"/issues/{issue['id']}/complete",
            json={"generation": lease["generation"], "verdict": "PASS"},
            headers=ADMIN,
        )

    assert refused.status_code == 422, refused.text
    assert _lease_row(db_file, issue["id"]) is not None
    assert _lease_events(db_file) == []


def test_no_release_control_without_a_live_lease(tmp_path):
    """A lapsed row is nobody's to complete but its holder's (complete_claim), so
    the page offers no release for it; an unclaimed issue offers none either."""
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, name="Sol Holder")
        free = _issue(client, "nobody holds this")
        lapsed = _issue(client, "the clock let go")
        _claim(client, agent, lapsed["id"])
        lapse_lease(db_file, lapsed["id"])
        login(client, "a@e.com")
        free_page = client.get(f"/aegis/issues/{free['id']}").text
        lapsed_page = client.get(f"/aegis/issues/{lapsed['id']}").text

    assert "/lease/release" not in free_page
    assert "lapsed lease" in lapsed_page
    assert "/lease/release" not in lapsed_page


def test_a_reflected_message_is_capped_and_wraps(tmp_path):
    """The page shows the notice its own forms return; a crafted link gets at
    most a few hundred characters, escaped, in a banner that wraps."""
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        issue = _issue(client, "a long notice")
        login(client, "a@e.com")
        page = client.get(
            f"/aegis/issues/{issue['id']}",
            params={"notice": "A" * 5000, "error": "<b>x</b>"},
        ).text
        styles = client.get("/static/styles.css").text

    assert "A" * 300 in page and "A" * 301 not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    start = styles.index(".alert {")
    assert "overflow-wrap: anywhere" in styles[start : styles.index("}", start)]
