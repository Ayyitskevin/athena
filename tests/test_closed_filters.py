"""An unknown value for a closed filter is a 422, not an empty 200 (A9).

``GET /search?kind=bogus`` used to answer ``[]``: a typo looked exactly like "no
results". A filter whose values are a closed set Athena owns now refuses any
other value and names the allowed ones. Filters over configured names (a
project's statuses, labels, rooms) still match nothing for an unknown name,
because the set is the operator's to change.
"""

import pytest
from fastapi.testclient import TestClient

from athena.aegis import issues
from athena.core import dispatch, search
from athena.main import create_app

ADMIN = {"X-Athena-Actor": "1"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "filters.sqlite")) as client:
        created = client.post(
            "/users", json={"email": "a@example.com", "name": "Ann"}, headers=ADMIN
        )
        assert created.status_code == 201, created.text
        client.headers.update(ADMIN)
        client.post("/issues", json={"title": "findable widget"})
        yield client


@pytest.mark.parametrize(
    ("path", "values", "name"),
    [
        ("/search?q=widget", search.KINDS, "kind"),
        ("/issues?limit=5", issues.PRIORITIES, "priority"),
        ("/dispatches?limit=5", dispatch.STATES, "state"),
    ],
)
def test_an_unknown_closed_filter_value_is_refused_and_names_the_set(
    client, path, values, name
):
    refused = client.get(f"{path}&{name}=bogus")
    assert refused.status_code == 422, refused.text
    assert refused.json() == {
        "detail": f"{name} must be one of: {', '.join(values)}",
        "code": "validation_error",
    }
    for value in values:
        accepted = client.get(f"{path}&{name}={value}")
        assert accepted.status_code == 200, (value, accepted.text)


def test_search_still_finds_by_a_known_kind(client):
    hits = client.get("/search?q=widget&kind=issue").json()
    assert [hit["title"] for hit in hits] == ["findable widget"]


def test_a_configured_name_filter_still_matches_nothing_rather_than_refusing(client):
    assert client.get("/issues?status=no_such_status").json()["issues"] == []
