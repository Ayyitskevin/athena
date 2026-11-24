"""The operator is told what an instance answers without a credential (C11).

WHY (OPS-1, D7, D23): with the defaults 74 of 156 GET routes answered a caller
with no credential — comments, attachment bytes, lease generations, the
work-context packet, the activity CSV — and nothing said so. Tailnet validation
passed with the default, and ``athena-doctor`` was silent in every mode. D23
keeps the default (reads stay open), so the state is reported instead:

- tailnet mode refuses to start until ``ATHENA_ANONYMOUS_READS`` is set either
  way (D7): an instance other machines can reach must not be open by omission;
- ``athena-doctor`` prints the setting, how many GET routes answer without a
  credential, and which projects and spaces are public;
- the first start (``athena-serve --bootstrap``) prints one line saying what
  anonymous reads are.

The route count is computed from the app's own routes, asked the way each
route's identity dependency decides (``athena.exposure``). The tests here hold
that count to what the routes actually answer over HTTP, and show it follows a
route added at run time, so it can never be a number written down.
"""

from __future__ import annotations

import re
import subprocess
import sys

from fastapi import Depends
from fastapi.testclient import TestClient
import pytest

from athena import agent_openapi, config, ops
from athena.core import db, identity, users
from athena.main import AGENT_TRANSPORT, create_app

from support import ADMIN, bootstrap

_PATH_PARAMETER = re.compile(r"\{[^}]+\}")


def _route_exposure(app):
    from athena import exposure

    return exposure.route_exposure(app, AGENT_TRANSPORT)


def _get_routes_without_parameters(app):
    for route in agent_openapi.route_contexts(app):
        methods = getattr(route, "methods", None) or ()
        if "GET" in methods and not _PATH_PARAMETER.search(route.path):
            yield (
                route,
                agent_openapi.classify(
                    route, openapi_url=app.openapi_url, transport=AGENT_TRANSPORT
                ),
            )


def _admitted(route, kind, anonymous_reads: bool) -> bool:
    """What the route's own dependency decides, restated per kind for the check."""
    if kind == agent_openapi.BROWSER:
        return anonymous_reads or route.path in {
            "/login",
            "/login/sso",
            "/auth/callback",
            "/logout",
            "/healthz",
        }
    if not hasattr(route, "dependant"):
        return True
    needed = agent_openapi.authentication(route)
    return needed is None or (needed == "optional" and anonymous_reads)


@pytest.mark.parametrize("anonymous_reads", [True, False])
def test_the_count_is_what_the_credential_gate_admits_over_http(
    tmp_path, monkeypatch, anonymous_reads
):
    """The count equals the routes ``_admitted`` lets through, and ``_admitted``
    is held to HTTP: every GET route without a path parameter is asked with no
    credential, and the credential gate refuses exactly the ones it excludes
    (401 for a REST route; the browser gate's own 303 to ``/login`` for a page).

    That is what the gate admits, not what answers: a page's handler may still
    answer a signed-out viewer 401 with its own sign-in prompt, and a hidden or
    absent row reads 404. Only a 2xx answers. Every route that answers is
    admitted, some admitted routes do not answer, so the printed sentence says
    "past the credential gate" and never "answer"."""
    monkeypatch.setattr(config, "ANONYMOUS_READS", anonymous_reads)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    app = create_app(tmp_path / "count.db")
    counted = _route_exposure(app)

    gets = [
        (
            route,
            agent_openapi.classify(
                route, openapi_url=app.openapi_url, transport=AGENT_TRANSPORT
            ),
        )
        for route in agent_openapi.route_contexts(app)
        if "GET" in (getattr(route, "methods", None) or ())
    ]
    browser = [(r, k) for r, k in gets if k == agent_openapi.BROWSER]
    documents = [(r, k) for r, k in gets if not hasattr(r, "dependant")]
    rest = [(r, k) for r, k in gets if (r, k) not in browser + documents]
    assert (counted.browser_total, counted.documents, counted.rest_total) == (
        len(browser),
        len(documents),
        len(rest),
    )
    assert counted.browser == sum(_admitted(r, k, anonymous_reads) for r, k in browser)
    assert counted.rest == sum(_admitted(r, k, anonymous_reads) for r, k in rest)

    answering, admitted_not_answering = [], []
    with TestClient(app) as client:
        bootstrap(client)
        checked = 0
        for route, kind in _get_routes_without_parameters(app):
            response = client.get(route.path, follow_redirects=False)
            if 200 <= response.status_code < 300:
                answering.append(route.path)
                assert _admitted(route, kind, anonymous_reads), route.path
            elif _admitted(route, kind, anonymous_reads):
                admitted_not_answering.append((route.path, response.status_code))
            if kind == agent_openapi.BROWSER:
                refused_by_gate = (
                    response.status_code == 303
                    and response.headers.get("location") == "/login"
                )
            else:
                refused_by_gate = response.status_code == 401
            assert refused_by_gate is not _admitted(route, kind, anonymous_reads), (
                route.path,
                response.status_code,
            )
            checked += 1
    assert checked > 40
    assert answering
    if anonymous_reads:
        # 401 (a page's own sign-in prompt) and 404 are admitted, not answers.
        assert {status for _, status in admitted_not_answering} & {401, 404}
        assert counted.browser == counted.browser_total
        assert counted.rest > 40
    else:
        assert counted.browser == 3  # GET /login, /login/sso, /auth/callback
        assert counted.rest == 3  # /healthz, /readyz, /version


def test_the_count_follows_a_route_added_at_run_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANONYMOUS_READS", True)
    app = create_app(tmp_path / "added.db")
    before = _route_exposure(app)

    @app.get("/exposure-probe")
    def probe(actor: dict | None = Depends(identity.optional_actor)) -> dict:
        return {}

    @app.get("/exposure-probe-required")
    def probe_required(actor: dict = Depends(identity.current_actor)) -> dict:
        return {}

    after = _route_exposure(app)
    assert (after.rest, after.rest_total) == (before.rest + 1, before.rest_total + 2)

    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    closed = _route_exposure(app)
    assert closed.rest == 3 and closed.rest_total == after.rest_total


def _seeded(tmp_path):
    """An admin, a public and a private project, a public space."""
    db_file = tmp_path / "athena.db"
    with TestClient(create_app(db_file)) as client:
        bootstrap(client)
        for key in ("OPN", "SEC"):
            response = client.post(
                "/projects", json={"name": f"Project {key}", "key": key}, headers=ADMIN
            )
            assert response.status_code == 201, response.text
        response = client.post(
            "/spaces", json={"key": "ENG", "name": "Engineering"}, headers=ADMIN
        )
        assert response.status_code == 201, response.text
    conn = db.connect(db_file)
    conn.execute("UPDATE projects SET visibility = 'private' WHERE key = 'SEC'")
    conn.commit()
    conn.close()
    return db_file


def _anonymous_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("anonymous reads")]
    assert len(lines) == 1, output
    return lines[0]


def test_doctor_prints_the_setting_the_route_count_and_the_public_containers(
    tmp_path, monkeypatch, capsys
):
    db_file = _seeded(tmp_path)
    monkeypatch.setattr(config, "ANONYMOUS_READS", True)
    monkeypatch.setattr(config, "ANONYMOUS_READS_EXPLICIT", False, raising=False)

    assert ops.doctor_main([str(db_file)]) == 0

    line = _anonymous_line(capsys.readouterr().out)
    counted = _route_exposure(create_app(db_file))
    assert line.startswith(
        "anonymous reads in this environment: on (ATHENA_ANONYMOUS_READS is not "
        "set; the default is on); "
        f"{counted.anonymous} of {counted.total} GET routes let a caller with no "
        "credential past the credential gate"
    ), line
    assert "answer a caller" not in line
    assert "public containers: 1 of 2 projects (OPN), 1 of 1 spaces (ENG)" in line
    assert "SEC" not in line  # a private container is never named
    assert "set ATHENA_ANONYMOUS_READS=false" in line


def test_doctor_reports_closed_reads_in_deployment_mode(tmp_path, monkeypatch, capsys):
    db_file = _seeded(tmp_path)
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "ANONYMOUS_READS", False)
    monkeypatch.setattr(config, "ANONYMOUS_READS_EXPLICIT", True, raising=False)
    argv = [str(db_file), "--attach-dir", str(attach_dir), "--deployment"]
    assert ops.doctor_main(argv) == 0

    line = _anonymous_line(capsys.readouterr().out)
    closed = _route_exposure(create_app(db_file))
    assert line.startswith(
        "anonymous reads in this environment: off (ATHENA_ANONYMOUS_READS=false); "
        f"{closed.anonymous} of {closed.total} GET routes"
    ), line
    assert closed.anonymous < 10  # sign-in, health and the OpenAPI documents
    assert "every container and every read beside them needs a credential" in line


def _tailnet(monkeypatch, tmp_path, *, explicit: bool):
    db_file = tmp_path / "athena.db"
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir(exist_ok=True)
    if not db_file.exists():
        conn = db.connect(db_file)
        db.migrate(conn)
        users.create_user(
            conn,
            email="admin@example.com",
            name="Admin",
            password="password",
            role="admin",
            commit=True,
        )
        conn.close()
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "")
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "NETWORK_MODE", "tailnet")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ("athena.tailnet:8443",))
    monkeypatch.setattr(config, "ANON_RATE_LIMIT_PER_MINUTE", 120)
    monkeypatch.setattr(config, "ANONYMOUS_READS", True)
    monkeypatch.setattr(config, "ANONYMOUS_READS_EXPLICIT", explicit, raising=False)
    return db_file, attach_dir


def test_tailnet_refuses_to_start_until_anonymous_reads_is_set_explicitly(
    tmp_path, monkeypatch, capsys
):
    launched = []
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: launched.append(kwargs))
    listener = ["--host", "100.64.1.2", "--port", "8443"]

    _tailnet(monkeypatch, tmp_path, explicit=False)
    assert ops.serve_main(listener) == 1
    err = capsys.readouterr().err
    assert "ATHENA_ANONYMOUS_READS" in err and "tailnet" in err
    assert err.count("\n") == 1
    assert launched == []

    _tailnet(monkeypatch, tmp_path, explicit=True)
    assert ops.serve_main(listener) == 0
    assert launched == [{"host": "100.64.1.2", "port": 8443, "fd": None}]

    # D23: the default does not change, and local mode needs no explicit value.
    assert config.SETTINGS["ATHENA_ANONYMOUS_READS"].default is True
    _tailnet(monkeypatch, tmp_path, explicit=False)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    assert ops.serve_main([]) == 0
    assert launched[-1] == {"host": "127.0.0.1", "port": 8000, "fd": None}


def test_tailnet_deployment_doctor_refuses_the_implicit_default(
    tmp_path, monkeypatch, capsys
):
    db_file, attach_dir = _tailnet(monkeypatch, tmp_path, explicit=False)
    argv = [
        str(db_file),
        "--attach-dir",
        str(attach_dir),
        "--deployment",
        "--host",
        "100.64.1.2",
        "--port",
        "8443",
    ]
    assert ops.doctor_main(argv) == 1
    assert "ATHENA_ANONYMOUS_READS" in capsys.readouterr().err


def test_the_first_start_prints_one_line_saying_what_anonymous_reads_are(
    tmp_path, monkeypatch, capsys
):
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "athena.db")
    monkeypatch.setattr(config, "ATTACH_DIR", attach_dir)
    monkeypatch.setattr(config, "BOOTSTRAP_TOKEN", "b" * 32)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "NETWORK_MODE", "local")
    monkeypatch.setattr(config, "ALLOWED_AUTHORITIES", ())
    monkeypatch.setattr(config, "ANONYMOUS_READS", True)
    monkeypatch.setattr(config, "ANONYMOUS_READS_EXPLICIT", False, raising=False)
    monkeypatch.setattr(ops, "_run_server", lambda **kwargs: None)

    assert ops.serve_main(["--bootstrap"]) == 0

    line = _anonymous_line(capsys.readouterr().out)
    counted = _route_exposure(create_app(tmp_path / "x.db"))
    assert "without signing in" in line
    assert "issues with no project" in line and "fleet metrics" in line
    assert "set ATHENA_ANONYMOUS_READS=false" in line
    assert f"{counted.anonymous} of {counted.total} GET routes" in line
    assert "athena-doctor" in line


# --- review of the first build ----------------------------------------------


def test_a_space_key_cannot_forge_doctor_output(tmp_path, monkeypatch, capsys):
    """POST /spaces accepts a key with a newline, an escape sequence or 76
    characters (left as found). The doctor names a container only by a key
    that matches the project-key pattern, otherwise ``#<id>``, and caps the
    list, so no account can write a line of the operator's report."""
    db_file = tmp_path / "athena.db"
    forged = (
        "Q)\r\x1b[2K\x1b[1A\x1b[2Kanonymous reads: off; 8 of 161\n"
        "athena-doctor: ok\x1b[8m"
    )
    with TestClient(create_app(db_file)) as client:
        bootstrap(client)
        for key in [forged, "A" * 76] + [f"S{n}" for n in range(12)]:
            response = client.post(
                "/spaces", json={"key": key, "name": "space"}, headers=ADMIN
            )
            assert response.status_code == 201, response.text
    monkeypatch.setattr(config, "ANONYMOUS_READS", True)

    assert ops.doctor_main([str(db_file)]) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output and "\r" not in output
    assert output.count("athena-doctor: ok") == 1
    line = _anonymous_line(output)
    assert "#1" in line and "#2" in line and "A" * 76 not in line
    assert "and 4 more" in line


def test_reads_that_no_container_scopes_are_named_and_answer(
    tmp_path, monkeypatch, capsys
):
    """With every project and space private and reads on, a caller with no
    credential still reads issues with no project, labels, the activity feed
    and its CSV export, and fleet metrics — so "0 of 2 projects" alone would
    understate what is readable. Each named read is asked over HTTP here."""
    from athena import exposure

    db_file = tmp_path / "athena.db"
    monkeypatch.setattr(config, "ANONYMOUS_READS", True)
    monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", True)
    with TestClient(create_app(db_file)) as client:
        bootstrap(client)
        for key in ("ONE", "TWO"):
            client.post("/projects", json={"name": key, "key": key}, headers=ADMIN)
            client.post("/spaces", json={"key": key, "name": key}, headers=ADMIN)
        issue = client.post(
            "/issues", json={"title": "Loose end"}, headers=ADMIN
        ).json()
        client.post("/labels", json={"name": "triage"}, headers=ADMIN)
        conn = db.connect(db_file)
        conn.execute("UPDATE projects SET visibility = 'private'")
        conn.execute("UPDATE spaces SET visibility = 'private'")
        conn.commit()
        conn.close()
        monkeypatch.setattr(config, "TRUST_ACTOR_HEADER", False)
        for path in exposure.UNSCOPED_READS:
            assert client.get(path, follow_redirects=False).status_code == 200, path
        listed = client.get("/issues").json()["issues"]
        items = listed["items"] if isinstance(listed, dict) else listed
        assert issue["id"] in [item["id"] for item in items]

    assert ops.doctor_main([str(db_file)]) == 0

    line = _anonymous_line(capsys.readouterr().out)
    assert "public containers: 0 of 2 projects, 0 of 2 spaces" in line
    assert "issues with no project: 1" in line
    assert "labels, the activity feed and its CSV export, and fleet metrics" in line


# --- second review: the doctor counts without building the app ---------------


@pytest.mark.parametrize("anonymous_reads", [True, False])
def test_the_stored_route_counts_are_the_apps_own(
    tmp_path, monkeypatch, anonymous_reads
):
    """The doctor prints ``exposure.ROUTE_COUNTS`` so that it need not import
    the app (seconds). This recomputes the count from the app; when a route
    changes it, the failure prints the values to store."""
    from athena import exposure

    monkeypatch.setattr(config, "ANONYMOUS_READS", anonymous_reads)
    computed = _route_exposure(create_app(tmp_path / "count.db"))
    assert exposure.ROUTE_COUNTS[anonymous_reads] == computed, (
        f"store ROUTE_COUNTS[{anonymous_reads}] = {computed!r} in "
        "src/athena/exposure.py"
    )


def test_the_doctor_imports_no_app(tmp_path):
    db_file = tmp_path / "athena.db"
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.close()
    code = (
        "import sys\n"
        "from athena.ops import doctor_main\n"
        f"code = doctor_main([{str(db_file)!r}])\n"
        "print('EXIT', code, 'MAIN', 'athena.main' in sys.modules)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert "anonymous reads in this environment:" in done.stdout
    assert "EXIT 0 MAIN False" in done.stdout, done.stdout + done.stderr
