"""The actor-visible, bounded work packet for one issue.

These tests treat the packet as an agent-facing contract, not a convenience
aggregate.  In particular, hidden resources must disappear without contributing
to counts, text and collections must stay bounded, and one response must come from
one SQLite snapshot so an agent never receives a torn decision context.
"""

from html import escape

from fastapi.testclient import TestClient

from athena.aegis import work_context
from athena.core import activity, db
from athena.main import create_app
from athena.mcp.client import AthenaClient


H_ADMIN = {"X-Athena-Actor": "1"}
H_OUTSIDER = {"X-Athena-Actor": "2"}


def _bootstrap(client: TestClient) -> None:
    admin = client.post(
        "/users",
        json={
            "email": "admin@example.com",
            "name": "Admin",
            "password": "pw",
        },
    )
    assert admin.status_code == 201
    outsider = client.post(
        "/users",
        json={
            "email": "outsider@example.com",
            "name": "Outsider",
            "password": "pw",
            "role": "member",
        },
        headers=H_ADMIN,
    )
    assert outsider.status_code == 201


def _project(client: TestClient, *, name: str, key: str) -> dict:
    response = client.post(
        "/projects",
        json={"name": name, "key": key},
        headers=H_ADMIN,
    )
    assert response.status_code == 201
    return response.json()


def _issue(
    client: TestClient,
    *,
    title: str,
    body: str = "",
    project_id: int | None = None,
    status: str | None = None,
) -> dict:
    payload = {"title": title, "body": body, "project_id": project_id}
    if status is not None:
        payload["status"] = status
    response = client.post("/issues", json=payload, headers=H_ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def _context(client: TestClient, ref: str | int, *, headers=None) -> tuple[dict, str]:
    response = client.get(f"/issues/{ref}/work-context", headers=headers)
    assert response.status_code == 200, response.text
    return response.json(), response.headers["etag"]


def _assert_strong_etag(value: str) -> None:
    assert value.startswith('"') and value.endswith('"')
    assert not value.startswith("W/")


def _vary(response) -> set[str]:
    return {
        value.strip().lower()
        for value in response.headers.get("vary", "").split(",")
        if value.strip()
    }


def test_rest_numeric_key_mcp_and_web_share_the_public_contract(tmp_path):
    """Every client gets the same packet and no internal attachment handle leaks."""
    db_file = tmp_path / "surfaces.db"
    hostile_title = "<script>alert(1)</script>"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        project = _project(client, name="Context", key="CTX")
        issue = _issue(
            client,
            title=hostile_title,
            body="The complete issue description.",
            project_id=project["id"],
        )
        uploaded = client.post(
            f"/issues/{issue['id']}/attachments",
            files={"file": ("brief.txt", b"agent context", "text/plain")},
            headers=H_ADMIN,
        )
        assert uploaded.status_code == 201

        by_id, etag_by_id = _context(client, issue["id"])
        by_key, etag_by_key = _context(client, issue["key"])
        assert by_key == by_id
        assert etag_by_key == etag_by_id
        _assert_strong_etag(etag_by_id)

        assert by_id["schema"] == "athena.issue_work_context.v1"
        assert by_id["scope"] == "visible_to_request_actor"
        assert by_id["semantics"] == {
            "snapshot": "current_visible_state",
            "does_not_assert": [
                "claimed",
                "ready",
                "unblocked",
                "running",
                "live",
                "replay_safe",
            ],
        }
        assert by_id["issue"]["key"] == "CTX-1"
        _assert_strong_etag(by_id["issue_etag"])
        assert set(by_id["attachments"]["items"][0]) == {
            "id",
            "filename",
            "content_type",
            "byte_size",
            "sha256",
            "uploaded_by",
            "created_at",
        }
        assert "stored_name" not in str(by_id)

        rest = client.get(f"/issues/{issue['id']}/work-context")
        assert rest.headers["cache-control"] == "private, no-store"
        assert {"authorization", "x-athena-actor"} <= _vary(rest)

        # The browser is a rendering of the same projection. Jinja must escape the
        # issue title because this surface is useful only if agent-authored text
        # cannot become executable operator UI.
        web = client.get(f"/aegis/issues/{issue['id']}/work-context")
        assert web.status_code == 200
        assert web.headers["cache-control"] == "private, no-store"
        assert "cookie" in _vary(web)
        assert hostile_title not in web.text
        assert escape(hostile_title) in web.text
        assert by_id["schema"] in web.text
        assert by_id["issue_etag"].replace('"', "&#34;") in web.text

        # MCP deliberately goes through REST. Its only addition is the response
        # validator under _etag, so agents can retain the exact packet validator.
        token = client.post(
            "/tokens", json={"name": "work-context", "scopes": ["admin"]}, headers=H_ADMIN
        ).json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        mcp_packet = AthenaClient(client=client).get_issue_work_context(issue["key"])
        mcp_etag = mcp_packet.pop("_etag")
        assert mcp_packet == by_id
        assert mcp_etag == etag_by_id


def test_hidden_and_missing_roots_are_identical_404s_on_json_and_html(tmp_path):
    """A work-context endpoint must not become a private-project oracle."""
    with TestClient(create_app(tmp_path / "root-privacy.db")) as client:
        _bootstrap(client)
        project = _project(client, name="Secret", key="SEC")
        hidden = _issue(
            client,
            title="Hidden root",
            project_id=project["id"],
        )
        made_private = client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H_ADMIN,
        )
        assert made_private.status_code == 200

        hidden_json = client.get(
            f"/issues/{hidden['id']}/work-context", headers=H_OUTSIDER
        )
        missing_json = client.get(
            "/issues/999999/work-context", headers=H_OUTSIDER
        )
        assert hidden_json.status_code == missing_json.status_code == 404
        assert hidden_json.json() == missing_json.json() == {
            "detail": "no such issue"
        }
        for response in (hidden_json, missing_json):
            assert response.headers["cache-control"] == "private, no-store"
            assert {"authorization", "x-athena-actor"} <= _vary(response)

        hidden_html = client.get(
            f"/aegis/issues/{hidden['id']}/work-context"
        )
        missing_html = client.get("/aegis/issues/999999/work-context")
        assert hidden_html.status_code == missing_html.status_code == 404
        assert hidden_html.text == missing_html.text
        for response in (hidden_html, missing_html):
            assert response.headers["cache-control"] == "private, no-store"
            assert "cookie" in _vary(response)


def test_hidden_nested_resources_are_omitted_and_not_counted_until_granted(
    tmp_path,
):
    """Visible totals are not allowed to disclose invisible work or knowledge."""
    db_file = tmp_path / "nested-privacy.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        root = _issue(client, title="Public root")

        secret_project = _project(client, name="Secret project", key="SEC")
        hidden_issue = _issue(
            client,
            title="Classified issue title",
            body=f"backlink [[issue:{root['id']}]]",
            project_id=secret_project["id"],
        )
        assert client.put(
            f"/issues/{hidden_issue['id']}/parent",
            json={"parent_id": root["id"]},
            headers=H_ADMIN,
        ).status_code == 200
        assert client.post(
            f"/issues/{root['id']}/links",
            json={
                "relation": "blocked_by",
                "target_ref": str(hidden_issue["id"]),
            },
            headers=H_ADMIN,
        ).status_code == 201

        space = client.post(
            "/spaces",
            json={"key": "SEC", "name": "Secret space"},
            headers=H_ADMIN,
        ).json()
        hidden_page = client.post(
            f"/spaces/{space['id']}/pages",
            json={
                "title": "Classified page title",
                "body": f"backlink [[issue:{root['id']}]]",
            },
            headers=H_ADMIN,
        ).json()
        assert client.patch(
            f"/issues/{root['id']}",
            json={
                "body": (
                    f"refs [[issue:{hidden_issue['id']}]] "
                    f"[[page:{hidden_page['id']}]]"
                )
            },
            headers=H_ADMIN,
        ).status_code == 200
        assert client.put(
            f"/projects/{secret_project['id']}/visibility",
            json={"visibility": "private"},
            headers=H_ADMIN,
        ).status_code == 200
        assert client.put(
            f"/spaces/{space['id']}/visibility",
            json={"visibility": "private"},
            headers=H_ADMIN,
        ).status_code == 200

        hidden_packet, _ = _context(
            client, root["id"], headers=H_OUTSIDER
        )
        groups = [
            hidden_packet["hierarchy"]["children"],
            hidden_packet["dependencies"]["open_blockers"],
            hidden_packet["dependencies"]["blocked_by"],
            hidden_packet["references"]["outgoing"],
            hidden_packet["references"]["backlinks"],
        ]
        for group in groups:
            assert group == {"items": [], "visible_total": 0, "clipped": False}
        assert "Classified issue title" not in str(hidden_packet)
        assert "Classified page title" not in str(hidden_packet)
        assert "visible_open_blockers" not in hidden_packet["warnings"]

        assert client.post(
            f"/projects/{secret_project['id']}/members",
            json={"user_id": 2},
            headers=H_ADMIN,
        ).status_code == 201
        assert client.post(
            f"/spaces/{space['id']}/members",
            json={"user_id": 2},
            headers=H_ADMIN,
        ).status_code == 201

        granted, _ = _context(client, root["id"], headers=H_OUTSIDER)
        assert [item["id"] for item in granted["hierarchy"]["children"]["items"]] == [
            hidden_issue["id"]
        ]
        assert granted["hierarchy"]["children"]["visible_total"] == 1
        assert [
            item["id"] for item in granted["dependencies"]["open_blockers"]["items"]
        ] == [hidden_issue["id"]]
        assert granted["dependencies"]["blocked_by"]["visible_total"] == 1
        assert {
            (item["kind"], item["id"])
            for item in granted["references"]["outgoing"]["items"]
        } == {("issue", hidden_issue["id"]), ("page", hidden_page["id"])}
        assert granted["references"]["outgoing"]["visible_total"] == 2
        assert {
            (item["kind"], item["id"])
            for item in granted["references"]["backlinks"]["items"]
        } == {("issue", hidden_issue["id"]), ("page", hidden_page["id"])}
        assert granted["references"]["backlinks"]["visible_total"] == 2
        assert "visible_open_blockers" in granted["warnings"]


def test_custom_done_status_remains_blocked_by_but_is_not_an_open_blocker(
    tmp_path,
):
    """Readiness-adjacent warnings must use status categories, not status names."""
    with TestClient(create_app(tmp_path / "done-category.db")) as client:
        _bootstrap(client)
        project = _project(client, name="Delivery", key="DEL")
        status = client.post(
            f"/projects/{project['id']}/statuses",
            json={"name": "shipped", "category": "done"},
            headers=H_ADMIN,
        )
        assert status.status_code == 201
        blocker = _issue(
            client,
            title="Already shipped",
            project_id=project["id"],
            status="shipped",
        )
        root = _issue(
            client,
            title="Delivery target",
            project_id=project["id"],
        )
        assert client.post(
            f"/issues/{root['id']}/links",
            json={"relation": "blocked_by", "target_ref": blocker["key"]},
            headers=H_ADMIN,
        ).status_code == 201

        packet, _ = _context(client, root["key"])
        assert packet["dependencies"]["open_blockers"] == {
            "items": [],
            "visible_total": 0,
            "clipped": False,
        }
        assert packet["dependencies"]["blocked_by"]["visible_total"] == 1
        assert packet["dependencies"]["blocked_by"]["items"][0]["status"] == (
            "shipped"
        )
        assert packet["dependencies"]["blocked_by"]["items"][0][
            "status_category"
        ] == "done"
        assert "visible_open_blockers" not in packet["warnings"]


def test_exact_bounds_order_and_text_excerpts_are_explicit(tmp_path):
    """A hostile or simply busy issue cannot turn one read into an unbounded dump."""
    db_file = tmp_path / "bounds.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        space = client.post(
            "/spaces",
            json={"key": "KB", "name": "Knowledge"},
            headers=H_ADMIN,
        ).json()
        pages = []
        for index in range(work_context.REFERENCE_LIMIT + 1):
            response = client.post(
                f"/spaces/{space['id']}/pages",
                json={
                    "title": f"Page {index:02d}",
                    "body": (
                        "P" * (work_context.PAGE_BODY_LIMIT + 1)
                        if index == 0
                        else f"page body {index}"
                    ),
                },
                headers=H_ADMIN,
            )
            assert response.status_code == 201
            pages.append(response.json())

        refs = " ".join(f"[[page:{page['id']}]]" for page in pages)
        body = refs + "\n" + "D" * (work_context.ISSUE_DESCRIPTION_LIMIT + 1)
        root = _issue(client, title="Busy root", body=body)

        comment_ids = []
        for index in range(31):
            comment_body = (
                "C" * (work_context.COMMENT_BODY_LIMIT + 1)
                if index == 30
                else f"comment {index:02d}"
            )
            response = client.post(
                f"/issues/{root['id']}/comments",
                json={"body": comment_body},
                headers=H_ADMIN,
            )
            assert response.status_code == 201
            comment_ids.append(response.json()["id"])

        conn = db.connect(db_file)
        try:
            activity.record(
                conn,
                actor_id=1,
                verb="large_detail_probe",
                target_kind="issue",
                target_id=root["id"],
                detail="A" * (work_context.ACTIVITY_DETAIL_LIMIT + 1),
            )
        finally:
            conn.close()

        packet, _ = _context(client, root["id"])
        description = packet["issue"]["description"]
        assert description == {
            "text": body[: work_context.ISSUE_DESCRIPTION_LIMIT],
            "total_chars": len(body),
            "truncated": True,
        }

        outgoing = packet["references"]["outgoing"]
        assert outgoing["visible_total"] == work_context.REFERENCE_LIMIT + 1
        assert outgoing["clipped"] is True
        assert [item["id"] for item in outgoing["items"]] == [
            page["id"] for page in pages[: work_context.REFERENCE_LIMIT]
        ]
        assert outgoing["items"][0]["body"] == {
            "text": "P" * work_context.PAGE_BODY_LIMIT,
            "total_chars": work_context.PAGE_BODY_LIMIT + 1,
            "truncated": True,
        }

        comments = packet["comments"]
        assert comments["visible_total"] == 31
        assert comments["clipped"] is True
        assert [item["id"] for item in comments["items"]] == comment_ids[-20:]
        assert comments["items"][-1]["body"] == {
            "text": "C" * work_context.COMMENT_BODY_LIMIT,
            "total_chars": work_context.COMMENT_BODY_LIMIT + 1,
            "truncated": True,
        }

        recent = packet["recent_activity"]
        assert recent["visible_total"] == 33  # create + 31 comments + probe
        assert len(recent["items"]) == work_context.ACTIVITY_LIMIT
        assert recent["clipped"] is True
        assert [item["id"] for item in recent["items"]] == sorted(
            (item["id"] for item in recent["items"]), reverse=True
        )
        assert recent["items"][0]["detail"] == {
            "text": "A" * work_context.ACTIVITY_DETAIL_LIMIT,
            "total_chars": work_context.ACTIVITY_DETAIL_LIMIT + 1,
            "truncated": True,
        }
        assert "context_clipped" in packet["warnings"]


def test_context_etag_is_stable_on_reads_and_tracks_included_context_changes(
    tmp_path,
):
    """The strong validator covers more than the root issue row."""
    with TestClient(create_app(tmp_path / "etag.db")) as client:
        _bootstrap(client)
        space = client.post(
            "/spaces",
            json={"key": "DOC", "name": "Docs"},
            headers=H_ADMIN,
        ).json()
        page = client.post(
            f"/spaces/{space['id']}/pages",
            json={"title": "Plan", "body": "version one"},
            headers=H_ADMIN,
        ).json()
        root = _issue(
            client,
            title="Root",
            body=f"plan [[page:{page['id']}]]",
        )

        _, first = _context(client, root["id"])
        _, same = _context(client, root["id"])
        assert same == first

        assert client.post(
            f"/issues/{root['id']}/comments",
            json={"body": "new decision"},
            headers=H_ADMIN,
        ).status_code == 201
        _, after_comment = _context(client, root["id"])
        assert after_comment != first
        assert _context(client, root["id"])[1] == after_comment

        assert client.patch(
            f"/pages/{page['id']}",
            json={"body": "version two"},
            headers=H_ADMIN,
        ).status_code == 200
        _, after_page = _context(client, root["id"])
        assert after_page != after_comment

        referenced = _issue(client, title="New reference")
        assert client.patch(
            f"/issues/{root['id']}",
            json={
                "body": (
                    f"plan [[page:{page['id']}]] "
                    f"and work [[issue:{referenced['id']}]]"
                )
            },
            headers=H_ADMIN,
        ).status_code == 200
        _, after_reference = _context(client, root["id"])
        assert after_reference != after_page

        assert client.post(
            f"/issues/{root['id']}/links",
            json={
                "relation": "blocked_by",
                "target_ref": str(referenced["id"]),
            },
            headers=H_ADMIN,
        ).status_code == 201
        _, after_dependency = _context(client, root["id"])
        assert after_dependency != after_reference
        assert _context(client, root["id"])[1] == after_dependency


def test_work_context_read_has_no_domain_side_effects(tmp_path):
    """Reading an agent packet must not mutate its evidence or audit trail."""
    db_file = tmp_path / "side-effects.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        root = _issue(client, title="Read only")
        assert client.post(
            f"/issues/{root['id']}/comments",
            json={"body": "evidence"},
            headers=H_ADMIN,
        ).status_code == 201

        conn = db.connect(db_file)
        domain_tables = (
            "issues",
            "comments",
            "issue_links",
            "links",
            "attachments",
            "issue_contributors",
            "activity",
            "activity_visibility_projects",
            "projects",
            "project_members",
            "spaces",
            "space_members",
            "pages",
            "labels",
            "issue_labels",
            "page_labels",
        )

        def snapshot() -> dict[str, list[tuple]]:
            return {
                table: [
                    tuple(row)
                    for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
                for table in domain_tables
            }

        before = snapshot()
        _context(client, root["id"], headers=H_ADMIN)
        _context(client, root["id"], headers=H_ADMIN)
        assert snapshot() == before
        conn.close()


def test_work_context_is_one_wal_snapshot_when_a_write_interleaves(
    tmp_path, monkeypatch
):
    """A writer after root lookup cannot tear the in-flight context packet."""
    db_file = tmp_path / "snapshot.db"
    with TestClient(create_app(db_file)) as client:
        _bootstrap(client)
        root = _issue(client, title="Snapshot root")
        probe = db.connect(db_file)
        try:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            probe.close()

        original_contributors = work_context._contributors
        interleaved = False

        def insert_after_root_lookup(conn, issue_id):
            nonlocal interleaved
            if not interleaved:
                writer = db.connect(db_file)
                try:
                    writer.execute(
                        "INSERT INTO comments (issue_id, author_id, body) "
                        "VALUES (?, 1, 'arrived concurrently')",
                        (issue_id,),
                    )
                    writer.commit()
                finally:
                    writer.close()
                interleaved = True
            return original_contributors(conn, issue_id)

        # _contributors runs after the root/visibility reads and before comments.
        # Without the outer read transaction, this request would observe the newly
        # committed comment despite having resolved its root from an older state.
        monkeypatch.setattr(
            work_context, "_contributors", insert_after_root_lookup
        )
        in_flight, _ = _context(client, root["id"])
        assert interleaved is True
        assert in_flight["comments"] == {
            "items": [],
            "visible_total": 0,
            "clipped": False,
        }

        next_packet, _ = _context(client, root["id"])
        assert next_packet["comments"]["visible_total"] == 1
        assert next_packet["comments"]["items"][0]["body"]["text"] == (
            "arrived concurrently"
        )
