"""Stage Q — planning: the project timeline and live parent rollups.

Two read models with one shared discipline: compute at read time, gate by the
viewer, and say out loud what was left out. Each layer can fail differently, so
each is exercised where it lives — geometry against a real database, visibility
over real HTTP, and the embed through the same resolver a page uses.

The bug this suite exists to keep dead: the rollup's GROUP BY once bound to the
LEFT JOINed ``project_statuses.category`` column instead of its own aliased
expression, which collapsed every backlog child into a single group and reported
one child's category for all of them — a parent with one done child out of two
read "2/2 done".
"""

from fastapi.testclient import TestClient
import pytest

from athena.aegis import dependencies, issues, projects, rollups, sprints, timeline
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
PASSWORD = "pw-long-enough"


def _conn(tmp_path, name="q.db"):
    conn = db.connect(tmp_path / name)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users (name, email, password_hash, role) "
        "VALUES ('Op', 'op@e.com', 'x', 'admin')"
    )
    conn.commit()
    return conn


def _issue(conn, title, *, project_id=None, status="open"):
    made = issues.create_issue(
        conn, title=title, body="", created_by=1, project_id=project_id
    )
    if status != "open":
        issues.update_issue(conn, made["id"], status=status)
    return made["id"]


def _admin(client):
    client.post(
        "/users", json={"email": "a@e.com", "name": "Ann", "password": PASSWORD}
    )


def _login(client, email="a@e.com"):
    client.post("/login", data={"email": email, "password": PASSWORD})
    return client


# ---------------------------------------------------------------------------
# Q-1 — the timeline's geometry and honesty.
# ---------------------------------------------------------------------------


def test_lanes_run_in_date_order_with_undated_then_backlog_last(tmp_path):
    """Lane ORDER is the only claim the timeline makes about time. Dated sprints
    lead in date order, undated ones follow in creation order rather than being
    given a fabricated date, and the backlog sits last because it is where work
    waits when it is not scheduled at all."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    later = sprints.create_sprint(
        conn, project_id=pid, name="March", start_date="2026-03-01"
    )
    earlier = sprints.create_sprint(
        conn, project_id=pid, name="January", start_date="2026-01-01"
    )
    undated = sprints.create_sprint(conn, project_id=pid, name="Someday")
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid)
    assert [lane["label"] for lane in drawn["lanes"]] == [
        "January",
        "March",
        "Someday",
        "Backlog",
    ]
    # Lanes are evenly spaced: width is not a duration, so equal columns are the
    # honest rendering of "these came in this order".
    xs = [lane["x"] for lane in drawn["lanes"]]
    gaps = {round(b - a, 2) for a, b in zip(xs, xs[1:])}
    assert len(gaps) == 1
    assert {earlier["id"], later["id"], undated["id"]}  # ids exist, unused further


def test_issues_land_in_their_sprint_lane_and_the_backlog_holds_the_rest(tmp_path):
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    cycle = sprints.create_sprint(
        conn, project_id=pid, name="C1", start_date="2026-01-01"
    )
    scheduled = _issue(conn, "scheduled", project_id=pid)
    unscheduled = _issue(conn, "unscheduled", project_id=pid)
    issues.set_sprint(conn, scheduled, cycle["id"])
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid)
    placement = {card["id"]: card["lane"] for card in drawn["cards"]}
    assert placement[scheduled] == str(cycle["id"])
    assert placement[unscheduled] == timeline.BACKLOG_LANE
    assert drawn["shown"] == drawn["total"] == 2
    assert drawn["truncated"] is False


def test_archived_issues_are_left_off_the_timeline(tmp_path):
    """Archived work is soft-deleted everywhere else; a roadmap that still drew
    it would show a plan nobody is working."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    live = _issue(conn, "live", project_id=pid)
    gone = _issue(conn, "abandoned", project_id=pid)
    conn.execute(
        "UPDATE issues SET archived_at = datetime('now') WHERE id = ?", (gone,)
    )
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid)
    assert [card["id"] for card in drawn["cards"]] == [live]


def test_edges_are_drawn_between_drawn_cards_and_directed_for_blocks(tmp_path):
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    blocker = _issue(conn, "blocker", project_id=pid)
    blocked = _issue(conn, "blocked", project_id=pid)
    cousin = _issue(conn, "cousin", project_id=pid)
    dependencies.add_link(
        conn, from_id=blocker, to_id=blocked, relation="blocks", created_by=1
    )
    dependencies.add_link(
        conn, from_id=blocked, to_id=cousin, relation="relates", created_by=1
    )
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid)
    kinds = {(edge["from_id"], edge["to_id"]): edge["kind"] for edge in drawn["edges"]}
    assert kinds[(blocker, blocked)] == "blocks"
    # 'relates' is stored once with the lower id first, so it draws exactly one
    # line regardless of which end the reader thinks of as the source.
    assert kinds[(min(blocked, cousin), max(blocked, cousin))] == "relates"
    assert len(drawn["edges"]) == 2
    assert drawn["edges_outside"] == 0


def test_a_dependency_leaving_the_view_is_counted_not_drawn(tmp_path):
    """An arrow to something off the picture reads as a rendering bug; no arrow
    at all reads as "nothing blocks this". The count is the third option."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    inside = _issue(conn, "inside", project_id=pid)
    outside = _issue(conn, "outside")  # backlog: not in this project
    dependencies.add_link(
        conn, from_id=inside, to_id=outside, relation="blocks", created_by=1
    )
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid)
    assert drawn["edges"] == []
    assert drawn["edges_outside"] == 1


def test_a_dependency_onto_archived_work_is_not_an_outside_edge(tmp_path):
    """The picture shows live work only, so its "N reach beyond" number must
    mean edges to work that could be drawn somewhere. A link onto archived
    history counted here would read as a live outside dependency that does not
    exist (review finding)."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    inside = _issue(conn, "inside", project_id=pid)
    outside = _issue(conn, "outside")  # backlog: off the picture
    dependencies.add_link(
        conn, from_id=inside, to_id=outside, relation="blocks", created_by=1
    )
    conn.commit()
    assert timeline.project_timeline(conn, project_id=pid)["edges_outside"] == 1

    conn.execute(
        "UPDATE issues SET archived_at = datetime('now') WHERE id = ?", (outside,)
    )
    conn.commit()
    assert timeline.project_timeline(conn, project_id=pid)["edges_outside"] == 0


def test_a_dependency_cycle_lays_out_without_hanging(tmp_path):
    """Only the direct two-cycle is refused at write time, so A→B→C→A is real
    data. Placement comes from sprint membership alone — nothing here sorts
    topologically, so a cycle can only change the arrows."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    a = _issue(conn, "a", project_id=pid)
    b = _issue(conn, "b", project_id=pid)
    c = _issue(conn, "c", project_id=pid)
    for source, target in ((a, b), (b, c), (c, a)):
        dependencies.add_link(
            conn, from_id=source, to_id=target, relation="blocks", created_by=1
        )
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid)
    assert len(drawn["edges"]) == 3
    assert drawn["shown"] == 3


def test_the_timeline_reports_what_each_lane_and_the_page_left_out(tmp_path):
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    cycle = sprints.create_sprint(
        conn, project_id=pid, name="C1", start_date="2026-01-01"
    )
    for index in range(9):
        made = _issue(conn, f"issue {index}", project_id=pid)
        issues.set_sprint(conn, made, cycle["id"])
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid, max_per_lane=4)
    lane = next(item for item in drawn["lanes"] if item["label"] == "C1")
    assert (lane["shown"], lane["total"], lane["truncated"]) == (4, 9, True)
    assert (drawn["shown"], drawn["total"], drawn["truncated"]) == (4, 9, True)

    # The whole-picture ceiling bites independently of the per-lane one.
    capped = timeline.project_timeline(conn, project_id=pid, max_items=2)
    assert capped["shown"] == 2
    assert capped["truncated"] is True


def test_timeline_bounds_are_clamped_not_trusted(tmp_path):
    """A caller asking for more than the ceiling gets the ceiling, and a caller
    passing nonsense gets the default — both observed through what is actually
    drawn, not merely by the call not raising."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    cycle = sprints.create_sprint(
        conn, project_id=pid, name="C1", start_date="2026-01-01"
    )
    for index in range(timeline.MAX_MAX_PER_LANE + 5):
        made = _issue(conn, f"issue {index}", project_id=pid)
        issues.set_sprint(conn, made, cycle["id"])
    conn.commit()

    greedy = timeline.project_timeline(
        conn, project_id=pid, max_per_lane=10_000, max_items=10_000
    )
    assert greedy["shown"] == timeline.MAX_MAX_PER_LANE
    assert greedy["truncated"] is True

    defaulted = timeline.project_timeline(
        conn,
        project_id=pid,
        max_per_lane="all",  # type: ignore[arg-type]
        max_items=None,  # type: ignore[arg-type]
    )
    assert defaulted["shown"] == timeline.DEFAULT_MAX_PER_LANE


def test_a_lane_drained_by_the_page_ceiling_says_so(tmp_path):
    """The overall ceiling is spent left to right, so a later lane can be cut to
    nothing. A lane that merely looked empty would read as "no work here"."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    first = sprints.create_sprint(
        conn, project_id=pid, name="First", start_date="2026-01-01"
    )
    second = sprints.create_sprint(
        conn, project_id=pid, name="Second", start_date="2026-02-01"
    )
    for sprint in (first, second):
        for index in range(3):
            made = _issue(conn, f"{sprint['name']} {index}", project_id=pid)
            issues.set_sprint(conn, made, sprint["id"])
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid, max_items=3)
    lanes = {lane["label"]: lane for lane in drawn["lanes"]}
    assert (lanes["First"]["shown"], lanes["First"]["hidden"]) == (3, 0)
    # Drained to nothing by the ceiling — and it reports the whole lane missing.
    assert (lanes["Second"]["shown"], lanes["Second"]["hidden"]) == (0, 3)
    assert lanes["Second"]["truncated"] is True


def test_edge_endpoints_sit_on_the_cards_they_connect(tmp_path):
    """Geometry lives in one place. If the card's box and the edge's endpoint
    were computed separately they would drift, and an arrow would land beside
    the box it points at."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    source = _issue(conn, "source", project_id=pid)
    target = _issue(conn, "target", project_id=pid)
    dependencies.add_link(
        conn, from_id=source, to_id=target, relation="blocks", created_by=1
    )
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid)
    cards = {card["id"]: card for card in drawn["cards"]}
    edge = drawn["edges"][0]
    assert edge["x1"] == cards[source]["x"] + cards[source]["width"]
    assert edge["x2"] == cards[target]["x"]
    assert edge["y1"] == cards[source]["y"] + cards[source]["height"] / 2
    # Cards sit inside their lane, so an endpoint is never on the lane border.
    lane = drawn["lanes"][0]
    assert cards[source]["x"] > lane["x"]
    assert cards[source]["x"] + cards[source]["width"] < lane["x"] + lane["width"]


def test_the_off_picture_edge_count_ignores_work_the_viewer_cannot_see(tmp_path):
    """The counter must not move when a HIDDEN issue gains a link to a visible
    one. A number that reacts to private work is an existence oracle just as
    surely as a title would be — the rule the rollup already follows."""
    conn = _conn(tmp_path)
    public = projects.create_project(conn, key="PUB", name="Public", created_by=1)["id"]
    secret = projects.create_project(conn, key="SEC", name="Secret", created_by=1)["id"]
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (secret,))
    shown = _issue(conn, "public work", project_id=public)
    hidden = _issue(conn, "classified", project_id=secret)
    conn.commit()

    def outsider_count():
        return timeline.project_timeline(
            conn, project_id=public, visible_project_ids={public}
        )["edges_outside"]

    before = outsider_count()
    dependencies.add_link(
        conn, from_id=hidden, to_id=shown, relation="blocks", created_by=1
    )
    conn.commit()
    assert outsider_count() == before == 0

    # An admin, who may see it, still gets a truthful count.
    assert (
        timeline.project_timeline(conn, project_id=public, visible_project_ids=None)[
            "edges_outside"
        ]
        == 1
    )

    # And a link to work the viewer CAN see is still counted for them.
    elsewhere = _issue(conn, "other public work")
    dependencies.add_link(
        conn, from_id=shown, to_id=elsewhere, relation="blocks", created_by=1
    )
    conn.commit()
    assert outsider_count() == 1


def test_timeline_hides_issues_the_viewer_cannot_see(tmp_path):
    """The gate is on the ISSUES: a cross-project child of this project's work
    could sit in a private project, and an ungated lane would leak its title."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    _issue(conn, "public work", project_id=pid)
    conn.commit()

    ungated = timeline.project_timeline(conn, project_id=pid)
    assert ungated["shown"] == 1
    # A viewer with no visible projects sees only backlog issues — none here.
    gated = timeline.project_timeline(conn, project_id=pid, visible_project_ids=set())
    assert gated["shown"] == 0


# ---------------------------------------------------------------------------
# Q-2 — rollups: the numbers, and the bug that made them lie.
# ---------------------------------------------------------------------------


def test_backlog_children_are_bucketed_individually(tmp_path):
    """The regression test for the GROUP BY alias collision. Backlog children
    have NO project_statuses rows, so the joined category column is NULL for
    every one of them — grouping on that column instead of the resolved
    expression silently merged them into a single bucket."""
    conn = _conn(tmp_path)
    parent = _issue(conn, "epic")
    _issue(conn, "shipped", status="done")
    _issue(conn, "underway", status="in_progress")
    _issue(conn, "waiting")
    for child in (2, 3, 4):
        issues.set_parent(conn, child, parent)
    conn.commit()

    rolled = rollups.child_rollup(conn, parent)
    assert rolled["counts"] == {"todo": 1, "doing": 1, "done": 1}
    assert rolled["total"] == 3
    assert rolled["done"] == 1
    assert rolled["percent_done"] == 33


def test_done_ness_follows_the_project_category_not_the_status_name(tmp_path):
    """A project whose finished state is called `shipped` must roll up exactly
    like one that calls it `done` — the promise QUERY.md and WORKFLOW_GATES.md
    already make about closed-ness."""
    conn = _conn(tmp_path)
    from athena.aegis import statuses

    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    statuses.add_status(conn, pid, "shipped", "done")
    conn.commit()
    parent = _issue(conn, "epic", project_id=pid)
    child = _issue(conn, "custom done", project_id=pid)
    issues.update_issue(conn, child, status="shipped")
    issues.set_parent(conn, child, parent)
    conn.commit()

    rolled = rollups.child_rollup(conn, parent)
    assert rolled["counts"]["done"] == 1
    assert rolled["percent_done"] == 100


def test_archived_children_are_excluded_and_said_out_loud(tmp_path):
    """Abandoned work must not sit in a denominator forever, but a bar that
    quietly drops rows is the same lie an unlabelled partial picture would be."""
    conn = _conn(tmp_path)
    parent = _issue(conn, "epic")
    live = _issue(conn, "live")
    gone = _issue(conn, "abandoned")
    issues.set_parent(conn, live, parent)
    issues.set_parent(conn, gone, parent)
    conn.execute(
        "UPDATE issues SET archived_at = datetime('now') WHERE id = ?", (gone,)
    )
    conn.commit()

    rolled = rollups.child_rollup(conn, parent)
    assert rolled["total"] == 1
    assert rolled["archived_excluded"] == 1
    assert rolled["has_children"] is True


def test_percent_reserves_the_ends_for_the_truth(tmp_path):
    """100% must mean everything is done and 0% must mean nothing is — plain
    rounding would call 199 of 200 children a finished job."""
    conn = _conn(tmp_path)
    parent = _issue(conn, "epic")
    children = [_issue(conn, f"child {index}") for index in range(200)]
    for child in children:
        issues.set_parent(conn, child, parent)
    for child in children[:199]:
        issues.update_issue(conn, child, status="done")
    conn.commit()

    nearly = rollups.child_rollup(conn, parent)
    assert nearly["done"] == 199 and nearly["total"] == 200
    assert nearly["percent_done"] == 99

    issues.update_issue(conn, children[199], status="done")
    conn.commit()
    assert rollups.child_rollup(conn, parent)["percent_done"] == 100


def test_a_parent_whose_children_are_all_archived_says_so(tmp_path):
    """Reporting 0% would read as untouched work; there is work, and all of it
    was set aside."""
    conn = _conn(tmp_path)
    parent = _issue(conn, "epic")
    child = _issue(conn, "abandoned")
    issues.set_parent(conn, child, parent)
    conn.execute(
        "UPDATE issues SET archived_at = datetime('now') WHERE id = ?", (child,)
    )
    conn.commit()

    rolled = rollups.child_rollup(conn, parent)
    assert rolled["total"] == 0
    assert rolled["archived_excluded"] == 1
    assert rolled["has_children"] is True
    assert rolled["segments"] == []


def test_segment_widths_are_computed_once_for_both_surfaces(tmp_path):
    conn = _conn(tmp_path)
    parent = _issue(conn, "epic")
    for index, status in enumerate(("done", "in_progress", "open", "open")):
        child = _issue(conn, f"child {index}", status=status)
        issues.set_parent(conn, child, parent)
    conn.commit()

    rolled = rollups.child_rollup(conn, parent)
    segments = rolled["segments"]
    assert [segment["bucket"] for segment in segments] == ["done", "doing", "todo"]
    assert [segment["count"] for segment in segments] == [1, 1, 2]
    assert sum(segment["percent"] for segment in segments) == pytest.approx(100)
    # The bar's buckets DERIVE from the one vocabulary (finished-first), never
    # a second literal tuple: a category added to statuses.CATEGORIES must
    # appear in the bar the same day it appears in the counts, or the bar
    # quietly sums below 100% (review finding).
    assert [segment["bucket"] for segment in segments] == [
        bucket for bucket in reversed(rollups.BUCKETS) if rolled["counts"][bucket]
    ]


def test_a_parent_with_no_children_reports_no_progress(tmp_path):
    """Zero children is not "finished" — 0% with no bar, never 100%."""
    conn = _conn(tmp_path)
    lonely = _issue(conn, "no kids")
    conn.commit()
    rolled = rollups.child_rollup(conn, lonely)
    assert rolled == {
        "counts": {"todo": 0, "doing": 0, "done": 0},
        "segments": [],
        "total": 0,
        "done": 0,
        "percent_done": 0,
        "archived_excluded": 0,
        "has_children": False,
    }


def test_rollup_counts_only_children_the_viewer_can_see(tmp_path):
    """A child may live in a private project under a visible parent. Counting it
    would turn the progress bar into an existence oracle for that work."""
    conn = _conn(tmp_path)
    private = projects.create_project(conn, key="SEC", name="Secret", created_by=1)[
        "id"
    ]
    conn.execute("UPDATE projects SET visibility = 'private' WHERE id = ?", (private,))
    parent = _issue(conn, "epic")
    hidden = _issue(conn, "hidden child", project_id=private, status="done")
    open_child = _issue(conn, "open child")
    issues.set_parent(conn, hidden, parent)
    issues.set_parent(conn, open_child, parent)
    conn.commit()

    everything = rollups.child_rollup(conn, parent)
    assert everything["total"] == 2

    outsider = rollups.child_rollup(conn, parent, visible_project_ids=set())
    assert outsider["total"] == 1
    assert outsider["counts"]["done"] == 0
    # The hidden child is absent from the count and NOT reported as excluded:
    # naming it would be the leak this gate exists to prevent.
    assert outsider["archived_excluded"] == 0


# ---------------------------------------------------------------------------
# The surfaces: web, REST, MCP, and the embed.
# ---------------------------------------------------------------------------


def test_timeline_page_draws_and_states_its_bounds(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        _admin(client)
        _login(client)
        project = client.post(
            "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
        ).json()
        client.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "C1", "start_date": "2026-01-01"},
            headers=H1,
        )
        client.post(
            "/issues", json={"title": "work", "project_id": project["id"]}, headers=H1
        )

        page = client.get(f"/aegis/projects/{project['id']}/timeline")
        assert page.status_code == 200
        assert "<svg" in page.text
        # Every card is a real link, so the picture works without JavaScript.
        assert "/aegis/issues/" in page.text
        assert "Archived issues are left out" in page.text
        assert "not a duration" in page.text


def test_timeline_hides_a_project_the_viewer_cannot_see(tmp_path):
    app = create_app(tmp_path / "vis.db")
    with TestClient(app) as owner:
        _admin(owner)
        _login(owner)
        project = owner.post(
            "/projects", json={"key": "SEC", "name": "Secret"}, headers=H1
        ).json()
        owner.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        owner.post(
            "/issues",
            json={"title": "classified", "project_id": project["id"]},
            headers=H1,
        )
        assert owner.get(f"/aegis/projects/{project['id']}/timeline").status_code == 200

        anonymous = TestClient(app)
        hidden = anonymous.get(f"/aegis/projects/{project['id']}/timeline")
        # A hidden project answers exactly like a missing one.
        assert hidden.status_code == 404
        assert "classified" not in hidden.text
        assert (
            anonymous.get("/aegis/projects/99999/timeline").status_code
            == hidden.status_code
        )


def test_timeline_rest_returns_the_same_structure_the_browser_draws(tmp_path):
    with TestClient(create_app(tmp_path / "rest.db")) as client:
        _admin(client)
        project = client.post(
            "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
        ).json()
        client.post(
            "/issues", json={"title": "work", "project_id": project["id"]}, headers=H1
        )

        payload = client.get(f"/projects/{project['id']}/timeline", headers=H1)
        assert payload.status_code == 200
        body = payload.json()
        assert {"lanes", "cards", "edges", "shown", "total", "truncated"} <= set(body)
        assert body["shown"] == 1
        assert client.get("/projects/99999/timeline", headers=H1).status_code == 404


def test_issue_page_shows_a_live_progress_bar(tmp_path):
    with TestClient(create_app(tmp_path / "bar.db")) as client:
        _admin(client)
        _login(client)
        parent = client.post("/issues", json={"title": "epic"}, headers=H1).json()
        first = client.post("/issues", json={"title": "one"}, headers=H1).json()
        second = client.post("/issues", json={"title": "two"}, headers=H1).json()
        for child in (first, second):
            client.put(
                f"/issues/{child['id']}/parent",
                json={"parent_id": parent["id"]},
                headers=H1,
            )
        client.patch(f"/issues/{first['id']}", json={"status": "done"}, headers=H1)

        page = client.get(f"/aegis/issues/{parent['id']}").text
        assert "(1/2 done)" in page
        assert "rollup-bar" in page
        assert "50% done" in page

        # Live: closing the second child moves the bar with no stored column.
        client.patch(f"/issues/{second['id']}", json={"status": "done"}, headers=H1)
        assert "100% done" in client.get(f"/aegis/issues/{parent['id']}").text


def test_rollup_embed_resolves_through_the_same_computation(tmp_path):
    with TestClient(create_app(tmp_path / "embed.db")) as client:
        _admin(client)
        parent = client.post("/issues", json={"title": "epic"}, headers=H1).json()
        child = client.post("/issues", json={"title": "one"}, headers=H1).json()
        client.put(
            f"/issues/{child['id']}/parent",
            json={"parent_id": parent["id"]},
            headers=H1,
        )

        resolved = client.post(
            "/embeds/resolve",
            json={"text": f"```athena\nkind: rollup\nissue: {parent['id']}\n```"},
            headers=H1,
        ).json()[0]
        assert resolved["kind"] == "rollup"
        assert resolved["error"] is None
        assert resolved["rollup"]["total"] == 1
        assert resolved["item"]["id"] == parent["id"]

        # The vocabulary is emitted, not restated, so help learns the kind free.
        assert "rollup" in client.get("/embeds/help", headers=H1).json()["kinds"]


@pytest.mark.parametrize(
    ("directive", "fragment"),
    [
        ("kind: rollup", "needs an 'issue:'"),
        ("kind: rollup\nissue: 1\nq: is:open", "takes an 'issue:'"),
        ("kind: rollup\nissue: 99999", "no issue matches"),
        ("kind: count\nissue: 1", "'kind: issue' or 'kind: rollup'"),
    ],
)
def test_rollup_directive_refusals_name_the_problem(tmp_path, directive, fragment):
    """A refused embed renders a message naming what was wrong — never a blank
    space that reads as "nothing to show"."""
    with TestClient(create_app(tmp_path / "refuse.db")) as client:
        _admin(client)
        client.post("/issues", json={"title": "an issue"}, headers=H1)
        resolved = client.post(
            "/embeds/resolve",
            json={"text": f"```athena\n{directive}\n```"},
            headers=H1,
        ).json()[0]
        assert fragment in resolved["error"]


def test_rollup_embed_counts_only_what_the_reader_may_see(tmp_path):
    """Two readers, one directive, different numbers — the embed resolves per
    request against the viewer, and a private child never reaches an outsider."""
    app = create_app(tmp_path / "embedvis.db")
    with TestClient(app) as owner:
        _admin(owner)
        _login(owner)
        private = owner.post(
            "/projects", json={"key": "SEC", "name": "Secret"}, headers=H1
        ).json()
        owner.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        parent = owner.post("/issues", json={"title": "epic"}, headers=H1).json()
        hidden = owner.post(
            "/issues",
            json={"title": "classified", "project_id": private["id"]},
            headers=H1,
        ).json()
        plain = owner.post("/issues", json={"title": "open"}, headers=H1).json()
        for child in (hidden, plain):
            owner.put(
                f"/issues/{child['id']}/parent",
                json={"parent_id": parent["id"]},
                headers=H1,
            )

        text = f"```athena\nkind: rollup\nissue: {parent['id']}\n```"
        mine = owner.post("/embeds/resolve", json={"text": text}, headers=H1).json()[0]
        assert mine["rollup"]["total"] == 2

        anonymous = TestClient(app)
        theirs = anonymous.post("/embeds/resolve", json={"text": text}).json()[0]
        assert theirs["rollup"]["total"] == 1


def test_mcp_client_reads_the_timeline_and_bounds_ride_through(tmp_path):
    """Steering rule 1: the timeline must be reachable over MCP, and the
    client's bound parameters must reach the service — both previously
    untested (review finding)."""
    from athena.mcp.client import AthenaClient

    with TestClient(create_app(tmp_path / "mcp_timeline.db")) as tc:
        _admin(tc)
        project = tc.post(
            "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
        ).json()
        for title in ("one", "two", "three"):
            tc.post(
                "/issues",
                json={"title": title, "project_id": project["id"]},
                headers=H1,
            )
        raw = tc.post(
            "/tokens", json={"name": "mcp", "scopes": ["admin"]}, headers=H1
        ).json()["token"]
        tc.headers.update({"Authorization": f"Bearer {raw}"})
        api = AthenaClient(client=tc)

        drawn = api.project_timeline(project["id"])
        assert drawn["shown"] == 3
        assert drawn["lanes"][-1]["label"] == "Backlog"

        clamped = api.project_timeline(project["id"], max_per_lane=1, max_items=1)
        assert clamped["shown"] == 1
        assert clamped["truncated"] is True
        assert clamped["total"] == 3


def test_an_undrawable_timeline_says_so_without_claiming_no_issues_exist(tmp_path):
    """The empty state must state what is TRUE — nothing drawable for this
    viewer — not "no issues", which would be false for a project whose work is
    all archived or all hidden (review finding: empty state untested)."""
    with TestClient(create_app(tmp_path / "empty_timeline.db")) as client:
        _admin(client)
        _login(client)
        project = client.post(
            "/projects", json={"key": "EMP", "name": "Empty"}, headers=H1
        ).json()
        page = client.get(f"/aegis/projects/{project['id']}/timeline")
        assert page.status_code == 200
        assert "Nothing to draw yet" in page.text
        assert "and no live issues you can" in page.text
        assert "Plan a sprint" in page.text
        assert "<svg" not in page.text


def test_ancient_sprint_lanes_drop_oldest_first_and_say_what_left(tmp_path):
    """Width grew by one lane per sprint forever. The cap keeps the newest
    sprints, and the dropped lanes' issues are counted out loud rather than
    misfiled into the backlog — a card in the wrong lane is worse than an
    admitted omission (review finding)."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    cycles = [
        sprints.create_sprint(
            conn, project_id=pid, name=f"C{n}", start_date=f"2026-0{n}-01"
        )
        for n in range(1, 5)
    ]
    ancient = _issue(conn, "ancient work", project_id=pid)
    issues.set_sprint(conn, ancient, cycles[0]["id"])
    fresh = _issue(conn, "fresh work", project_id=pid)
    issues.set_sprint(conn, fresh, cycles[3]["id"])
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid, max_lanes=3)
    assert [lane["label"] for lane in drawn["lanes"]] == ["C2", "C3", "C4", "Backlog"]
    assert drawn["omitted_lanes"] == 1
    assert drawn["omitted_lane_issues"] == 1
    # The ancient issue is neither drawn nor silently re-homed to the backlog.
    assert [card["id"] for card in drawn["cards"]] == [fresh]
    assert drawn["total"] == 1

    # Without the cap biting, nothing is omitted and nothing is claimed.
    full = timeline.project_timeline(conn, project_id=pid)
    assert (full["omitted_lanes"], full["omitted_lane_issues"]) == (0, 0)


def test_the_issue_read_is_bounded_and_admits_the_cap(tmp_path):
    """The drawn set was always capped; the QUERY was not, and it is reachable
    anonymously on a public project. When the cap bites, the totals describe
    what was loaded and the flag says more exist (review finding)."""
    conn = _conn(tmp_path)
    pid = projects.create_project(conn, key="ATH", name="A", created_by=1)["id"]
    for n in range(5):
        _issue(conn, f"work {n}", project_id=pid)
    conn.commit()

    drawn = timeline.project_timeline(conn, project_id=pid, max_fetch=3)
    assert drawn["fetch_clipped"] is True
    assert drawn["fetch_limit"] == 3
    assert drawn["total"] == 3

    full = timeline.project_timeline(conn, project_id=pid)
    assert full["fetch_clipped"] is False
    assert full["total"] == 5
