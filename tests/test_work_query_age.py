"""Age and claim atoms in the work query, and the browser search speaking them.

The operator's first question about a quiet board is "what has gone untouched?"
The query language could not ask it: no age atom, no idle atom, no way to say
"nobody holds this". These pin the new atoms:

- ``idle:>Nd`` / ``idle:<Nd`` measure from the last recorded WORK on the issue (the
  same verb list as the progress view), falling back to the creation time when no
  work was ever recorded. A check-in or a lease renewal never resets it.
- ``age:>Nd`` / ``age:<Nd`` measure from creation.
- ``is:claimed`` / ``is:unclaimed`` follow the lease clock: an expired lease is
  unclaimed.
- ``sort:idle-desc`` puts the longest-idle work first.
- A typo refuses with a readable error naming the atom, never an empty result.
- Visibility applies to the rows, the count and the browser search alike.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from athena.aegis import issue_query, work_progress
from athena.core import db, work_query
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
_TS = "%Y-%m-%d %H:%M:%S"


def _app(tmp_path, name="age.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _login(client, email="a@e.com"):
    client.post(
        "/login", data={"email": email, "password": "pw"}, follow_redirects=False
    )


def _agent(client, email="sol@e.com"):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": ["read", "issue:write"]},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _ago(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).strftime(_TS)


def _sql(db_file, statement, params=()):
    conn = db.connect(db_file)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _issue(client, title, **fields):
    return client.post("/issues", json={"title": title, **fields}, headers=H1).json()


def _make_old(db_file, issue_id, *, days):
    """Back-date the issue and every event on it, as if it all happened long ago."""
    stamp = _ago(days=days)
    _sql(db_file, "UPDATE issues SET created_at = ? WHERE id = ?", (stamp, issue_id))
    _sql(
        db_file,
        "UPDATE activity SET created_at = ? WHERE target_kind = 'issue' "
        "AND target_id = ?",
        (stamp, issue_id),
    )


def _claim(client, agent, issue_id):
    client.post(
        f"/issues/{issue_id}/delegate",
        json={"user_id": agent["user"]["id"]},
        headers=H1,
    )
    read = client.get(f"/issues/{issue_id}", headers=_bearer(agent))
    claimed = client.post(
        f"/issues/{issue_id}/claim",
        headers={
            **_bearer(agent),
            "If-Match": read.headers["ETag"],
            "X-Athena-Run": f"run-{issue_id}",
        },
    )
    assert claimed.status_code in (200, 201), claimed.text
    beat = client.put(
        "/agent-runs/heartbeat",
        json={"run_id": f"run-{issue_id}"},
        headers=_bearer(agent),
    )
    assert beat.status_code == 200, beat.text


def _titles(client, query, headers=H1):
    r = client.get("/issues", params={"q": query, "limit": 100}, headers=headers)
    assert r.status_code == 200, r.text
    return [row["title"] for row in r.json()]


def _count(client, query, headers=H1):
    r = client.get("/issues/query/count", params={"q": query}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["matched"]


# ── grammar: closed shapes, readable refusals ───────────────────────────────


@pytest.mark.parametrize(
    "raw", ["idle:>14d", "idle:<7d", "age:>60d", "age:<1d", "IDLE:>14D"]
)
def test_age_and_idle_atoms_parse(raw):
    query = work_query.parse(raw)
    (term,) = query.terms
    assert term.field in ("idle", "age")
    assert term.value == raw.split(":", 1)[1].lower()
    assert work_query.parse_days(term.value) in (
        (">", 14),
        ("<", 7),
        (">", 60),
        ("<", 1),
    )


def test_claim_atoms_and_idle_sort_parse():
    query = work_query.parse("is:claimed -is:unclaimed sort:idle-desc")
    assert query.sort == "idle-desc"
    assert [(t.value, t.negated) for t in query.terms] == [
        ("claimed", False),
        ("unclaimed", True),
    ]


@pytest.mark.parametrize(
    ("raw", "atom"),
    [
        ("idle:14d", "idle:14d"),
        ("idle:>14", "idle:>14"),
        ("idle:>14h", "idle:>14h"),
        ("idle:>-3d", "idle:>-3d"),
        ("age:>=3d", "age:>=3d"),
        ("idle:>99999d", "idle:>99999d"),
        ("is:claimd", "is:claimd"),
        ("sort:idle", "sort:idle"),
        ("idel:>14d", "idel"),
    ],
)
def test_a_typod_atom_refuses_with_a_readable_error(tmp_path, raw, atom):
    with pytest.raises(work_query.QueryError) as caught:
        work_query.parse(raw)
    assert caught.value.atom == atom
    assert atom in str(caught.value)
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        refused = c.get("/issues", params={"q": raw}, headers=H1)
        assert refused.status_code == 422
        assert refused.json()["detail"]["atom"] == atom
        # Positive control: the same request with a valid atom is answered.
        assert c.get("/issues", params={"q": "idle:>1d"}, headers=H1).status_code == 200


def test_the_shape_error_says_what_would_have_worked():
    with pytest.raises(work_query.QueryError) as caught:
        work_query.parse("idle:14d")
    assert "idle:>Nd" in str(caught.value) and "idle:<Nd" in str(caught.value)


def test_describe_lists_the_new_atoms():
    described = work_query.describe()
    assert "claimed" in described["fields"]["is"]
    assert "unclaimed" in described["fields"]["is"]
    assert "idle-desc" in described["fields"]["sort"]
    assert "idle" in described["fields"] and "age" in described["fields"]


# ── idle is measured from work, not from pulse ──────────────────────────────


def test_an_old_issue_with_a_fresh_heartbeat_still_matches_idle(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        stalled = _issue(c, "stalled but reporting")
        c.post(
            f"/issues/{stalled['id']}/comments",
            json={"body": "started"},
            headers=H1,
        )
        _make_old(db_file, stalled["id"], days=30)
        # Claimed, renewed and heart-beating today: alive, but no work since.
        _claim(c, agent, stalled["id"])
        busy = _issue(c, "worked today")
        c.post(f"/issues/{busy['id']}/comments", json={"body": "w"}, headers=H1)
        _make_old(db_file, busy["id"], days=30)
        c.post(f"/issues/{busy['id']}/comments", json={"body": "fresh"}, headers=H1)
        never = _issue(c, "never worked, old")
        _make_old(db_file, never["id"], days=30)
        newborn = _issue(c, "never worked, new")

        idle = _titles(c, "idle:>14d")
        assert "stalled but reporting" in idle
        assert "never worked, old" in idle  # no work: idle since creation
        # Positive controls: recent work and a new issue are not idle.
        assert "worked today" not in idle
        assert "never worked, new" not in idle
        recent = _titles(c, "idle:<14d")
        assert "worked today" in recent and "never worked, new" in recent
        assert "stalled but reporting" not in recent
        assert _count(c, "idle:>14d") == 2
        # Negation wraps the positive form.
        assert set(_titles(c, "-idle:>14d")) == set(recent)
        assert newborn["id"]


def test_age_measures_from_creation(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        old = _issue(c, "old")
        _make_old(db_file, old["id"], days=90)
        c.post(f"/issues/{old['id']}/comments", json={"body": "fresh"}, headers=H1)
        _issue(c, "new")
        # Fresh work does not make an old issue young.
        assert _titles(c, "age:>60d") == ["old"]
        assert _titles(c, "age:<60d") == ["new"]


def test_sort_idle_desc_puts_the_longest_idle_first(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        for title, days in (("ten", 10), ("forty", 40), ("twenty", 20)):
            issue = _issue(c, title)
            c.post(f"/issues/{issue['id']}/comments", json={"body": "w"}, headers=H1)
            _make_old(db_file, issue["id"], days=days)
        assert _titles(c, "sort:idle-desc") == ["forty", "twenty", "ten"]
        # Positive control: the other orders are unchanged (newest first here).
        assert _titles(c, "sort:id-desc") == ["twenty", "forty", "ten"]


# ── claimed follows the lease clock ─────────────────────────────────────────


def test_is_unclaimed_excludes_an_issue_with_a_live_lease(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        held = _issue(c, "held")
        _claim(c, agent, held["id"])
        lapsed = _issue(c, "lapsed")
        _claim(c, agent, lapsed["id"])
        _sql(
            db_file,
            "UPDATE issue_leases SET expires_at = ? WHERE issue_id = ?",
            (_ago(hours=1), lapsed["id"]),
        )
        _issue(c, "free")

        assert "held" not in _titles(c, "is:unclaimed")
        assert _titles(c, "is:claimed") == ["held"]
        # Positive controls: an expired lease and no lease both read as unclaimed.
        assert set(_titles(c, "is:unclaimed")) == {"lapsed", "free"}
        assert set(_titles(c, "-is:claimed")) == {"lapsed", "free"}
        # Reading changes no lease (K1: shown, never cleared).
        conn = db.connect(db_file)
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM issue_leases").fetchone()["n"] == 2
        )
        conn.close()


# ── visibility: rows, counts and the browser ────────────────────────────────


def _private_idle_setup(c, db_file):
    outsider = c.post(
        "/users",
        json={"email": "out@e.com", "name": "Out", "password": "pw"},
        headers=H1,
    ).json()
    project = c.post(
        "/projects", json={"name": "Private", "key": "PVT"}, headers=H1
    ).json()
    hidden = _issue(c, "hidden idle", project_id=project["id"])
    public = _issue(c, "public idle")
    for issue in (hidden, public):
        _make_old(db_file, issue["id"], days=30)
    c.put(
        f"/projects/{project['id']}/visibility",
        json={"visibility": "private"},
        headers=H1,
    )
    return outsider


def test_a_hidden_projects_items_stay_hidden_in_results_counts_and_find(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        outsider = _private_idle_setup(c, db_file)
        theirs = {"X-Athena-Actor": str(outsider["id"])}

        assert _titles(c, "idle:>14d", headers=theirs) == ["public idle"]
        assert _count(c, "idle:>14d", headers=theirs) == 1
        assert _count(c, "is:unclaimed sort:idle-desc", headers=theirs) == 1
        # Positive control: the admin sees both, in rows and in the count.
        assert set(_titles(c, "idle:>14d")) == {"hidden idle", "public idle"}
        assert _count(c, "idle:>14d") == 2

        _login(c, "out@e.com")
        page = c.get("/find", params={"q": "idle:>14d"}).text
        assert "public idle" in page and "hidden idle" not in page
        _login(c)
        page = c.get("/find", params={"q": "idle:>14d"}).text
        assert "public idle" in page and "hidden idle" in page


def test_the_idle_read_is_indexed_per_issue_not_a_scan_of_the_trail(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
    conn = db.connect(db_file)
    where, params, order_by = issue_query.compile_query(
        conn,
        work_query.parse("idle:>14d sort:idle-desc"),
        actor={"id": 1, "role": "admin"},
        visible_project_ids=None,
    )
    plan = " | ".join(
        row["detail"]
        for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT i.id FROM issues i{issue_query._JOIN} "
            f"WHERE {where} ORDER BY {order_by}",
            params,
        )
    )
    conn.close()
    assert "idx_activity_target" in plan, plan
    # Positive control: the activity table is never scanned whole.
    assert "SCAN activity" not in plan and "SCAN wa" not in plan, plan
    assert (
        work_progress.SUBSTANTIVE_WORK_VERBS
    )  # the same verb list as the progress view


# ── the browser search speaks the grammar ───────────────────────────────────


def test_find_accepts_atoms_and_a_filter_only_browse(tmp_path):
    app, db_file = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        old = _issue(c, "dusty task")
        _make_old(db_file, old["id"], days=30)
        _issue(c, "shiny task")
        doing = _issue(c, "doing task")
        _sql(
            db_file,
            "UPDATE issues SET status = 'in_progress' WHERE id = ?",
            (doing["id"],),
        )
        _login(c)

        page = c.get("/find", params={"q": "idle:>14d"})
        assert page.status_code == 200
        assert "dusty task" in page.text and "shiny task" not in page.text
        # Atoms plus free text: both apply.
        assert "dusty task" in c.get("/find", params={"q": "idle:>14d dusty"}).text
        assert "dusty task" not in c.get("/find", params={"q": "idle:>14d shiny"}).text

        # A filter-only browse: empty text box, one dropdown set.
        browse = c.get("/find", params={"q": "", "status": "in_progress"}).text
        assert "doing task" in browse and "shiny task" not in browse
        # Positive control: a fully blank form still shows the prompt, not every issue.
        blank = c.get("/find").text
        assert "shiny task" not in blank and "Type a query" in blank


def test_find_shows_the_compile_error_on_a_typo(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _issue(c, "any task")
        _login(c)
        refused = c.get("/find", params={"q": "is:claimd"})
        assert refused.status_code == 422
        text = " ".join(refused.text.split())
        assert "is:claimd" in text and "is:claimed" in text  # names it, lists options
        assert "any task" not in text  # never an answer
        # Positive control: plain text search still works as before.
        assert "any task" in c.get("/find", params={"q": "any"}).text


def test_find_refuses_atoms_combined_with_dropdown_filters(tmp_path):
    # The same rule as GET /issues: a query and the structured filters are not
    # silently merged.
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _issue(c, "any task")
        _login(c)
        refused = c.get("/find", params={"q": "idle:>1d", "status": "open"})
        assert refused.status_code == 422
        assert "any task" not in refused.text
        # Positive control: free text with a dropdown keeps the ranked filtered mode.
        kept = c.get("/find", params={"q": "any", "status": "open"})
        assert kept.status_code == 200 and "any task" in kept.text
