"""Conventions the suite keeps: shared setup, and waits on conditions, not the clock.

Two drifts these stop. About a hundred test files each declared their own
``_bootstrap`` with small differences; ``tests/support.py`` is the shared start,
and the count of files still declaring their own may only fall. And
``pyproject.toml`` says the suite has no ``time.sleep``, which is why it runs
cleanly under ``pytest -n 4``: a sleep is a guess at how long something takes,
and a guess made on an idle machine is wrong on a loaded one. That claim is
enforced here rather than trusted.
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import support

TESTS = Path(__file__).resolve().parent

# Test files that still define their own ``_bootstrap``. It may only fall: a new
# test imports ``bootstrap`` and friends from tests/support.py, and a file that
# moves to them lowers this number in the same change.
BOOTSTRAP_REDECLARED = 93

# Files under tests/ allowed to call ``time.sleep``, each with the reason the wait
# cannot be on a condition. Keep it empty: wait on an event, a barrier or a
# bounded poll that names what it waited for.
SLEEP_ALLOWED: dict[str, str] = {}


def _python_files() -> list[Path]:
    return sorted(TESTS.rglob("*.py"))


def _defines(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    )


def test_new_tests_start_from_the_shared_support_module():
    redeclaring = [
        path.relative_to(TESTS).as_posix()
        for path in _python_files()
        if _defines(ast.parse(path.read_text(encoding="utf-8")), "_bootstrap")
    ]
    assert len(redeclaring) <= BOOTSTRAP_REDECLARED, (
        f"{len(redeclaring)} test files define their own _bootstrap; at most "
        f"{BOOTSTRAP_REDECLARED} may. Start a new test from tests/support.py "
        "(bootstrap, add_user, onboard_agent, mint_token, bearer, client_as, login, "
        "events) instead of declaring another copy."
    )
    assert len(redeclaring) >= BOOTSTRAP_REDECLARED, (
        f"only {len(redeclaring)} test files define their own _bootstrap now: lower "
        f"BOOTSTRAP_REDECLARED in tests/test_suite_conventions.py to "
        f"{len(redeclaring)} so the count cannot creep back up."
    )


def sleep_calls(source: str) -> list[int]:
    """Line numbers where ``source`` reaches ``time.sleep``, however it was imported."""
    tree = ast.parse(source)
    modules: set[str] = set()
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "time"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            functions.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"sleep", "*"}
            )
    if "*" in functions:
        functions.add("sleep")
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "sleep"
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ) or (
            isinstance(node, ast.Name)
            and node.id in functions
            and isinstance(node.ctx, ast.Load)
        ):
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("import time\ntime.sleep(1)\n", [2]),
        ("import time as clock\nclock.sleep(0.1)\n", [2]),
        ("from time import sleep\nsleep(1)\n", [2]),
        ("from time import sleep as nap\nnap(1)\n", [2]),
        ("from time import *\nsleep(1)\n", [2]),
        # Handing it over to be called later is the same wait.
        ("import time\nwait(on=time.sleep)\n", [2]),
        # Not a clock wait: another module's sleep, or a patched-out one.
        ("import asyncio\nawait asyncio.sleep(0)\n", []),
        ("import time\nmonkeypatch.setattr(time, 'sleep', lambda s: None)\n", []),
        ("import time\nstarted = time.monotonic()\n", []),
    ],
)
def test_the_sleep_check_sees_every_spelling(source, lines):
    assert sleep_calls(source) == lines


def test_no_test_waits_on_the_clock():
    found = {
        path.relative_to(TESTS).as_posix(): lines
        for path in _python_files()
        if (lines := sleep_calls(path.read_text(encoding="utf-8")))
    }
    unexplained = {
        path: lines for path, lines in found.items() if path not in SLEEP_ALLOWED
    }
    assert not unexplained, (
        f"time.sleep in tests/ at {unexplained}: wait on the condition instead (a "
        "threading.Event, a Barrier, or a bounded poll that fails naming what never "
        "arrived), or add the file to SLEEP_ALLOWED in "
        "tests/test_suite_conventions.py with the reason it cannot."
    )
    stale = sorted(set(SLEEP_ALLOWED) - set(found))
    assert not stale, f"SLEEP_ALLOWED names files with no time.sleep: {stale}"
    assert all(reason.strip() for reason in SLEEP_ALLOWED.values())


def test_pyproject_states_the_rule_this_file_enforces():
    pyproject = (TESTS.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "no time.sleep" in pyproject
    assert "tests/test_suite_conventions.py" in pyproject


# --- the shared module does what it says --------------------------------------------


def test_support_builds_accounts_tokens_and_clients_that_act_as_them(tmp_path):
    app, db_file = support.make_app(tmp_path)
    with TestClient(app) as client:
        admin = support.bootstrap(client)
        member = support.add_user(client, "m@e.com")
        agent = support.onboard_agent(client, scopes=["read"])
        token = support.mint_token(client, scopes=["read", "docs:write"])

        assert (admin["id"], admin["role"]) == (1, "admin")
        assert member["role"] == "member"
        assert token["scopes"] == ["read", "docs:write"]

        as_agent = support.client_as(app, token=agent)
        me = as_agent.get("/users/me").json()
        assert (me["id"], me["scopes"]) == (agent["user"]["id"], ["read"])
        refused = as_agent.post("/issues", json={"title": "not with read"})
        assert refused.status_code == 403

        as_member = support.client_as(app, user_id=member["id"])
        assert as_member.get("/users/me").json()["id"] == member["id"]

        with_token = support.client_as(app, token=token["token"])
        assert with_token.get("/users/me").json()["scopes"] == ["read", "docs:write"]

        assert client.get("/admin/run-controls").status_code == 401
        support.login(client, "a@e.com")  # the admin's browser session
        assert client.get("/admin/run-controls").status_code == 200

    minted = support.events(db_file, "minted_token")
    assert [event["actor_id"] for event in minted] == [1, 1]  # newest first
    assert all(event["verb"] == "minted_token" for event in minted)
    assert len(support.events(db_file)) > len(minted)


def test_support_bearer_reads_every_token_shape():
    onboarded = {"user": {"id": 2}, "token": {"token": "raw-secret"}}
    minted = {"id": 5, "token": "raw-secret"}
    expected = {"Authorization": "Bearer raw-secret"}
    assert support.bearer("raw-secret") == expected
    assert support.bearer(minted) == expected
    assert support.bearer(onboarded) == expected
    assert support.bearer(minted, run="run-1") == {**expected, "X-Athena-Run": "run-1"}


def test_support_client_as_takes_exactly_one_identity(tmp_path):
    app, _ = support.make_app(tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        support.client_as(app)
    with pytest.raises(ValueError, match="exactly one"):
        support.client_as(app, user_id=1, token="raw-secret")
