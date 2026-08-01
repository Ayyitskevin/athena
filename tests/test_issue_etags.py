"""Strong issue ETags and atomic If-Match update preconditions."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from athena.core.etag import strong_etag
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}
H3 = {"X-Athena-Actor": "3"}


def _bootstrap(client: TestClient) -> None:
    response = client.post("/users", json={"email": "a@e.com", "name": "A"})
    assert response.status_code == 201


def _create_issue(client: TestClient, title: str = "one", **extra) -> tuple[dict, str]:
    response = client.post("/issues", json={"title": title, **extra}, headers=H1)
    assert response.status_code == 201
    return response.json(), response.headers["etag"]


def _created_event_count(client: TestClient) -> int:
    return len(
        client.get("/events", params={"kind": "issue"}, headers=H1).json()["events"]
    )


def test_issue_etag_is_strong_stable_and_changes_only_with_representation(tmp_path):
    with TestClient(create_app(tmp_path / "stable.db")) as client:
        _bootstrap(client)
        issue, created_tag = _create_issue(client)
        assert created_tag.startswith('"sha256-') and created_tag.endswith('"')
        assert "_etag" not in issue

        first = client.get(f"/issues/{issue['id']}", headers=H1)
        second = client.get(f"/issues/{issue['id']}", headers=H1)
        assert first.json() == second.json() == issue
        assert first.headers["etag"] == second.headers["etag"] == created_tag
        assert first.headers["etag"] == strong_etag("issue-v1", first.json())

        events_before = _created_event_count(client)
        no_op = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "one"},
            headers={**H1, "If-Match": created_tag},
        )
        assert no_op.status_code == 200
        assert no_op.headers["etag"] == created_tag
        assert _created_event_count(client) == events_before

        changed = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "two"},
            headers={**H1, "If-Match": created_tag},
        )
        assert changed.status_code == 200
        assert changed.headers["etag"] != created_tag
        assert changed.json()["title"] == "two"


def test_hidden_parent_is_redacted_from_actor_visible_etags_and_writes(tmp_path):
    """A hidden relationship must not leak through JSON, validators, or If-Match."""
    with TestClient(create_app(tmp_path / "parent-visibility.db")) as client:
        _bootstrap(client)
        outsider = client.post(
            "/users",
            json={
                "email": "outsider@e.com",
                "name": "Outsider",
                "password": "pw",
                "is_agent": True,
            },
            headers=H1,
        ).json()
        secret_project = client.post(
            "/projects",
            json={"name": "Secret", "key": "SEC"},
            headers=H1,
        ).json()
        first_parent, _ = _create_issue(
            client,
            title="First hidden parent",
            project_id=secret_project["id"],
        )
        second_parent, _ = _create_issue(
            client,
            title="Second hidden parent",
            project_id=secret_project["id"],
        )
        child, child_tag = _create_issue(client, title="redactionchild")
        assigned = client.put(
            f"/issues/{child['id']}/assignee",
            json={"assignee_id": outsider["id"]},
            headers={**H1, "If-Match": child_tag},
        )
        assert assigned.status_code == 200
        assert (
            client.put(
                f"/issues/{child['id']}/parent",
                json={"parent_id": first_parent["id"]},
                headers=H1,
            ).status_code
            == 200
        )
        path = f"/issues/{child['id']}"
        public_parent_view = client.get(path, headers=H2)
        assert public_parent_view.json()["parent_id"] == first_parent["id"]
        assert (
            client.put(
                f"/projects/{secret_project['id']}/visibility",
                json={"visibility": "private"},
                headers=H1,
            ).status_code
            == 200
        )

        anonymous_first = client.get(path)
        outsider_first = client.get(path, headers=H2)
        owner_first = client.get(path, headers=H1)
        assert anonymous_first.json()["parent_id"] is None
        assert outsider_first.json()["parent_id"] is None
        assert owner_first.json()["parent_id"] == first_parent["id"]
        assert anonymous_first.headers["etag"] == outsider_first.headers["etag"]
        assert outsider_first.headers["etag"] == strong_etag(
            "issue-v1", outsider_first.json()
        )
        assert owner_first.headers["etag"] == strong_etag(
            "issue-v1", owner_first.json()
        )
        assert owner_first.headers["etag"] != outsider_first.headers["etag"]
        assert owner_first.headers["etag"] == public_parent_view.headers["etag"]

        for params in ({"search": "redactionchild"}, {"q": "redactionchild"}):
            listed = client.get("/issues", params=params, headers=H2)
            assert listed.status_code == 200
            listed_child = next(
                row for row in listed.json() if row["id"] == child["id"]
            )
            assert listed_child["parent_id"] is None

        outsider_context = client.get(f"{path}/work-context", headers=H2).json()
        assert outsider_context["hierarchy"]["parent"] is None
        assert outsider_context["issue_etag"] == outsider_first.headers["etag"]

        assert (
            client.put(
                f"/issues/{child['id']}/parent",
                json={"parent_id": second_parent["id"]},
                headers=H1,
            ).status_code
            == 200
        )
        outsider_second = client.get(path, headers=H2)
        owner_second = client.get(path, headers=H1)
        assert outsider_second.json() == outsider_first.json()
        assert outsider_second.headers["etag"] == outsider_first.headers["etag"]
        assert owner_second.json()["parent_id"] == second_parent["id"]
        assert owner_second.headers["etag"] != owner_first.headers["etag"]

        wrong_actor_tag = client.patch(
            path,
            json={"body": "must not land"},
            headers={**H2, "If-Match": owner_second.headers["etag"]},
        )
        assert wrong_actor_tag.status_code == 412
        assert wrong_actor_tag.headers["etag"] == outsider_second.headers["etag"]

        grant = client.post(
            f"/projects/{secret_project['id']}/members",
            json={"user_id": outsider["id"]},
            headers=H1,
        )
        assert grant.status_code == 201
        member_view = client.get(path, headers=H2)
        assert member_view.json()["parent_id"] == second_parent["id"]
        assert member_view.headers["etag"] == owner_second.headers["etag"]

        revoke = client.delete(
            f"/projects/{secret_project['id']}/members/{outsider['id']}",
            headers=H1,
        )
        assert revoke.status_code == 200
        redacted_again = client.get(path, headers=H2)
        assert redacted_again.json()["parent_id"] is None
        assert redacted_again.headers["etag"] == outsider_second.headers["etag"]

        stale_owner = client.patch(
            path,
            json={"body": "must not land"},
            headers={**H1, "If-Match": owner_first.headers["etag"]},
        )
        assert stale_owner.status_code == 412
        assert stale_owner.headers["etag"] == owner_second.headers["etag"]

        accepted_outsider = client.patch(
            path,
            json={"body": "actor-visible edit"},
            headers={**H2, "If-Match": outsider_first.headers["etag"]},
        )
        assert accepted_outsider.status_code == 200
        assert accepted_outsider.json()["parent_id"] is None
        assert accepted_outsider.headers["etag"] == strong_etag(
            "issue-v1", accepted_outsider.json()
        )

        claimed = client.post(
            f"{path}/claim",
            headers={**H2, "If-Match": accepted_outsider.headers["etag"]},
        )
        assert claimed.status_code == 201

        login = client.post(
            "/login",
            data={"email": "outsider@e.com", "password": "pw"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        board = client.get("/aegis/boards")
        assert board.status_code == 200
        escaped_outsider_tag = accepted_outsider.headers["etag"].replace('"', "&#34;")
        assert f'value="{escaped_outsider_tag}"' in board.text


def test_stale_if_match_is_412_and_has_no_side_effect(tmp_path):
    with TestClient(create_app(tmp_path / "stale.db")) as client:
        _bootstrap(client)
        issue, original_tag = _create_issue(client)
        changed = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "current"},
            headers={**H1, "If-Match": original_tag},
        )
        current_tag = changed.headers["etag"]
        events_before = _created_event_count(client)

        stale = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "lost update"},
            headers={**H1, "If-Match": original_tag},
        )
        assert stale.status_code == 412
        assert stale.json() == {
            "detail": "If-Match precondition failed",
            "code": "precondition_failed",
        }
        assert stale.headers["etag"] == current_tag
        assert client.get(f"/issues/{issue['id']}").json()["title"] == "current"
        assert _created_event_count(client) == events_before

        invalid = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "   "},
            headers={**H1, "If-Match": original_tag},
        )
        assert invalid.status_code == 422


def test_all_command_backed_issue_relationship_routes_honor_if_match(tmp_path):
    with TestClient(create_app(tmp_path / "relationships.db")) as client:
        _bootstrap(client)
        user = client.post(
            "/users",
            json={"email": "b@e.com", "name": "B"},
            headers=H1,
        ).json()
        project = client.post(
            "/projects", json={"name": "P", "key": "P"}, headers=H1
        ).json()
        sprint = client.post(
            f"/projects/{project['id']}/sprints",
            json={"name": "S"},
            headers=H1,
        ).json()
        issue, tag = _create_issue(client)

        assigned = client.put(
            f"/issues/{issue['id']}/assignee",
            json={"assignee_id": user["id"]},
            headers={**H1, "If-Match": tag},
        )
        assert assigned.status_code == 200
        assert assigned.headers["etag"] != tag
        tag = assigned.headers["etag"]

        moved = client.put(
            f"/issues/{issue['id']}/project",
            json={"project_id": project["id"]},
            headers={**H1, "If-Match": tag},
        )
        assert moved.status_code == 200
        tag = moved.headers["etag"]

        sprinted = client.put(
            f"/issues/{issue['id']}/sprint",
            json={"sprint_id": sprint["id"]},
            headers={**H1, "If-Match": tag},
        )
        assert sprinted.status_code == 200
        assert sprinted.headers["etag"] != tag


def test_if_match_http_grammar_and_bounds(tmp_path):
    with TestClient(create_app(tmp_path / "grammar.db")) as client:
        _bootstrap(client)
        issue, tag = _create_issue(client)
        path = f"/issues/{issue['id']}"
        body = {"title": "one"}

        assert (
            client.patch(path, json=body, headers={**H1, "If-Match": "*"}).status_code
            == 200
        )
        assert (
            client.patch(
                path,
                json=body,
                headers={**H1, "If-Match": f'"missing", {tag}'},
            ).status_code
            == 200
        )
        assert (
            client.patch(
                path,
                json=body,
                headers={**H1, "If-Match": f'"opaque,comma", {tag}'},
            ).status_code
            == 200
        )
        repeated = client.patch(
            path,
            json=body,
            headers=[
                ("X-Athena-Actor", "1"),
                ("If-Match", '"missing"'),
                ("If-Match", tag),
            ],
        )
        assert repeated.status_code == 200

        weak = client.patch(path, json=body, headers={**H1, "If-Match": f"W/{tag}"})
        assert weak.status_code == 412
        for accepted in (f"{tag},", f", {tag}", f'{tag}, , "other"'):
            assert (
                client.patch(
                    path, json=body, headers={**H1, "If-Match": accepted}
                ).status_code
                == 200
            )
        empty = client.patch(path, json=body, headers={**H1, "If-Match": ""})
        assert empty.status_code == 412
        for malformed in ("unquoted", f"*, {tag}"):
            response = client.patch(
                path, json=body, headers={**H1, "If-Match": malformed}
            )
            assert response.status_code == 400
            assert response.json()["code"] == "invalid_if_match"

        oversized = client.patch(
            path,
            json=body,
            headers={**H1, "If-Match": '"' + ("x" * 8200) + '"'},
        )
        assert oversized.status_code == 431
        assert oversized.json()["code"] == "if_match_too_large"


def test_visibility_and_role_errors_precede_stale_precondition(tmp_path):
    with TestClient(create_app(tmp_path / "auth.db")) as client:
        _bootstrap(client)
        client.post(
            "/users",
            json={"email": "b@e.com", "name": "B"},
            headers=H1,
        )
        client.post(
            "/users",
            json={"email": "c@e.com", "name": "C", "role": "viewer"},
            headers=H1,
        )
        project = client.post(
            "/projects", json={"name": "Secret", "key": "SEC"}, headers=H1
        ).json()
        client.put(
            f"/projects/{project['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        hidden, _ = _create_issue(client, project_id=project["id"])
        public, _ = _create_issue(client)

        hidden_response = client.patch(
            f"/issues/{hidden['id']}",
            json={"title": "probe"},
            headers={**H2, "If-Match": '"stale"'},
        )
        assert hidden_response.status_code == 404

        viewer_response = client.patch(
            f"/issues/{public['id']}",
            json={"title": "probe"},
            headers={**H3, "If-Match": '"stale"'},
        )
        assert viewer_response.status_code == 403


def test_concurrent_writers_with_one_tag_have_one_winner(tmp_path):
    app = create_app(tmp_path / "concurrent.db")
    with TestClient(app) as client:
        _bootstrap(client)
        issue, tag = _create_issue(client)
        path = f"/issues/{issue['id']}"

        def write(title: str, key: str):
            return client.patch(
                path,
                json={"title": title},
                headers={**H1, "If-Match": tag, "Idempotency-Key": key},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(lambda args: write(*args), [("alpha", "a"), ("beta", "b")])
            )

        assert sorted(response.status_code for response in responses) == [200, 412]
        winner = next(response for response in responses if response.status_code == 200)
        loser = next(response for response in responses if response.status_code == 412)
        assert loser.headers["etag"] == winner.headers["etag"]
        assert client.get(path).json()["title"] in {"alpha", "beta"}
        assert _created_event_count(client) == 2


def test_idempotent_retry_replays_original_etag_and_changed_precondition_conflicts(
    tmp_path,
):
    with TestClient(create_app(tmp_path / "idempotent.db")) as client:
        _bootstrap(client)
        issue, tag = _create_issue(client)
        path = f"/issues/{issue['id']}"
        headers = {**H1, "If-Match": tag, "Idempotency-Key": "edit-once"}

        first = client.patch(path, json={"title": "updated"}, headers=headers)
        replay = client.patch(path, json={"title": "updated"}, headers=headers)
        assert first.status_code == replay.status_code == 200
        assert replay.headers["idempotent-replay"] == "true"
        assert replay.headers["etag"] == first.headers["etag"]
        assert replay.json() == first.json()

        changed_header = client.patch(
            path,
            json={"title": "updated"},
            headers={
                **H1,
                "If-Match": first.headers["etag"],
                "Idempotency-Key": "edit-once",
            },
        )
        assert changed_header.status_code == 409
        assert changed_header.json()["code"] == "idempotency_mismatch"
        assert _created_event_count(client) == 2


def test_derived_project_and_label_changes_change_get_etag(tmp_path):
    with TestClient(create_app(tmp_path / "derived.db")) as client:
        _bootstrap(client)
        project = client.post(
            "/projects", json={"name": "Old", "key": "OLD"}, headers=H1
        ).json()
        issue, first_tag = _create_issue(client, project_id=project["id"])

        client.patch(f"/projects/{project['id']}", json={"name": "New"}, headers=H1)
        second = client.get(f"/issues/{issue['id']}")
        second_tag = second.headers["etag"]
        assert second_tag != first_tag

        events_before = _created_event_count(client)
        stale_project = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "must not land"},
            headers={**H1, "If-Match": first_tag},
        )
        assert stale_project.status_code == 412
        assert stale_project.headers["etag"] == second_tag
        assert stale_project.json()["code"] == "precondition_failed"
        assert _created_event_count(client) == events_before
        assert client.get(f"/issues/{issue['id']}").json()["title"] == "one"

        label = client.post(
            "/labels", json={"name": "bug", "color": "#ff0000"}, headers=H1
        ).json()
        client.post(
            f"/issues/{issue['id']}/labels",
            json={"label_id": label["id"]},
            headers=H1,
        )
        third = client.get(f"/issues/{issue['id']}")
        third_tag = third.headers["etag"]
        assert third_tag != second_tag

        events_before = _created_event_count(client)
        stale_label = client.patch(
            f"/issues/{issue['id']}",
            json={"title": "also must not land"},
            headers={**H1, "If-Match": second_tag},
        )
        assert stale_label.status_code == 412
        assert stale_label.headers["etag"] == third_tag
        assert _created_event_count(client) == events_before
        assert client.get(f"/issues/{issue['id']}").json()["title"] == "one"
