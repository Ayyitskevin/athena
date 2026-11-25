"""The cockpit's own JSON answers name a registered refusal code (G4).

WHY: every REST refusal carries a ``code`` from ``core/refusal_codes.py`` (A5),
and the source check held REST modules to it. The cockpit's JSON routes (the
command palette, the fleet roster's JSON, a run's replay file, a project's
floor) were skipped as "browser adapters": their refusals carried a detail and
no code, and the palette sent the command's internal kind (``invalid``) where
a registered code (``validation_error``) belongs. A client of those routes, the
palette's own script included, could not tell refusals apart by code.

The source check now holds ``web/`` to the JSON-body rules (rules 4 and 5);
its HTML answers keep their status tables, which carry no codes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from support import ADMIN, add_user, bootstrap, login, make_app

from athena.core import refusal_codes

REPO = Path(__file__).resolve().parents[1]


def _checker():
    spec = importlib.util.spec_from_file_location(
        "check_refusal_codes", REPO / "scripts" / "check_refusal_codes.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["check_refusal_codes"] = module
    spec.loader.exec_module(module)
    return module


def test_a_cockpit_json_refusal_without_a_code_fails_the_check():
    checker = _checker()
    codes = checker.registered_codes(checker.REGISTRY_PATH.read_text())
    uncoded = (
        "def route():\n"
        "    return JSONResponse({'detail': 'no such thing'}, status_code=404)\n"
    )
    found = checker.check_source(
        uncoded, "web/x.py", codes, rest=False, json_bodies=True
    )
    assert any('"detail" but no "code"' in str(v) for v in found), found
    unregistered = (
        "def route():\n"
        "    return JSONResponse({'detail': 'no', 'code': 'invalid'}, status_code=422)\n"
    )
    found = checker.check_source(
        unregistered, "web/x.py", codes, rest=False, json_bodies=True
    )
    assert any("'invalid'" in str(v) for v in found), found
    # The cockpit's HTML status tables carry no codes and are not held to rule 3.
    html_table = "SEAT_STATUS_BY_KIND = {'invalid': 400, 'conflict': 409}\n"
    assert (
        checker.check_source(
            html_table, "web/x.py", codes, rest=False, json_bodies=True
        )
        == []
    )


def test_the_check_holds_web_to_the_json_rules():
    checker = _checker()
    # The real tree: the check reads web/ with the JSON rules on, and passes.
    assert checker.check() == []
    assert checker.json_rules_apply("web") and checker.json_rules_apply("aegis")
    assert not checker.json_rules_apply("mcp")


@pytest.fixture
def cockpit(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app) as client:
        bootstrap(client)
        yield client


def _coded(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["code"] in refusal_codes.REFUSAL_CODES


def test_the_palettes_refusals_carry_registered_codes(cockpit):
    client = cockpit
    _coded(client.get("/aegis/palette/actions?issue_ref=1"), 401, "unauthenticated")
    login(client, "a@e.com")
    _coded(client.get("/aegis/palette/actions?issue_ref=NOPE-999999"), 404, "not_found")
    # A command refusal maps its kind to the code a REST write answers with.
    blank = client.post("/aegis/palette/capture", data={"title": "   "})
    _coded(blank, 422, "validation_error")


def test_the_roster_replay_and_floor_json_refusals_carry_registered_codes(cockpit):
    client = cockpit
    _coded(client.get("/admin/fleet.json"), 401, "unauthenticated")
    add_user(client, "m@e.com", name="Mel")
    login(client, "m@e.com")
    _coded(client.get("/admin/fleet.json"), 403, "forbidden")
    _coded(client.get("/aegis/projects/999999/floor.json"), 404, "not_found")
    login(client, "a@e.com")
    _coded(client.get("/admin/agents/runs/no-such-run/replay.json"), 404, "not_found")
    project = client.post(
        "/projects", json={"name": "Ops", "key": "OPS"}, headers=ADMIN
    ).json()
    assert client.get(f"/aegis/projects/{project['id']}/floor.json").status_code == 200


def test_the_whole_tree_check_reads_a_cockpit_module_with_the_json_rules(tmp_path):
    checker = _checker()
    web = tmp_path / "web"
    web.mkdir()
    (web / "x.py").write_text(
        "def route():\n    return JSONResponse({'detail': 'gone'}, status_code=404)\n"
    )
    found = checker.check(tmp_path)
    assert any("web/x.py" in str(v) and '"code"' in str(v) for v in found), found
