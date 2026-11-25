"""List, search and single-issue reads name who wrote the text; a bulk row
refused for its status lists the statuses offered (G10).

WHY: ``authored_by`` (A10) told an agent whose text it was reading in the
work-context packet, the desk, comments and pages, but not in ``GET /issues``
(rows with whole bodies), ``GET /issues/{ref}``, ``GET /issues/{id}/children``
or either search, so an agent listing work read another account's prose with
nothing saying it was data. And ``POST /issues/bulk`` refused a status the
project does not offer with the sentence alone, where the single write lists
the statuses it does offer. Other refused rows keep their legacy shape.
"""

from __future__ import annotations

import random

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, add_user, as_user, bearer, bootstrap, make_app, onboard_agent

from athena.core import activity, authorship, db


@pytest.fixture
def world(tmp_path):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        agent = onboard_agent(client, scopes=("read", "issue:write", "docs:write"))
        h = bearer(agent)
        parent = client.post(
            "/issues", json={"title": "zebra parent", "body": "zebra text"}, headers=h
        ).json()
        child = client.post(
            "/issues",
            json={"title": "zebra child", "body": "zebra", "parent_id": parent["id"]},
            headers=h,
        ).json()
        space = client.post(
            "/spaces", json={"key": "SP", "name": "S"}, headers=ADMIN
        ).json()
        page = client.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "zebra page", "body": "zebra page body"},
            headers=h,
        ).json()
        client.post(
            f"/issues/{parent['id']}/comments",
            json={"body": "zebra from the operator"},
            headers=ADMIN,
        )
        yield {
            "client": client,
            "db": db_file,
            "agent": agent["user"],
            "h": h,
            "parent": parent,
            "child": child,
            "page": page,
        }


def _by(agent_or_admin: dict | int) -> dict:
    if isinstance(agent_or_admin, int):
        return {"id": agent_or_admin, "is_agent": False}
    return {"id": agent_or_admin["id"], "is_agent": True}


def test_the_issue_reads_name_the_current_texts_author(world):
    client, h, agent = world["client"], world["h"], world["agent"]
    parent, child = world["parent"], world["child"]

    rows = client.get("/issues", headers=h).json()["issues"]
    assert {row["id"]: row["authored_by"] for row in rows} == {
        parent["id"]: _by(agent),
        child["id"]: _by(agent),
    }
    one = client.get(f"/issues/{parent['id']}", headers=h).json()
    assert one["authored_by"] == _by(agent)
    (kid,) = client.get(f"/issues/{parent['id']}/children", headers=h).json()
    assert kid["authored_by"] == _by(agent)

    # The operator edits the text: the current text is now the operator's.
    etag = client.get(f"/issues/{parent['id']}", headers=ADMIN).headers["ETag"]
    edited = client.patch(
        f"/issues/{parent['id']}",
        json={"body": "rewritten by the operator"},
        headers={**ADMIN, "If-Match": etag},
    )
    assert edited.status_code == 200, edited.text
    assert client.get(f"/issues/{parent['id']}", headers=h).json()[
        "authored_by"
    ] == _by(1)
    # The structured query path (q=) names it too.
    queried = client.get("/issues", params={"q": "is:open"}, headers=h).json()
    assert {row["id"]: row["authored_by"] for row in queried["issues"]}[
        parent["id"]
    ] == _by(1)


def test_a_write_answer_carries_no_author_and_its_etag_matches_the_read(world):
    # The author is a reader-dependent fact: it stays out of the representation
    # the ETag is computed from, so a read and a write agree on the tag.
    client = world["client"]
    issue = world["parent"]
    read = client.get(f"/issues/{issue['id']}", headers=ADMIN)
    patched = client.patch(
        f"/issues/{issue['id']}",
        json={"priority": "high"},
        headers={**ADMIN, "If-Match": read.headers["ETag"]},
    )
    assert patched.status_code == 200, patched.text
    assert "authored_by" not in patched.json()
    again = client.get(f"/issues/{issue['id']}", headers=ADMIN)
    assert again.headers["ETag"] == patched.headers["ETag"]


def test_search_hits_name_the_author_of_the_snippets_text(world):
    client, h, agent = world["client"], world["h"], world["agent"]
    hits = client.get("/search", params={"q": "zebra"}, headers=h).json()
    by = {(hit["kind"], hit["source_id"]): hit["authored_by"] for hit in hits}
    assert by[("issue", world["parent"]["id"])] == _by(agent)
    assert by[("page", world["page"]["id"])] == _by(agent)
    # A comment hit borrows its issue's title but names the comment's author.
    (comment_author,) = [a for (k, _), a in by.items() if k == "issue_comment"]
    assert comment_author == _by(1)

    issue_hits = client.get("/issues/search", params={"q": "zebra"}, headers=h).json()
    assert {hit["source_id"]: hit["authored_by"] for hit in issue_hits} == {
        world["parent"]["id"]: _by(agent),
        world["child"]["id"]: _by(agent),
    }


def test_a_bulk_row_refused_for_its_status_lists_the_statuses_offered(world):
    client = world["client"]
    issue = world["parent"]
    single = client.patch(
        f"/issues/{issue['id']}",
        json={"status": "bogus"},
        headers={
            **ADMIN,
            "If-Match": client.get(f"/issues/{issue['id']}", headers=ADMIN).headers[
                "ETag"
            ],
        },
    )
    assert single.status_code == 422
    bulk = client.post(
        "/issues/bulk", json={"ids": [issue["id"]], "status": "bogus"}, headers=ADMIN
    )
    assert bulk.status_code == 200
    (row,) = bulk.json()["results"]
    assert row["ok"] is False
    assert row["error"] == single.json()["detail"]
    assert row["valid_statuses"] == single.json()["valid_statuses"]
    assert "in_progress" in row["valid_statuses"]


def test_other_refused_bulk_rows_keep_their_legacy_shape(world):
    # Only a status refusal names statuses; other refused rows are unchanged.
    client = world["client"]
    member = add_user(client, "m@e.com", name="Mel")
    bulk = client.post(
        "/issues/bulk",
        json={"ids": [world["parent"]["id"], 999_999], "priority": "high"},
        headers=as_user(member["id"]),
    )
    rows = {row["id"]: row for row in bulk.json()["results"]}
    assert rows[999_999] == {"id": 999_999, "ok": False, "error": "no such issue"}
    assert all("valid_statuses" not in row for row in rows.values())


def _per_issue_reference(conn, ids, actor):
    """The lookup as it was before it was batched: one timeline read per issue
    and verb, then the reader's gate on the newest event."""
    result = {}
    for target_id in sorted(set(ids)):
        result[target_id] = None
        candidates = [
            row
            for verb in authorship.ISSUE_TEXT_VERBS
            for row in activity.list_activity(
                conn, target_kind="issue", target_id=target_id, verb=verb, limit=1
            )
        ]
        if not candidates:
            continue
        newest = max(candidates, key=lambda row: int(row["id"]))
        if newest["imported_at"] is not None:
            continue
        if activity.get_visible_activity(conn, int(newest["id"]), actor) is None:
            continue
        result[target_id] = authorship.author(conn, newest["actor_id"])
    return result


def test_the_batched_lookup_answers_as_the_per_issue_one(tmp_path):
    """Many issues, edits by several accounts, private projects, imported
    newest events and issues with no event at all, read as an admin, a member
    and nobody: the batch and the per-issue reading agree on every one."""
    app, db_file = make_app(tmp_path)
    rng = random.Random(10)
    with TestClient(app) as client:
        bootstrap(client)
        member = add_user(client, "m@e.com", name="Mel")
        agent = onboard_agent(client)
        private = client.post(
            "/projects",
            json={"name": "Hidden", "key": "HID", "visibility": "private"},
            headers=ADMIN,
        ).json()
        writers = [ADMIN, bearer(agent)]
        ids = []
        for n in range(60):
            in_private = rng.random() < 0.3
            body = {"title": f"issue {n}"}
            if in_private:
                body["project_id"] = private["id"]
            # The agent writes some of the public ones; it may edit what it
            # created, so its edits land too.
            creator = ADMIN if in_private else rng.choice(writers)
            created = client.post("/issues", json=body, headers=creator).json()
            ids.append(created["id"])
            for _ in range(rng.randrange(3)):
                who = rng.choice(writers)
                etag = client.get(f"/issues/{created['id']}", headers=ADMIN).headers[
                    "ETag"
                ]
                client.patch(
                    f"/issues/{created['id']}",
                    json={"body": f"edit {rng.random()}"},
                    headers={**who, "If-Match": etag},
                )
        conn = db.connect(db_file)
        try:
            # Some issues' newest text event arrived in a bundle; some issues
            # have no text event at all.
            for issue_id in rng.sample(ids, 8):
                conn.execute(
                    "UPDATE activity SET imported_at = '2026-01-01 00:00:00' "
                    "WHERE id = (SELECT MAX(id) FROM activity WHERE target_kind = "
                    "'issue' AND target_id = ? AND verb IN ('created', 'issue_edited'))",
                    (issue_id,),
                )
            for issue_id in rng.sample(ids, 5):
                # Rows are referenced elsewhere, so they are renamed rather
                # than deleted: no event among the text verbs remains.
                conn.execute(
                    "UPDATE activity SET verb = 'noted' WHERE target_kind = 'issue' "
                    "AND target_id = ? AND verb IN ('created', 'issue_edited')",
                    (issue_id,),
                )
            conn.commit()
        except Exception:
            conn.close()
            raise
        try:
            from athena.core import users

            readers = [
                users.get_user(conn, 1),
                users.get_user(conn, member["id"]),
                None,
            ]
            for reader in readers:
                batched = authorship.issue_text_authors(
                    conn, ids + [999_999], actor=reader
                )
                assert batched == _per_issue_reference(conn, ids + [999_999], reader)
            # The two lookups are not trivially alike: some answers are null,
            # some name the agent, some the operator.
            values = list(
                authorship.issue_text_authors(conn, ids, actor=readers[0]).values()
            )
            assert None in values
            assert {"id": agent["user"]["id"], "is_agent": True} in values
            assert {"id": 1, "is_agent": False} in values
        finally:
            conn.close()
