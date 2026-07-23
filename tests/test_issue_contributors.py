"""Issue contributors — delegating teammates (humans or agents) onto an issue.

These encode the contract from the agent-as-teammate model: an issue keeps one
accountable assignee, and contributors are additional actors added alongside.
Adding one is write-gated (creator/assignee), idempotent, audited, and auto-watches
the new contributor so the delegation lands in their inbox; an unknown user is
rejected; removing a non-contributor is a 404; and the web detail page exposes the
add/remove forms.
"""

from athena.main import create_app
from fastapi.testclient import TestClient

H1 = {"X-Athena-Actor": "1"}
H2 = {"X-Athena-Actor": "2"}


def _admin(client):
    client.post("/users", json={"email": "a@e.com", "name": "Alice", "password": "pw"})


def _user2(client):
    # A second user — the "agent"/teammate being delegated to.
    client.post("/users", json={"email": "b@e.com", "name": "Bob"}, headers=H1)


def _agent2(client):
    client.post(
        "/users",
        json={"email": "bot@e.com", "name": "Bot", "is_agent": True},
        headers=H1,
    )


def _issue(client, title):
    return client.post("/issues", json={"title": title}, headers=H1).json()


# --- API --------------------------------------------------------------------


def test_add_list_remove_contributor(tmp_path):
    with TestClient(create_app(tmp_path / "c.db")) as client:
        _admin(client)
        _user2(client)
        iss = _issue(client, "deploy")
        iid = iss["id"]
        assert client.get(f"/issues/{iid}/contributors").json() == []
        r = client.post(f"/issues/{iid}/contributors", json={"user_id": 2}, headers=H1)
        assert r.status_code == 201
        assert [c["name"] for c in r.json()] == ["Bob"]
        assert r.json()[0]["added_by"] == 1
        # clear it
        rem = client.delete(f"/issues/{iid}/contributors/2", headers=H1)
        assert rem.status_code == 200 and rem.json() == []


def test_delegate_issue_to_agent_keeps_assignee_and_audits(tmp_path):
    # WHY: explicit delegation is agent-as-teammate, not reassignment: the human
    # stays accountable while the agent joins as a contributor with its own audit verb.
    with TestClient(create_app(tmp_path / "d.db")) as client:
        _admin(client)
        _agent2(client)
        iid = _issue(client, "delegate")["id"]
        client.put(f"/issues/{iid}/assignee", json={"assignee_id": 1}, headers=H1)

        delegated = client.post(
            f"/issues/{iid}/delegate", json={"user_id": 2}, headers=H1
        )
        assert delegated.status_code == 201, delegated.text
        assert delegated.json()[0]["name"] == "Bot"
        assert delegated.json()[0]["is_agent"] is True
        assert client.get(f"/issues/{iid}", headers=H1).json()["assignee_id"] == 1

        verbs = [
            a["verb"]
            for a in client.get(
                f"/activity?target_kind=issue&target_id={iid}", headers=H1
            ).json()
        ]
        assert "delegated" in verbs and "added_contributor" not in verbs
        assert any(
            n["verb"] == "delegated"
            for n in client.get("/notifications", headers=H2).json()
        )


def test_delegate_requires_agent_user(tmp_path):
    with TestClient(create_app(tmp_path / "human.db")) as client:
        _admin(client)
        _user2(client)
        iid = _issue(client, "human")["id"]
        rejected = client.post(
            f"/issues/{iid}/delegate", json={"user_id": 2}, headers=H1
        )
        assert rejected.status_code == 422


def test_add_is_idempotent(tmp_path):
    with TestClient(create_app(tmp_path / "i.db")) as client:
        _admin(client)
        _user2(client)
        iid = _issue(client, "x")["id"]
        client.post(f"/issues/{iid}/contributors", json={"user_id": 2}, headers=H1)
        again = client.post(
            f"/issues/{iid}/contributors", json={"user_id": 2}, headers=H1
        )
        assert [c["user_id"] for c in again.json()] == [2]  # still just one


def test_unknown_user_rejected(tmp_path):
    with TestClient(create_app(tmp_path / "u.db")) as client:
        _admin(client)
        iid = _issue(client, "x")["id"]
        assert (
            client.post(
                f"/issues/{iid}/contributors", json={"user_id": 999}, headers=H1
            ).status_code
            == 422
        )


def test_add_is_write_gated(tmp_path):
    # WHY: delegating is a write on the issue — only its creator/assignee may do it.
    with TestClient(create_app(tmp_path / "g.db")) as client:
        _admin(client)
        _user2(client)
        iid = _issue(client, "x")["id"]  # created by user 1
        # user 2 is neither creator nor assignee
        assert (
            client.post(
                f"/issues/{iid}/contributors", json={"user_id": 2}, headers=H2
            ).status_code
            == 403
        )


def test_delegation_lands_in_contributor_inbox(tmp_path):
    # WHY: a delegated teammate should hear about it — adding a contributor
    # auto-watches them, so the "added_contributor" event fans out to their inbox.
    with TestClient(create_app(tmp_path / "n.db")) as client:
        _admin(client)
        _user2(client)
        iid = _issue(client, "ship it")["id"]
        client.post(f"/issues/{iid}/contributors", json={"user_id": 2}, headers=H1)
        inbox = client.get("/notifications", headers=H2).json()
        assert any(n["verb"] == "added_contributor" for n in inbox)


def test_add_and_remove_are_audited(tmp_path):
    with TestClient(create_app(tmp_path / "a.db")) as client:
        _admin(client)
        _user2(client)
        iid = _issue(client, "x")["id"]
        client.post(f"/issues/{iid}/contributors", json={"user_id": 2}, headers=H1)
        client.delete(f"/issues/{iid}/contributors/2", headers=H1)
        verbs = [
            a["verb"]
            for a in client.get(
                f"/activity?target_kind=issue&target_id={iid}", headers=H1
            ).json()
        ]
        assert "added_contributor" in verbs and "removed_contributor" in verbs


def test_remove_non_contributor_is_404(tmp_path):
    with TestClient(create_app(tmp_path / "r.db")) as client:
        _admin(client)
        _user2(client)
        iid = _issue(client, "x")["id"]
        assert (
            client.delete(f"/issues/{iid}/contributors/2", headers=H1).status_code
            == 404
        )


def test_contributors_of_missing_issue_is_404(tmp_path):
    with TestClient(create_app(tmp_path / "m.db")) as client:
        _admin(client)
        assert client.get("/issues/9999/contributors").status_code == 404


# --- web --------------------------------------------------------------------


def _login(client):
    client.post("/login", data={"email": "a@e.com", "password": "pw"})
    return client.cookies.get("athena_csrf")


def test_web_detail_add_and_remove_contributor(tmp_path):
    with TestClient(create_app(tmp_path / "w.db")) as client:
        _admin(client)
        _user2(client)
        iid = _issue(client, "deploy")["id"]
        csrf = _login(client)
        page = client.get(f"/aegis/issues/{iid}").text
        assert "Contributors" in page and "Delegate to…" in page

        client.post(
            f"/aegis/issues/{iid}/contributors",
            data={"user_id": "2"},
            headers={"X-CSRF-Token": csrf},
        )
        page = client.get(f"/aegis/issues/{iid}").text
        assert "Bob" in page
        assert f"/aegis/issues/{iid}/contributors/2/delete" in page
        assert [
            c["name"] for c in client.get(f"/issues/{iid}/contributors").json()
        ] == ["Bob"]

        client.post(
            f"/aegis/issues/{iid}/contributors/2/delete",
            headers={"X-CSRF-Token": csrf},
        )
        assert client.get(f"/issues/{iid}/contributors").json() == []
