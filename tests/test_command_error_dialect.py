"""The error-kind dialect, and the wire contract it must not change.

Six command modules used to carry an HTTP ``status_code``. They now carry a
transport-neutral ``kind``, mapped by each route module's own ``STATUS_BY_KIND``.

That refactor is only correct if it is invisible from outside, so these tests pin
both halves:

  * the command layer names WHAT went wrong (`kind`) and no longer names how to
    say it — asserted structurally, so a module that regresses to a status code
    fails here rather than in review;
  * the HTTP surface answers exactly what it answered before — asserted over the
    real app, because "identical wire statuses" is a claim about responses, not
    about a mapping table.
"""

from fastapi.testclient import TestClient
import pytest

from athena.aegis import api as aegis_api
from athena.aegis import comment_commands, sprint_commands, sprints_api
from athena.core import token_commands, tokens_api, webhook_commands, webhooks_api
from athena.main import create_app
from athena.mentor import api as mentor_api
from athena.mentor import page_comment_commands, space_commands

H1 = {"X-Athena-Actor": "1"}

MIGRATED = (
    space_commands.SpaceCommandError,
    comment_commands.CommentCommandError,
    sprint_commands.SprintCommandError,
    token_commands.TokenCommandError,
    webhook_commands.WebhookCommandError,
    page_comment_commands.PageCommentCommandError,
)

ROUTE_MODULES = (
    mentor_api,
    aegis_api,
    sprints_api,
    tokens_api,
    webhooks_api,
)

#: The dialect's canonical mapping. A route module may deviate deliberately, but
#: none of these five does, and an accidental deviation should fail loudly.
CANONICAL = {
    "not_found": 404,
    "invalid": 422,
    "conflict": 409,
    "forbidden": 403,
    "unauthorized": 401,
}


@pytest.mark.parametrize("error_type", MIGRATED, ids=lambda e: e.__name__)
def test_the_command_layer_names_a_kind_not_a_status(error_type):
    exc = error_type("not_found", "gone")
    assert exc.kind == "not_found"
    assert str(exc) == "gone"
    # The whole point: a command no longer knows what an HTTP status is.
    assert not hasattr(exc, "status_code")


@pytest.mark.parametrize("module", ROUTE_MODULES, ids=lambda m: m.__name__)
def test_every_route_module_maps_the_dialect_the_same_way(module):
    assert module.STATUS_BY_KIND == CANONICAL


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(tmp_path / "dialect.db")) as c:
        c.post(
            "/users",
            json={"email": "a@e.com", "name": "A", "password": "secret"},
            headers=H1,
        )
        yield c


def test_the_wire_statuses_are_unchanged(client):
    # WHY: the refactor's actual contract. Each of these paths reaches a migrated
    # command's refusal (or the boundary check in front of it) and must answer
    # exactly what it answered before the kinds existed.
    project = client.post(
        "/projects", json={"key": "ATH", "name": "Athena"}, headers=H1
    ).json()
    issue = client.post(
        "/issues", json={"title": "x", "project_id": project["id"]}, headers=H1
    ).json()

    # comment edit/delete on something that is not there -> 404
    assert (
        client.patch(
            f"/issues/{issue['id']}/comments/9999", json={"body": "y"}, headers=H1
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/issues/{issue['id']}/comments/9999", headers=H1).status_code
        == 404
    )

    # sprint lifecycle on a missing sprint -> 404
    for path in ("start", "complete"):
        assert client.post(f"/sprints/9999/{path}", headers=H1).status_code == 404, path
    assert client.delete("/sprints/9999", headers=H1).status_code == 404

    # space edit/delete on a missing space -> 404
    assert (
        client.patch("/spaces/9999", json={"name": "n"}, headers=H1).status_code == 404
    )
    assert client.delete("/spaces/9999", headers=H1).status_code == 404

    # page comment edit on a missing comment -> 404
    space = client.post(
        "/spaces", json={"key": "ENG", "name": "Eng"}, headers=H1
    ).json()
    page = client.post(
        f"/spaces/{space['id']}/pages", json={"title": "P"}, headers=H1
    ).json()
    assert (
        client.patch(
            f"/pages/{page['id']}/comments/9999", json={"body": "y"}, headers=H1
        ).status_code
        == 404
    )

    # webhook pause on a missing webhook -> 404
    assert client.post("/webhooks/9999/pause", headers=H1).status_code == 404

    # an unknown token scope -> 422 (the one migrated 'invalid')
    bad = client.post(
        "/tokens", json={"name": "t", "scopes": ["not-a-scope"]}, headers=H1
    )
    assert bad.status_code == 422
