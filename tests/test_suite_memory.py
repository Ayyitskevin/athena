"""The suite must release each test's app, or the full gate cannot fit in memory.

Thousands of tests build an app with create_app(). If each one stays alive after
its test, memory grows linearly until xdist workers are killed and the gate
reports a few arbitrary "failed" tests instead of the cause. The release happens
in a conftest fixture's teardown, after the test body, so a test cannot observe
its own app being freed. This test watches from outside: it runs a generated
file of app-building tests under the real tests/conftest.py in a separate pytest
process, and that file's last test counts the FastAPI apps still alive.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


_CONFTEST = Path(__file__).resolve().parent / "conftest.py"
_APP_BUILDING_TESTS = 20

_GENERATED_TESTS = f"""
import gc

import fastapi
from fastapi.testclient import TestClient
import pytest

from athena.main import create_app


@pytest.mark.parametrize("index", range({_APP_BUILDING_TESTS}))
def test_builds_and_routes_an_app(tmp_path, index):
    with TestClient(create_app(tmp_path / "app.db")) as client:
        response = client.post("/users", json={{"email": "a@e.com", "name": "A"}})
        assert response.status_code == 201, response.text


def test_zz_previous_apps_were_released():
    # athena.main builds one module-level app at import; no test's app may remain.
    gc.collect()
    live = sum(isinstance(obj, fastapi.FastAPI) for obj in gc.get_objects())
    print(f"LIVE FASTAPI APPS: {{live}}")
    assert live <= 1, f"{{live}} FastAPI apps outlived their tests"
"""


def test_suite_releases_each_tests_app(tmp_path):
    shutil.copyfile(_CONFTEST, tmp_path / "conftest.py")
    generated = tmp_path / "test_generated_apps.py"
    generated.write_text(_GENERATED_TESTS, encoding="utf-8")
    # The child is a plain pytest run: no inherited options, xdist or coverage.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_", "COVERAGE_"))
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-s",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            str(generated),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert f"{_APP_BUILDING_TESTS + 1} passed" in result.stdout, result.stdout
    assert "LIVE FASTAPI APPS: " in result.stdout
