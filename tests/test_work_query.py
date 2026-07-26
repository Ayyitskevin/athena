"""What the query atoms MEAN, against real issues.

The grammar is pinned in test_work_query_grammar.py without a database. This file
is the other half: `is:open` against a project whose done state is called
`shipped`, `label:` against the join, `@me` against a token's actor, and — the
one that matters most — visibility, because a query is a new way to ask for rows
and a new way to ask is a new way to leak.

The parity tests exist because the guide requires MCP to reach the same behavior
through REST. A second query implementation behind MCP would be the exact drift
the import contract and the one-command-owner rule were written to prevent.
"""

import pytest
from fastapi.testclient import TestClient

from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="query.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _user(client, email, *, role="member"):
    return client.post(
        "/users",
        json={
            "email": email,
            "name": email.split("@")[0],
            "password": "pw",
            "role": role,
        },
        headers=H1,
    ).json()


def _agent(client, email="sol@e.com", scopes=("read", "issue:write")):
    return client.post(
        "/users/onboard_agent",
        json={"email": email, "name": "Sol", "scopes": list(scopes)},
        headers=H1,
    ).json()


def _bearer(onboarded):
    return {"Authorization": f"Bearer {onboarded['token']['token']}"}


def _project(client, name, key, headers=H1):
    return client.post(
        "/projects", json={"name": name, "key": key}, headers=headers
    ).json()


def _issue(client, title, *, project_id=None, headers=H1, **fields):
    payload = {"title": title, **fields}
    if project_id is not None:
        payload["project_id"] = project_id
    return client.post("/issues", json=payload, headers=headers).json()


def _q(client, query, headers=H1, **params):
    r = client.get("/issues", params={"q": query, **params}, headers=headers)
    return r


def _titles(client, query, headers=H1, **params):
    r = _q(client, query, headers=headers, **params)
    assert r.status_code == 200, r.text
    return [row["title"] for row in r.json()]


# --- the atoms mean what they say ------------------------------------------


def test_is_open_and_is_closed_follow_the_project_status_category(tmp_path):
    """The payoff of routing through statuses.category_sql: a project whose done
    state is named 'shipped' is closed, and nothing hardcodes the word 'done'."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c, "Ship", "SHP")
        c.post(
            f"/projects/{project['id']}/statuses",
            json={"name": "shipped", "category": "done"},
            headers=H1,
        )
        todo = _issue(c, "still going", project_id=project["id"])
        done = _issue(c, "finished", project_id=project["id"])
        c.patch(f"/issues/{done['id']}", json={"status": "shipped"}, headers=H1)

        assert _titles(c, "is:open") == ["still going"]
        assert _titles(c, "is:closed") == ["finished"]
        # And the negations are the exact complements.
        assert _titles(c, "-is:closed") == ["still going"]
        assert _titles(c, "-is:open") == ["finished"]
        assert todo["id"] != done["id"]


def test_labels_and_negated_labels(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        keep = _issue(c, "keep")
        drop = _issue(c, "drop")
        both = _issue(c, "both")
        label = c.post("/labels", json={"name": "infra"}, headers=H1).json()
        noise = c.post("/labels", json={"name": "noise"}, headers=H1).json()
        for issue in (keep, both):
            c.post(
                f"/issues/{issue['id']}/labels",
                json={"label_id": label["id"]},
                headers=H1,
            )
        for issue in (drop, both):
            c.post(
                f"/issues/{issue['id']}/labels",
                json={"label_id": noise["id"]},
                headers=H1,
            )

        assert sorted(_titles(c, "label:infra")) == ["both", "keep"]
        assert _titles(c, "label:infra -label:noise") == ["keep"]
        # Repeated labels AND together, matching GitHub.
        assert _titles(c, "label:infra label:noise") == ["both"]
        # Label matching is case-insensitive, like the labels table's collation.
        assert sorted(_titles(c, "label:INFRA")) == ["both", "keep"]


def test_project_resolves_by_key_and_by_id_and_none_is_the_backlog(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = _project(c, "Athena", "ATH")
        _issue(c, "in project", project_id=project["id"])
        _issue(c, "in backlog")

        assert _titles(c, "project:ATH") == ["in project"]
        assert _titles(c, "project:ath") == ["in project"]  # keys are NOCASE
        assert _titles(c, f"project:{project['id']}") == ["in project"]
        assert _titles(c, "project:none") == ["in backlog"]
        # An unknown key matches nothing rather than erroring: a project you
        # cannot see must be indistinguishable from one that does not exist.
        assert _titles(c, "project:NOPE") == []


def test_assignee_me_resolves_to_the_caller_and_refuses_anonymously(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        mine = _issue(c, "mine")
        theirs = _issue(c, "theirs")
        c.put(f"/issues/{mine['id']}/assignee", json={"assignee_id": 1}, headers=H1)
        c.put(
            f"/issues/{theirs['id']}/assignee",
            json={"assignee_id": bob["id"]},
            headers=H1,
        )

        assert _titles(c, "assignee:@me") == ["mine"]
        assert _titles(
            c, "assignee:@me", headers={"X-Athena-Actor": str(bob["id"])}
        ) == ["theirs"]
        assert _titles(c, f"assignee:{bob['id']}") == ["theirs"]
        assert _titles(c, "is:unassigned") == []
        assert _titles(c, "assignee:none") == []

        # Anonymous @me is a refusal, not an empty list: "you have no work" is a
        # different claim from "I don't know who you are".
        anon = c.get("/issues", params={"q": "assignee:@me"})
        assert anon.status_code == 422
        assert anon.json()["detail"]["atom"] == "assignee:@me"


def test_has_predicates_are_structural(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        parent = _issue(c, "parent")
        child = _issue(c, "child")
        blocked = _issue(c, "blocked")
        blocker = _issue(c, "blocker")
        c.put(
            f"/issues/{child['id']}/parent",
            json={"parent_id": parent["id"]},
            headers=H1,
        )
        linked = c.post(
            f"/issues/{blocked['id']}/links",
            json={"target_ref": str(blocker["id"]), "relation": "blocked_by"},
            headers=H1,
        )
        assert linked.status_code == 201, linked.text

        assert _titles(c, "has:parent") == ["child"]
        assert _titles(c, "has:children") == ["parent"]
        assert _titles(c, "has:blockers") == ["blocked"]
        assert "blocked" not in _titles(c, "-has:blockers")


def test_text_terms_match_title_and_body(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _issue(c, "payment retry loop")
        _issue(c, "unrelated", body="the payment path is fine")
        _issue(c, "nothing to see")

        assert sorted(_titles(c, "payment")) == ["payment retry loop", "unrelated"]
        assert _titles(c, '"retry loop"') == ["payment retry loop"]
        assert _titles(c, "payment is:open label:absent") == []


def test_archived_issues_are_excluded_unless_asked_for(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        live = _issue(c, "live")
        gone = _issue(c, "gone")
        c.post(f"/issues/{gone['id']}/archive", headers=H1)

        assert _titles(c, "") == ["live"]
        assert _titles(c, "is:archived") == ["gone"]
        assert live["id"] != gone["id"]


def test_sort_orders_priority_by_rank_not_alphabetically(tmp_path):
    """Alphabetical priority would read urgent, medium, low, high — a nonsense
    order that looks plausible enough to ship unnoticed."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        for title, priority in (
            ("lowest", "low"),
            ("middle", "medium"),
            ("highest", "urgent"),
            ("high one", "high"),
        ):
            _issue(c, title, priority=priority)

        assert _titles(c, "sort:priority-desc") == [
            "highest",
            "high one",
            "middle",
            "lowest",
        ]
        assert _titles(c, "sort:priority-asc")[0] == "lowest"


def test_default_sort_is_newest_first_and_id_asc_is_available(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _issue(c, "first")
        _issue(c, "second")
        assert _titles(c, "") == ["second", "first"]
        assert _titles(c, "sort:id-asc") == ["first", "second"]


# --- visibility: a new way to ask is a new way to leak ---------------------


def test_a_query_never_returns_an_issue_the_caller_cannot_see(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        outsider = _user(c, "out@e.com")
        h_out = {"X-Athena-Actor": str(outsider["id"])}

        private = _project(c, "Private", "PRV")
        c.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        _issue(c, "secret work", project_id=private["id"])
        _issue(c, "public work")

        assert sorted(_titles(c, "")) == ["public work", "secret work"]
        assert _titles(c, "", headers=h_out) == ["public work"]
        # Naming the private project directly does not reveal its contents.
        assert _titles(c, "project:PRV", headers=h_out) == []
        assert _titles(c, "secret", headers=h_out) == []
        # ...nor does the count, which is the other half of the leak.
        counted = c.get("/issues/query/count", params={"q": "secret"}, headers=h_out)
        assert counted.json()["matched"] == 0


def test_visibility_is_applied_in_sql_so_a_page_is_not_silently_under_filled(
    tmp_path,
):
    """If rows were fetched then dropped in Python, limit=2 over a mix of visible
    and hidden issues would return fewer than 2 while more matched — under-filling
    the page and leaking the hidden count through the gap."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        outsider = _user(c, "out@e.com")
        h_out = {"X-Athena-Actor": str(outsider["id"])}
        private = _project(c, "Private", "PRV")
        c.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        # Interleave hidden and visible so a naive fetch-then-drop fails.
        for n in range(6):
            _issue(c, f"hidden {n}", project_id=private["id"])
            _issue(c, f"visible {n}")

        page = _titles(c, "sort:id-asc", headers=h_out, limit=2)
        assert len(page) == 2
        assert all(t.startswith("visible") for t in page)


def test_an_anonymous_caller_sees_only_public_work(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        private = _project(c, "Private", "PRV")
        c.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        _issue(c, "secret", project_id=private["id"])
        _issue(c, "open to all")

        anon = c.get("/issues", params={"q": "is:open"})
        assert anon.status_code == 200
        assert [row["title"] for row in anon.json()] == ["open to all"]


# --- refusals reach the transport intact -----------------------------------


def test_an_unknown_field_is_a_422_naming_the_atom(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.get("/issues", params={"q": "asignee:@me"}, headers=H1)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["code"] == "invalid_query"
        assert detail["atom"] == "asignee"


def test_q_and_the_structured_filters_refuse_to_be_combined(tmp_path):
    """Merging them would make `?q=is:open&status=done` return nothing and look
    like missing data; preferring one would ignore what the caller asked for."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.get("/issues", params={"q": "is:open", "status": "done"}, headers=H1)
        assert r.status_code == 422
        assert "status" in r.json()["detail"]


def test_the_structured_filters_still_work_untouched(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _issue(c, "urgent thing", priority="urgent")
        _issue(c, "calm thing", priority="low")
        rows = c.get("/issues", params={"priority": "urgent"}, headers=H1).json()
        assert [r["title"] for r in rows] == ["urgent thing"]


# --- count, help, and paging -----------------------------------------------


def test_count_reports_the_total_behind_a_bounded_page(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        for n in range(7):
            _issue(c, f"issue {n}")
        assert len(_titles(c, "is:open", limit=3)) == 3
        counted = c.get("/issues/query/count", params={"q": "is:open"}, headers=H1)
        assert counted.json() == {"q": "is:open", "matched": 7}


def test_query_help_is_emitted_from_the_parser(tmp_path):
    from athena.core import work_query

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        described = c.get("/issues/query/help", headers=H1).json()
        assert described["fields"]["is"] == list(work_query.IS_VALUES)
        assert described["limits"]["max_terms"] == work_query.MAX_TERMS


# --- saved filters ----------------------------------------------------------


def test_a_saved_query_filter_runs_as_the_runner_not_the_author(tmp_path):
    """`assignee:@me` in a saved filter is the reason to save a query rather than
    a fixed user id: it means whoever runs it."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        bob = _user(c, "bob@e.com")
        h_bob = {"X-Athena-Actor": str(bob["id"])}
        ann_issue = _issue(c, "ann's")
        bob_issue = _issue(c, "bob's")
        c.put(
            f"/issues/{ann_issue['id']}/assignee", json={"assignee_id": 1}, headers=H1
        )
        c.put(
            f"/issues/{bob_issue['id']}/assignee",
            json={"assignee_id": bob["id"]},
            headers=H1,
        )

        created = c.post(
            "/filters",
            json={"name": "Mine", "criteria": {"query": "assignee:@me"}},
            headers=H1,
        )
        assert created.status_code == 201, created.text
        fid = created.json()["id"]
        assert [
            r["title"] for r in c.get(f"/filters/{fid}/issues", headers=H1).json()
        ] == ["ann's"]

        bob_filter_id = c.post(
            "/filters",
            json={"name": "Mine", "criteria": {"query": "assignee:@me"}},
            headers=h_bob,
        ).json()["id"]
        bobs_rows = c.get(f"/filters/{bob_filter_id}/issues", headers=h_bob).json()
        assert [r["title"] for r in bobs_rows] == ["bob's"]


def test_a_saved_filter_cannot_store_an_unparseable_query(tmp_path):
    """Validated at WRITE time: a filter that fails every run would otherwise
    fail closed to an empty list, which is the invented answer the grammar
    exists to prevent."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post(
            "/filters",
            json={"name": "Broken", "criteria": {"query": "asignee:@me"}},
            headers=H1,
        )
        assert r.status_code == 422
        assert "asignee" in r.json()["detail"]


def test_a_saved_filter_is_a_query_or_fields_never_both(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        r = c.post(
            "/filters",
            json={
                "name": "Muddled",
                "criteria": {"query": "is:open", "status": "done"},
            },
            headers=H1,
        )
        assert r.status_code == 422
        assert "status" in r.json()["detail"]


# --- transport parity -------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "is:open",
        "label:infra",
        "project:ATH sort:priority-desc",
        "-label:infra",
        "payment",
    ],
)
def test_mcp_and_rest_return_the_same_ids_for_the_same_query(tmp_path, query):
    """MCP reaches the query through REST — there is no second implementation to
    drift. Same q, same ids, or the parity claim is false."""
    from athena.mcp.client import AthenaClient

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = _agent(c)
        project = _project(c, "Athena", "ATH")
        label = c.post("/labels", json={"name": "infra"}, headers=H1).json()
        for n in range(4):
            issue = _issue(
                c,
                f"payment {n}",
                project_id=project["id"] if n % 2 else None,
                priority="urgent" if n == 0 else "low",
            )
            if n < 2:
                c.post(
                    f"/issues/{issue['id']}/labels",
                    json={"label_id": label["id"]},
                    headers=H1,
                )

        c.headers.update(_bearer(agent))
        rest = [row["id"] for row in c.get("/issues", params={"q": query}).json()]
        client = AthenaClient(client=c)
        assert [row["id"] for row in client.search_work(query)] == rest
        assert client.count_work(query)["matched"] == len(rest)
