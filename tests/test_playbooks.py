"""Stage F-2 — Playbooks: a checklist page becomes real work.

This is the stage that closes the loop between the two modules. Embeds already
let docs SHOW work; run learnings already let work WRITE BACK to docs; a
playbook lets docs START work. The first test proves that loop end to end —
page → issues → backlinks on the page → a rollup embed counting them — because
the claim "Mentor and Aegis are one product" is exactly what would rot first if
nobody pinned it.

The discipline the rest of the suite guards: a template is not a live mirror
(instantiation snapshots the page), ticked steps are the author's statement and
never become work, and every write goes through `issue_commands` so a playbook
cannot become a second way to create an issue.
"""

import pytest
from fastapi.testclient import TestClient

from athena.core import db
from athena.main import create_app
from athena.mcp.client import AthenaClient, AthenaError
from athena.workflows import playbook_commands

H1 = {"X-Athena-Actor": "1"}
PASSWORD = "pw-long-enough"

CHECKLIST = """Before you deploy, work through these.

- [ ] Freeze the release branch
- [ ] Run the migration rehearsal
- [x] Tell the operator (already done)
- [ ] Verify the chain after restore

Notes: the box style above is the only one that counts.
"""


def _app(tmp_path, name="playbooks.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post(
        "/users", json={"email": "a@e.com", "name": "Ann", "password": PASSWORD}
    )


def _space(client, key="OPS", name="Operations"):
    return client.post("/spaces", json={"key": key, "name": name}, headers=H1).json()


def _page(client, space_id, title="Deploy", body=CHECKLIST, playbook=True):
    page = client.post(
        f"/spaces/{space_id}/pages",
        json={"title": title, "body": body},
        headers=H1,
    ).json()
    if playbook:
        label = client.post(
            "/labels", json={"name": playbook_commands.PLAYBOOK_LABEL}, headers=H1
        ).json()
        attached = client.post(
            f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=H1
        )
        assert attached.status_code in (200, 201), attached.text
    return page


def _start(client, page_id, headers=H1, **body):
    return client.post(
        f"/pages/{page_id}/start-playbook", json=body or {}, headers=headers
    )


# --- the loop ----------------------------------------------------------------


def test_starting_a_playbook_closes_the_docs_to_work_loop(tmp_path):
    # WHY: the flagship claim of this stage. One test, end to end: the page
    # creates the work, the work links back to the page through ORDINARY
    # wikilinks (so backlinks light up with no playbook-specific code), and a
    # rollup embed on the page counts the children it started. If any link in
    # that chain breaks, "one product" stops being true.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])

        started = _start(client, page["id"])
        assert started.status_code == 201, started.text
        result = started.json()

        # One parent, one child per UNCHECKED step; the ticked one is skipped
        # and reported rather than silently dropped.
        assert result["parent"]["title"] == "Deploy"
        assert [c["title"] for c in result["children"]] == [
            "Freeze the release branch",
            "Run the migration rehearsal",
            "Verify the chain after restore",
        ]
        assert result["checked_skipped"] == 1

        # Every child is nested under the parent — real hierarchy, set through
        # the issue command owner.
        for child in result["children"]:
            fetched = client.get(f"/issues/{child['id']}", headers=H1).json()
            assert fetched["parent_id"] == result["parent"]["id"]

        # The page now shows the work it started, through ordinary backlinks.
        backlinks = client.get(f"/pages/{page['id']}/backlinks", headers=H1).json()
        linked = {row["id"] for row in backlinks if row["kind"] == "issue"}
        assert result["parent"]["id"] in linked
        for child in result["children"]:
            assert child["id"] in linked

        # And a rollup embed on the page counts the children's progress — the
        # docs-show-work direction, over the work docs just started.
        client.patch(
            f"/pages/{page['id']}",
            json={
                "body": CHECKLIST
                + f"\n```athena\nkind: rollup\nissue: {result['parent']['id']}\n```\n"
            },
            headers=H1,
        )
        rendered = client.get(f"/mentor/pages/{page['id']}").text
        assert "3" in rendered  # three children counted by the embed


def test_an_indented_checklist_instantiates_as_a_real_issue_tree(tmp_path):
    # WHY: indentation is structure, all the way to the wire. The checklist's
    # shape must come back as issues.parent_id — set through the ordinary
    # set_issue_parent command — and the reported children must carry their
    # REAL parent_id, not the pre-nesting None.
    nested = (
        "- [ ] Prepare the rollout\n"
        "  - [ ] Freeze the branch\n"
        "  - [ ] Announce the window\n"
        "- [ ] Verify afterwards\n"
        "  - [ ] Walk the chain\n"
    )
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"], body=nested)

        result = _start(client, page["id"]).json()
        by_title = {c["title"]: c for c in result["children"]}
        root = result["parent"]["id"]

        assert by_title["Prepare the rollout"]["parent_id"] == root
        assert by_title["Verify afterwards"]["parent_id"] == root
        assert (
            by_title["Freeze the branch"]["parent_id"]
            == by_title["Prepare the rollout"]["id"]
        )
        assert (
            by_title["Announce the window"]["parent_id"]
            == by_title["Prepare the rollout"]["id"]
        )
        assert (
            by_title["Walk the chain"]["parent_id"]
            == by_title["Verify afterwards"]["id"]
        )

        # The stored rows agree with what the command reported.
        for child in result["children"]:
            fetched = client.get(f"/issues/{child['id']}", headers=H1).json()
            assert fetched["parent_id"] == child["parent_id"]


def test_the_page_snapshot_does_not_follow_later_edits(tmp_path):
    # WHY: a template is not a live mirror. Editing the page after starting it
    # must not touch work already created, and starting again must produce a
    # SECOND independent instantiation — that is what templates are for.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])
        first = _start(client, page["id"]).json()

        client.patch(
            f"/pages/{page['id']}",
            json={"body": "- [ ] An entirely different step\n"},
            headers=H1,
        )
        # The first instantiation is untouched.
        for child in first["children"]:
            assert client.get(f"/issues/{child['id']}", headers=H1).status_code == 200
        assert len(first["children"]) == 3

        second = _start(client, page["id"]).json()
        assert second["parent"]["id"] != first["parent"]["id"]
        assert [c["title"] for c in second["children"]] == [
            "An entirely different step"
        ]


# --- what counts as a step ----------------------------------------------------


def test_the_checklist_parser_reads_only_real_task_lines():
    # WHY: the parser decides what becomes work, so its boundaries are the
    # feature's boundaries. Bullets and box styles it accepts, prose it ignores,
    # ticked boxes it counts, empty boxes it skips.
    steps, checked = playbook_commands.parse_checklist(
        "- [ ] dash\n"
        "* [ ] star\n"
        "+ [ ] plus\n"
        "  - [ ] indented\n"
        "- [x] done lower\n"
        "- [X] done upper\n"
        "- [ ]   \n"  # empty box: not work
        "- [] malformed\n"
        "- regular bullet\n"
        "Just prose about [ ] boxes\n"
    )
    assert [s["title"] for s in steps] == ["dash", "star", "plus", "indented"]
    assert checked == 2
    # "indented" sits under "plus" — indentation is structure now.
    assert [s["parent"] for s in steps] == [None, None, None, 2]


def test_indentation_maps_to_hierarchy_and_promotes_through_non_work():
    # WHY: the whole feature in one body. Relative indent nests; a sibling
    # returns to its level; a DONE step's unchecked children are still work and
    # promote to the nearest ancestor that became work (top level when none),
    # rather than vanishing with their parent or resurrecting it.
    steps, checked = playbook_commands.parse_checklist(
        "- [ ] root a\n"
        "  - [ ] child of a\n"
        "    - [ ] grandchild of a\n"
        "  - [ ] second child of a\n"
        "- [x] root b, already done\n"
        "  - [ ] orphan promotes to top level\n"
        "- [ ] root c\n"
        "\t- [ ] tab-indented child of c\n"
    )
    assert checked == 1
    assert [(s["title"], s["parent"]) for s in steps] == [
        ("root a", None),
        ("child of a", 0),
        ("grandchild of a", 1),
        ("second child of a", 0),
        ("orphan promotes to top level", None),
        ("root c", None),
        ("tab-indented child of c", 5),
    ]


def test_a_long_step_title_is_bounded():
    steps, _ = playbook_commands.parse_checklist("- [ ] " + "x" * 500)
    assert len(steps[0]["title"]) == playbook_commands.MAX_TITLE_LENGTH


def test_the_parser_scans_long_lines_linearly():
    # WHY: the body is author-supplied text and the regex runs over every line.
    # The repeated term and its successor are disjoint by construction, so a
    # long run of indentation cannot backtrack polynomially (core/links.py
    # documents the discipline and why CodeQL flags the alternative).
    import time

    def scan(size: int) -> float:
        line = " " * size + "- [ ] tail"
        start = time.perf_counter()
        playbook_commands.parse_checklist(line)
        return time.perf_counter() - start

    scan(4000)
    small, large = scan(4000), scan(16000)
    assert large < max(small * 8, 0.05), (small, large)


# --- refusals -----------------------------------------------------------------


def test_a_page_without_the_label_is_not_a_playbook(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"], playbook=False)
        refused = _start(client, page["id"])
        assert refused.status_code == 422
        assert "not a playbook" in refused.json()["detail"]


def test_a_playbook_with_no_unchecked_steps_refuses(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"], body="- [x] all done\nprose only\n")
        refused = _start(client, page["id"])
        assert refused.status_code == 422
        assert "no unchecked" in refused.json()["detail"]


def test_too_many_steps_is_a_capacity_refusal(tmp_path):
    # WHY: 51 issues from one click is a data-entry accident, not a plan. The
    # capacity kind maps to 429, the convention for new modules.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        oversized = "".join(
            f"- [ ] step {n}\n" for n in range(playbook_commands.MAX_ITEMS + 1)
        )
        page = _page(client, space["id"], body=oversized)
        refused = _start(client, page["id"])
        assert refused.status_code == 429
        assert str(playbook_commands.MAX_ITEMS) in refused.json()["detail"]
        # Nothing partial was created.
        assert client.get("/issues", headers=H1).json() == []


def test_an_archived_playbook_refuses(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])
        client.post(f"/pages/{page['id']}/archive", headers=H1)
        refused = _start(client, page["id"])
        assert refused.status_code == 422
        assert "archived" in refused.json()["detail"]


def test_a_missing_page_and_an_invisible_one_answer_the_same(tmp_path):
    # WHY: the refusal must not become a probe for private spaces.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        private = _space(client, key="SEC", name="Secret")
        page = _page(client, private["id"])
        # The create route does not take visibility; the space is made private
        # the way every other access test does it.
        conn = db.connect(db_file)
        conn.execute(
            "UPDATE spaces SET visibility = 'private' WHERE id = ?", (private["id"],)
        )
        conn.commit()
        conn.close()
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "Mem", "password": PASSWORD},
            headers=H1,
        ).json()
        outsider = {"X-Athena-Actor": str(member["id"])}

        hidden = _start(client, page["id"], headers=outsider)
        missing = _start(client, 999999, headers=outsider)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json()["detail"] == missing.json()["detail"]


def test_anonymous_callers_cannot_start_work(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])
        assert client.post(f"/pages/{page['id']}/start-playbook").status_code == 401


def test_a_blank_title_override_refuses(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])
        refused = _start(client, page["id"], title="   ")
        assert refused.status_code == 422


# --- placement and identity ----------------------------------------------------


def test_created_work_lands_in_the_named_project_and_takes_the_override(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])
        project = client.post(
            "/projects", json={"key": "REL", "name": "Release"}, headers=H1
        ).json()

        result = _start(
            client, page["id"], project_id=project["id"], title="March release"
        ).json()
        assert result["parent"]["title"] == "March release"
        for issue in [result["parent"], *result["children"]]:
            assert (
                client.get(f"/issues/{issue['id']}", headers=H1).json()["project_id"]
                == project["id"]
            )


def test_the_instantiation_is_attributed_and_audited_like_any_issue_write(tmp_path):
    # WHY: writes go through issue_commands, so they carry the ordinary actor
    # attribution and audit trail. A playbook must not become a second way to
    # create an issue that skips either.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])
        result = _start(client, page["id"]).json()

        events = client.get(
            "/activity", params={"verb": "created", "limit": 50}, headers=H1
        ).json()
        created_ids = {
            event["target_id"] for event in events if event["target_kind"] == "issue"
        }
        assert result["parent"]["id"] in created_ids
        for child in result["children"]:
            assert child["id"] in created_ids
        assert all(event["actor_id"] == 1 for event in events)


def test_a_failed_instantiation_leaves_nothing_behind(tmp_path):
    # WHY: one transaction. A refusal after the parent would otherwise strand
    # an orphan parent with half its steps.
    app, db_file = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"], body="- [ ] " + "x" * 10)
        before = len(client.get("/issues", headers=H1).json())

        # Force a failure inside the loop, after the parent exists.
        original = playbook_commands.issue_commands.set_issue_parent

        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        playbook_commands.issue_commands.set_issue_parent = explode
        try:
            with pytest.raises(RuntimeError):
                _start(client, page["id"])
        finally:
            playbook_commands.issue_commands.set_issue_parent = original

        assert len(client.get("/issues", headers=H1).json()) == before


# --- retry safety and MCP -------------------------------------------------------


def test_an_idempotency_key_makes_a_retry_safe(tmp_path):
    # WHY: starting a playbook creates many issues, so a network retry must not
    # double them. The /pages root already honors Idempotency-Key; this feature
    # reuses that contract rather than inventing a second replay mechanism.
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        space = _space(client)
        page = _page(client, space["id"])
        headers = {**H1, "Idempotency-Key": "start-once"}

        first = client.post(
            f"/pages/{page['id']}/start-playbook", json={}, headers=headers
        )
        assert first.status_code == 201
        replay = client.post(
            f"/pages/{page['id']}/start-playbook", json={}, headers=headers
        )
        assert replay.status_code == 201
        assert replay.json()["parent"]["id"] == first.json()["parent"]["id"]
        # Four issues total (one parent + three steps), not eight.
        assert len(client.get("/issues", headers=H1).json()) == 4


def test_mcp_starts_a_playbook_and_surfaces_refusals(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as tc:
        _bootstrap(tc)
        space = _space(tc)
        page = _page(tc, space["id"])
        plain = _page(tc, space["id"], title="Notes", playbook=False)
        raw = tc.post(
            "/tokens", json={"name": "mcp", "scopes": ["admin"]}, headers=H1
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        api = AthenaClient(client=tc)

        result = api.start_playbook(page["id"])
        assert len(result["children"]) == 3
        assert result["checked_skipped"] == 1

        with pytest.raises(AthenaError) as refused:
            api.start_playbook(plain["id"])
        assert refused.value.status_code == 422
