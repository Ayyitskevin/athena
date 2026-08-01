"""Every surrogate row id in a URL is bounded to what SQLite can hold.

SQLite stores INTEGER as signed 64-bit, and the ``sqlite3`` driver raises
``OverflowError`` — not a clean refusal — when asked to bind anything larger.
An unbounded ``int`` path parameter therefore turns
``GET /workers/9223372036854775808`` into a 500 from inside the driver, on a
surface where every other out-of-range input is a tidy 422.

The adversarial review found this on ``/run-controls`` and it was fixed there;
a later review found the same class still open on that endpoint's own
``worker_id`` body field. That is the argument for a single shared bound
(``core/ids.py``) and for this test, which walks the routes themselves rather
than a list someone must remember to extend: a new route with a bare ``int``
id fails here the day it is added.
"""

import pathlib
import re

from fastapi.testclient import TestClient
import pytest

from athena.core.ids import MAX_SQLITE_INTEGER
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}

#: One past what SQLite can bind — the exact value that used to 500.
OVERFLOW = MAX_SQLITE_INTEGER + 1

_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "athena"

# Path parameters that are NOT row ids: opaque client strings and refs with
# their own validation, which must keep their own (non-integer) contracts.
_NON_ID_PARAMS = {"run_id", "ref", "source_name", "space_key", "title", "key"}


def _get_routes_with_id_params() -> list[str]:
    """Every GET route whose path carries a surrogate-id placeholder.

    Read from the decorators rather than the live app, because the app's
    OpenAPI schema is deliberately disabled in the hardened deployment."""
    found: set[str] = set()
    for path in sorted(_SOURCE_ROOT.rglob("*_api.py")):
        source = path.read_text()
        prefix_match = re.search(r'APIRouter\(prefix="([^"]+)"', source)
        prefix = prefix_match.group(1) if prefix_match else ""
        for match in re.finditer(
            r'@\w*router\.get\(\s*\n?\s*["\']([^"\']*)["\']', source
        ):
            route = match.group(1)
            params = set(re.findall(r"\{(\w+)\}", route))
            if not params or params & _NON_ID_PARAMS:
                continue
            found.add(prefix + route)
    return sorted(found)


@pytest.fixture
def client(tmp_path):
    # Function-scoped on purpose: conftest's network-boundary and bootstrap
    # fixtures are function-scoped autouse, and an app built outside them is
    # refused by the deployment guard (503) before any route runs.
    app = create_app(tmp_path / "bounds.db")
    with TestClient(app) as test_client:
        test_client.post(
            "/users",
            json={"email": "a@e.com", "name": "Ann", "password": "pw-long-enough"},
        )
        yield test_client


def test_the_route_walk_actually_finds_routes():
    # WHY: a discovery bug would make every assertion below vacuously pass.
    routes = _get_routes_with_id_params()
    assert len(routes) >= 15, routes
    assert "/workers/{worker_id}" in routes


def test_no_get_route_500s_on_an_id_sqlite_cannot_hold(client):
    # WHY: this is the defect class itself — the driver raising OverflowError
    # deep inside a read, surfacing as a 500 where the same surface answers 422
    # for every other out-of-range input.
    failures = []
    for route in _get_routes_with_id_params():
        url = re.sub(r"\{[^}]+\}", str(OVERFLOW), route)
        response = client.get(url, headers=H1)
        if response.status_code >= 500:
            failures.append((route, response.status_code))
    assert not failures, f"routes 500 on an oversized id: {failures}"


def test_oversized_ids_are_refused_as_validation_errors(client):
    # WHY: "not a 500" is not enough — the contract is that an id outside the
    # storable range is a REFUSAL the caller can act on, not a 404 that reads
    # as "this row does not exist" (it never could have).
    for route in ("/workers/{worker_id}", "/webhooks/{webhook_id}"):
        url = re.sub(r"\{[^}]+\}", str(OVERFLOW), route)
        assert client.get(url, headers=H1).status_code == 422, route


def test_zero_and_negative_ids_are_refused_too(client):
    # WHY: a surrogate id is a positive integer. Zero reaching a query is a
    # caller bug worth naming, not an empty result that reads like "not found".
    for value in (0, -1):
        assert client.get(f"/workers/{value}", headers=H1).status_code == 422


def test_ids_inside_the_range_still_reach_their_handler(client):
    # WHY: a bound that also refuses legitimate ids would be a worse bug than
    # the one it fixes. The largest storable id is a normal 404 (no such row).
    assert client.get(f"/workers/{MAX_SQLITE_INTEGER}", headers=H1).status_code == 404
    assert client.get("/workers/1", headers=H1).status_code == 404
