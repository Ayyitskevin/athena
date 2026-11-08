"""A per-project Definition of Done: a labelled Mentor page, shown before claiming, reported against.

The checklist is an ordinary page carrying the label ``definition-of-done:<PROJECT KEY>``. It is
shown in the work context, and a completion report is annotated "N of M checklist items reported"
by matching each result's subject to the checklist lines exactly. It never refuses anything.
"""

import pytest
from fastapi.testclient import TestClient

from athena.aegis import definition_of_done
from athena.core import db
from athena.main import create_app

H1 = {"X-Athena-Actor": "1", "X-Athena-Run": "dod-test"}
BODY = """# Done means

- [ ] Tests pass
- [x] Docs updated
* Reviewed by a second seat
Some prose that is not an item.
- Rollback noted
"""


def _client(tmp_path):
    client = TestClient(create_app(tmp_path / "dod.sqlite"))
    client.db_path = tmp_path / "dod.sqlite"
    client.__enter__()
    client.post(
        "/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"}
    )
    return client


def _project(client, key="OPS", name="Ops"):
    return client.post(
        "/projects", json={"name": name, "key": key}, headers=H1
    ).json()["id"]


def _space(client, key="DOC"):
    return client.post("/spaces", json={"key": key, "name": key}, headers=H1).json()


def _checklist_page(client, key="OPS", body=BODY, space=None, title="Definition of done"):
    space = space or _space(client, f"S{key}")
    page = client.post(
        "/pages",
        json={"space_id": space["id"], "title": title, "body": body},
        headers=H1,
    )
    assert page.status_code == 201, page.text
    page = page.json()
    label = client.post(
        "/labels", json={"name": f"definition-of-done:{key}"}, headers=H1
    )
    label = label.json() if label.status_code == 201 else None
    if label is None:
        label = next(
            lb
            for lb in client.get("/labels", headers=H1).json()
            if lb["name"].lower() == f"definition-of-done:{key}".lower()
        )
    attached = client.post(
        f"/pages/{page['id']}/labels", json={"label_id": label["id"]}, headers=H1
    )
    assert attached.status_code == 201, attached.text
    return page


def _issue(client, project_id, title="Work"):
    response = client.post(
        "/issues", json={"title": title, "project_id": project_id}, headers=H1
    )
    assert response.status_code == 201, response.text
    return response.json()


def _complete(client, issue, **report):
    etag = client.get(f"/issues/{issue['id']}", headers=H1).headers["etag"]
    claimed = client.post(
        f"/issues/{issue['id']}/claim", json={}, headers={**H1, "If-Match": etag}
    )
    assert claimed.status_code == 201, claimed.text
    done = client.post(
        f"/issues/{issue['id']}/complete",
        json={"generation": claimed.json()["generation"], **report},
        headers=H1,
    )
    assert done.status_code == 200, done.text
    return done


def _result(condition, state="met", subject=None):
    item = {"condition": condition, "state": state, "note": ""}
    if subject is not None:
        item["subject"] = subject
    return item


def _completed_item(client, issue):
    history = client.get(f"/issues/{issue['id']}/history", headers=H1).json()
    return next(i for i in history["items"] if i["state"] == "claim_completed")


def _context(client, issue, headers=H1):
    return client.get(f"/issues/{issue['id']}/work-context", headers=headers)


# --- the checklist itself ----------------------------------------------------------------


def test_the_page_is_parsed_into_checklist_items(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        page = _checklist_page(client)
        conn = db.connect(client.db_path)
        try:
            found = definition_of_done.checklist(conn, pid, actor=None)
        finally:
            conn.close()
        assert found["page_id"] == page["id"] and found["title"] == "Definition of done"
        assert found["items"] == [
            "Tests pass",
            "Docs updated",
            "Reviewed by a second seat",
            "Rollback noted",
        ]
        assert found["label"] == "definition-of-done:OPS"
    finally:
        client.__exit__(None, None, None)


def test_items_are_capped_and_deduplicated(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        body = "\n".join(f"- item {n}" for n in range(40)) + "\n- item 1\n- ITEM  2\n"
        _checklist_page(client, body=body)
        conn = db.connect(client.db_path)
        try:
            found = definition_of_done.checklist(conn, pid, actor=None)
        finally:
            conn.close()
        assert len(found["items"]) == definition_of_done.MAX_ITEMS == 30
        assert found["truncated"] is True
        assert found["items"][:2] == ["item 0", "item 1"]
    finally:
        client.__exit__(None, None, None)


def test_only_this_projects_label_counts_and_archived_pages_are_ignored(tmp_path):
    client = _client(tmp_path)
    try:
        ops = _project(client, "OPS")
        web = _project(client, "WEB", "Web")
        _checklist_page(client, "WEB", body="- web only", title="Web DoD")
        conn = db.connect(client.db_path)
        try:
            assert definition_of_done.checklist(conn, ops, actor=None) is None
            found = definition_of_done.checklist(conn, web, actor=None)
            assert found["items"] == ["web only"]  # positive control
            # A label with a different case still matches (labels are case-insensitive).
            conn.execute("UPDATE labels SET name = 'Definition-Of-Done:web'")
            conn.commit()
            assert definition_of_done.checklist(conn, web, actor=None) is not None
            conn.execute("UPDATE pages SET archived_at = datetime('now')")
            conn.commit()
            assert definition_of_done.checklist(conn, web, actor=None) is None
        finally:
            conn.close()
        assert ops
    finally:
        client.__exit__(None, None, None)


def test_with_two_labelled_pages_the_oldest_is_the_checklist(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        first = _checklist_page(client, body="- first", title="One")
        _checklist_page(client, body="- second", title="Two", space=_space(client, "TWO"))
        conn = db.connect(client.db_path)
        try:
            found = definition_of_done.checklist(conn, pid, actor=None)
        finally:
            conn.close()
        assert found["page_id"] == first["id"] and found["items"] == ["first"]
    finally:
        client.__exit__(None, None, None)


# --- the work context --------------------------------------------------------------------


def test_the_checklist_appears_in_the_work_context_before_any_claim(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        issue = _issue(client, pid)
        before = _context(client, issue).json()
        assert "definition_of_done" not in before  # positive control: nothing labelled yet
        page = _checklist_page(client)
        context = _context(client, issue).json()
        dod = context["definition_of_done"]
        assert dod["page_id"] == page["id"]
        assert dod["items"][0] == "Tests pass" and len(dod["items"]) == 4
        assert "advisory" in dod["note"] and "not enforced" in dod["note"]
        assert context["recent_activity"]["items"] == [
            i for i in context["recent_activity"]["items"] if "checklist_coverage" not in i
        ]
    finally:
        client.__exit__(None, None, None)


def test_a_project_without_a_checklist_is_unchanged(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        other = _project(client, "WEB", "Web")
        _checklist_page(client, "WEB")  # a different project's checklist exists
        issue = _issue(client, pid)
        _complete(client, issue, results=[_result("Tests pass", subject=["Tests pass"])])
        context = _context(client, issue).json()
        assert "definition_of_done" not in context
        assert all("checklist_coverage" not in i for i in context["recent_activity"]["items"])
        assert "checklist_coverage" not in _completed_item(client, issue)
        page = client.get(f"/aegis/issues/{issue['id']}/history", headers=H1)
        assert "checklist items reported" not in page.text
        # Positive control: the project that does have one is annotated.
        web_issue = _issue(client, other)
        _complete(client, web_issue, results=[_result("x", subject=["web only"])])
        assert "checklist_coverage" in _completed_item(client, web_issue)
    finally:
        client.__exit__(None, None, None)


# --- the coverage annotation -------------------------------------------------------------


def test_coverage_counts_exact_matches_of_result_subjects(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _checklist_page(client)
        issue = _issue(client, pid)
        _complete(
            client,
            issue,
            results=[
                _result("unit", "met", ["tests pass"]),  # case differs: still the item
                _result("docs", "not_met", ["Docs   updated", "something else"]),
                _result("near miss", "met", ["Tests pass!"]),  # not the item
                _result("no subject", "met"),  # names nothing
                _result("again", "met", ["Tests pass"]),  # same item twice: counted once
            ],
        )
        coverage = _completed_item(client, issue)["checklist_coverage"]
        assert coverage["reported"] == 2 and coverage["total"] == 4
        assert coverage["summary"] == "2 of 4 checklist items reported"
        assert coverage["items"] == [
            {"item": "Tests pass", "reported": True},
            {"item": "Docs updated", "reported": True},
            {"item": "Reviewed by a second seat", "reported": False},
            {"item": "Rollback noted", "reported": False},
        ]
    finally:
        client.__exit__(None, None, None)


def test_every_item_reported_reads_n_of_n_and_none_reads_zero(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _checklist_page(client, body="- a\n- b")
        full = _issue(client, pid, "full")
        _complete(client, full, results=[_result("c", subject=["a", "b"])])
        assert _completed_item(client, full)["checklist_coverage"]["summary"] == (
            "2 of 2 checklist items reported"
        )
        none = _issue(client, pid, "none")
        _complete(client, none, unfinished=["everything"])
        assert _completed_item(client, none)["checklist_coverage"]["summary"] == (
            "0 of 2 checklist items reported"
        )
    finally:
        client.__exit__(None, None, None)


def test_the_annotation_shows_in_the_history_page_the_work_context_and_the_html(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _checklist_page(client)
        issue = _issue(client, pid)
        _complete(client, issue, results=[_result("u", subject=["Tests pass"])])
        context = _context(client, issue).json()
        completed = next(
            i
            for i in context["recent_activity"]["items"]
            if i["verb"] == "claim_completed"
        )
        assert completed["checklist_coverage"]["summary"] == "1 of 4 checklist items reported"
        for path in (
            f"/aegis/issues/{issue['id']}/history",
            f"/aegis/issues/{issue['id']}/work-context",
        ):
            page = client.get(path, headers=H1)
            assert page.status_code == 200, path
            assert "1 of 4 checklist items reported" in page.text, path
    finally:
        client.__exit__(None, None, None)


def test_an_old_report_in_a_project_without_a_checklist_renders_unchanged(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        issue = _issue(client, pid)
        _complete(client, issue, results=[_result("old shape")], unfinished=["docs"])
        item = _completed_item(client, issue)
        assert set(item) >= {"reported_completion"} and "checklist_coverage" not in item
        page = client.get(f"/aegis/issues/{issue['id']}/history", headers=H1).text
        assert "Reported completion (advisory)" in page
        assert "checklist" not in page.lower()
    finally:
        client.__exit__(None, None, None)


# --- no enforcement ----------------------------------------------------------------------


def test_an_incomplete_report_is_never_refused(tmp_path):
    client = _client(tmp_path)
    try:
        pid = _project(client)
        _checklist_page(client)
        issue = _issue(client, pid)
        done = _complete(client, issue)  # no report at all
        assert done.json()["released"] is True
        bare = _issue(client, pid, "second")
        assert _complete(client, bare, unfinished=["all of it"]).status_code == 200
    finally:
        client.__exit__(None, None, None)


# --- hidden containers -------------------------------------------------------------------


def test_a_checklist_in_a_space_the_viewer_cannot_see_is_never_shown(tmp_path):
    client = _client(tmp_path)
    try:
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw", "role": "member"},
            headers=H1,
        ).json()
        pid = _project(client)
        space = _space(client, "PRV")
        page = _checklist_page(client, space=space)
        issue = _issue(client, pid)
        _complete(client, issue, results=[_result("u", subject=["Tests pass"])])
        conn = db.connect(client.db_path)
        try:
            admin = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
            outsider = dict(
                conn.execute("SELECT * FROM users WHERE id = ?", (member["id"],)).fetchone()
            )
            # Positive control: while the space is public both actors see it.
            assert definition_of_done.checklist(conn, pid, actor=outsider)["page_id"] == page["id"]
            conn.execute("UPDATE spaces SET visibility='private' WHERE id=?", (space["id"],))
            conn.commit()
            assert definition_of_done.checklist(conn, pid, actor=admin) is not None
            assert definition_of_done.checklist(conn, pid, actor=outsider) is None
        finally:
            conn.close()
        as_member = {"X-Athena-Actor": str(member["id"])}
        context = _context(client, issue, headers=as_member).json()
        assert "definition_of_done" not in context
        assert "definition_of_done" in _context(client, issue).json()
        # No annotation either: the count would leak the hidden page's length.
        assert all("checklist_coverage" not in i for i in context["recent_activity"]["items"])
    finally:
        client.__exit__(None, None, None)


def test_the_checklist_of_a_private_project_is_never_shown(tmp_path):
    client = _client(tmp_path)
    try:
        member = client.post(
            "/users",
            json={"email": "m@e.com", "name": "M", "password": "pw", "role": "member"},
            headers=H1,
        ).json()
        pid = _project(client)
        _checklist_page(client)
        issue = _issue(client, pid)
        conn = db.connect(client.db_path)
        try:
            outsider = dict(
                conn.execute("SELECT * FROM users WHERE id = ?", (member["id"],)).fetchone()
            )
            assert definition_of_done.checklist(conn, pid, actor=outsider) is not None
            conn.execute("UPDATE projects SET visibility='private' WHERE id=?", (pid,))
            conn.commit()
            assert definition_of_done.checklist(conn, pid, actor=outsider) is None
        finally:
            conn.close()
        hidden = _context(client, issue, headers={"X-Athena-Actor": str(member["id"])})
        assert hidden.status_code == 404
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("item", ["Tests pass", "  tests   PASS "])
def test_matching_is_exact_after_case_and_space_folding(item):
    assert definition_of_done.normalize(item) == "tests pass"
    assert definition_of_done.normalize("Tests pass!") != "tests pass"
