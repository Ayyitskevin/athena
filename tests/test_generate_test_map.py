"""The changed profile's app-reached map is generated from per-test coverage (G7).

WHY: `scripts/select_tests.py` cannot follow imports through the app factory,
so a test that reaches a router or a helper only through the app is selected
by the map alone. A map kept by hand matched tests by file name and silently
missed a route used as setup by a test named for something else. These tests
hold `scripts/generate_test_map.py` to the rules its docstring states, on
coverage data built here with coverage's own API (and once, end to end, on a
three-module checkout), and hold the scheduled job that keeps the committed
map honest to being read-only and to running the check. Nothing here records
coverage of the real suite.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys

from coverage import CoverageData
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "athena_generate_test_map_under_test", ROOT / "scripts" / "generate_test_map.py"
)
assert _SPEC is not None and _SPEC.loader is not None
generator = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = generator
_SPEC.loader.exec_module(generator)
_SHARDS_SPEC = importlib.util.spec_from_file_location(
    "athena_coverage_shards_for_map", ROOT / "scripts" / "coverage_shards.py"
)
assert _SHARDS_SPEC is not None and _SHARDS_SPEC.loader is not None
coverage_shards = importlib.util.module_from_spec(_SHARDS_SPEC)
_SHARDS_SPEC.loader.exec_module(coverage_shards)

WORKFLOW = ROOT / ".github" / "workflows" / "test-map.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PINNED = re.compile(r"[^@\s]+@[0-9a-f]{40}")

# A miniature checkout: the app factory imports two routers and the middleware;
# test_helper.py and test_direct.py import what they test, every other test
# builds the app.
TREE = {
    "src/athena/__init__.py": "",
    "src/athena/main.py": (
        "from athena import middleware\n"
        "from athena.core import widgets_api, gadgets_api\n"
    ),
    "src/athena/middleware.py": "",
    "src/athena/core/__init__.py": "",
    "src/athena/core/helper.py": "",
    "src/athena/core/constants.py": "",
    "src/athena/core/widgets_api.py": "from athena.core import helper\n",
    "src/athena/core/gadgets_api.py": "",
    "src/athena/core/direct.py": "",
    "tests/test_helper.py": "from athena.core import helper\n",
    "tests/test_direct.py": "from athena.core import direct\n",
    "tests/test_widgets.py": "from athena.main import app\n",
    "tests/test_setup_uses_widgets.py": "from athena.main import app\n",
    "tests/test_gadgets.py": "from athena.main import app\n",
}

# context -> {source file: lines it ran}. Contexts are pytest-cov's
# "<node id>|<phase>"; "" is import time, which no test owns.
CONTEXTS = {
    "": {
        "src/athena/core/constants.py": [1],
        "src/athena/core/helper.py": [1],
        "src/athena/main.py": [1, 2],
    },
    "tests/test_helper.py::test_direct|run": {
        "src/athena/core/helper.py": [2],
        "src/athena/middleware.py": [1],
    },
    "tests/test_widgets.py::test_list[a::b]|run": {
        "src/athena/core/widgets_api.py": [3],
        "src/athena/core/helper.py": [2],
        "src/athena/middleware.py": [1],
        "src/athena/main.py": [3],
    },
    # A route used as setup by a test named for something else: the miss a
    # map of file names makes and a recorded map does not.
    "tests/test_setup_uses_widgets.py::TestFlow::test_flow|setup": {
        "src/athena/core/widgets_api.py": [3],
        "src/athena/middleware.py": [1],
    },
    "tests/test_gadgets.py::test_gadget|teardown": {
        "src/athena/core/gadgets_api.py": [4],
        "src/athena/middleware.py": [1],
    },
    "tests/test_direct.py::test_direct|call": {
        "src/athena/core/direct.py": [1],
        "src/athena/middleware.py": [1],
    },
    # Not a test file: a context the generator must ignore.
    "scripts/tool.py::helper|run": {"src/athena/core/gadgets_api.py": [9]},
}

EXPECTED = {
    "about": generator.ABOUT,
    "recorded_test_files": 5,
    "ubiquitous_share": 0.5,
    # Run by all five recorded test files: no entry, listed with its count.
    "ubiquitous": {"src/athena/middleware.py": 5},
    "map": {
        # Recorded, and the one test that ran it imports it: the graph's.
        "src/athena/core/direct.py": [],
        "src/athena/core/gadgets_api.py": ["tests/test_gadgets.py"],
        # test_helper.py imports it, so the graph selects that one already.
        "src/athena/core/helper.py": ["tests/test_widgets.py"],
        "src/athena/core/widgets_api.py": [
            "tests/test_setup_uses_widgets.py",
            "tests/test_widgets.py",
        ],
        # constants.py ran only at import time; main.py is a safety valve.
    },
}


def build_tree(root: Path) -> Path:
    for relative, text in TREE.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def record(path: Path, contexts: dict[str, dict[str, list[int]]]) -> Path:
    """A coverage data file with per-test contexts, as coverage.py writes one."""
    data = CoverageData(basename=str(path))
    for context, files in contexts.items():
        data.set_context(context)
        data.add_arcs(
            {
                source: [(-1, lines[0])] + list(zip(lines, lines[1:], strict=False))
                for source, lines in files.items()
            }
        )
    data.write()
    return path


def generated(root: Path, *data_files: Path) -> dict:
    return json.loads(generator.generate(data_files, root))


def test_contexts_become_the_tests_that_reach_each_file_through_the_app(tmp_path):
    root = build_tree(tmp_path / "checkout")
    data = record(tmp_path / ".coverage", CONTEXTS)
    assert generated(root, data) == EXPECTED


def test_the_map_is_the_same_whichever_way_the_data_is_split(tmp_path):
    root = build_tree(tmp_path / "checkout")
    whole = generator.generate([record(tmp_path / "whole", CONTEXTS)], root)
    names = sorted(CONTEXTS)
    first = record(tmp_path / "one", {n: CONTEXTS[n] for n in names[::2]})
    second = record(tmp_path / "two", {n: CONTEXTS[n] for n in names[1::2]})
    # CI records four shards; their order must not change a byte.
    assert generator.generate([second, first], root) == whole
    assert generator.generate([first, second], root) == whole
    assert whole.endswith("}\n")
    assert whole == generator.render(json.loads(whole))
    document = json.loads(whole)
    assert list(document["map"]) == sorted(document["map"])


def test_data_recorded_without_per_test_contexts_is_refused(tmp_path):
    root = build_tree(tmp_path / "checkout")
    data = record(tmp_path / ".coverage", {"": CONTEXTS[""]})
    with pytest.raises(generator.MapError, match="--cov-context=test"):
        generator.generate([data], root)


def test_check_fails_on_a_stale_map_and_passes_on_a_fresh_one(tmp_path, capsys):
    root = build_tree(tmp_path / "checkout")
    (root / "scripts").mkdir()
    data = str(record(tmp_path / ".coverage", CONTEXTS))
    committed = root / generator.MAP
    stale = dict(
        EXPECTED,
        map={
            "src/athena/core/widgets_api.py": EXPECTED["map"][
                "src/athena/core/widgets_api.py"
            ][1:]
        },
    )
    committed.write_text(generator.render(stale))
    artifact = tmp_path / "artifact" / "test_map.json"

    assert (
        generator.main(["--check", "--data", data, "--output", str(artifact)], root)
        == 1
    )
    output = capsys.readouterr().out
    assert '+      "tests/test_setup_uses_widgets.py",\n' in output
    assert "test map is stale" in output
    # --check writes nothing to the committed map, and the artifact is fresh.
    assert committed.read_text() == generator.render(stale)
    assert json.loads(artifact.read_text()) == EXPECTED

    assert generator.main(["--data", data], root) == 0
    assert json.loads(committed.read_text()) == EXPECTED
    capsys.readouterr()
    assert generator.main(["--check", "--data", data], root) == 0
    assert "test map is current: 4 source files, 4 entries" in capsys.readouterr().out


def test_a_broken_committed_map_is_replaced_not_read(tmp_path):
    # The selector refuses an unreadable map; regenerating is how it is fixed.
    root = build_tree(tmp_path / "checkout")
    (root / "scripts").mkdir()
    (root / generator.MAP).write_text('{"map": ')
    data = str(record(tmp_path / ".coverage", CONTEXTS))
    assert generator.main(["--data", data], root) == 0
    assert json.loads((root / generator.MAP).read_text()) == EXPECTED


RECORDED_CHECKOUT = {
    "pyproject.toml": (
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n'
        "[tool.coverage.run]\nbranch = true\nrelative_files = true\n"
        'source = ["src/athena"]\n'
    ),
    "src/athena/__init__.py": "",
    "src/athena/main.py": (
        "from athena import widgets\n\n\ndef app():\n    return widgets.render()\n"
    ),
    "src/athena/widgets.py": "def render():\n    return 1\n",
    "src/athena/shared.py": "def value():\n    return 2\n",
    **{
        f"tests/test_app_{n}.py": (
            "from athena.main import app\n\n\ndef test_app():\n    assert app() == 1\n"
        )
        for n in range(3)
    },
    **{
        f"tests/test_shared_{n}.py": (
            "from athena import shared\n\n\n"
            "def test_shared():\n    assert shared.value() == 2\n"
        )
        for n in range(3)
    },
}


def _without_outer_run(monkeypatch, root):
    # The nested pytest must not inherit this run's options, xdist or coverage.
    for key in list(os.environ):
        if key.startswith(("PYTEST_", "COV_CORE_", "COVERAGE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PYTHONPATH", str(root / "src"))
    monkeypatch.setenv("ATHENA_COVERAGE_WORKERS", "1")


def test_record_is_one_command_and_never_maps_a_failing_run(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "checkout"
    for relative, text in RECORDED_CHECKOUT.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    (root / "scripts").mkdir()
    _without_outer_run(monkeypatch, root)

    assert generator.main(["--record"], root) == 0
    document = json.loads((root / generator.MAP).read_text())
    # The test_app files reach widgets.py only through the app factory; the
    # test_shared files import shared.py, so the graph is its whole answer.
    # Each ran in exactly half of the six recorded test files: not more than
    # half, so both keep their entries.
    assert document["map"] == {
        "src/athena/shared.py": [],
        "src/athena/widgets.py": [f"tests/test_app_{n}.py" for n in range(3)],
    }
    assert document["recorded_test_files"] == 6
    assert document["ubiquitous"] == {}

    (root / generator.MAP).unlink()
    (root / "tests/test_broken.py").write_text("def test_broken():\n    assert 0\n")
    capsys.readouterr()
    assert generator.main(["--record"], root) == 1
    assert "pytest failed" in capsys.readouterr().err
    assert not (root / generator.MAP).exists()


@pytest.mark.parametrize("contexts", [False, True])
def test_the_shard_runner_records_per_test_contexts_only_when_asked(
    tmp_path, monkeypatch, contexts
):
    # CI's required coverage shards record none; the Test map job asks.
    arguments = []
    monkeypatch.setenv("COVERAGE_FILE", os.environ.get("COVERAGE_FILE", "unset"))
    monkeypatch.setattr(
        coverage_shards, "collect", lambda path: [f"t.py::test_{n}" for n in range(4)]
    )
    monkeypatch.setattr(
        coverage_shards.pytest,
        "main",
        lambda args, plugins: arguments.extend(args) or 1,
    )
    with pytest.raises(ValueError, match="pytest failed"):
        coverage_shards.run(0, tmp_path / "shard", contexts)
    assert ("--cov-context=test" in arguments) is contexts
    assert "--cov" in arguments


def test_the_committed_map_is_the_generators_own_output():
    # The real map is read by every changed-profile run; it must be what the
    # generator writes (sorted, deterministic), not something edited by hand.
    text = (ROOT / generator.MAP).read_text()
    document = json.loads(text)
    assert text == generator.render(document)
    assert set(document) == set(EXPECTED)
    limit = document["recorded_test_files"] * document["ubiquitous_share"]
    for source, tests in document["map"].items():
        assert source.startswith("src/athena/") and source.endswith(".py"), source
        assert generator.select_tests.valve(source) is None, source
        assert tests == sorted(set(tests)) and len(tests) <= limit, source
        assert all(generator.select_tests._is_test_file(test) for test in tests)
    assert all(count > limit for count in document["ubiquitous"].values())
    assert not set(document["ubiquitous"]) & set(document["map"])


# --- the scheduled job -------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(job):
    return _workflow()["jobs"][job]["steps"]


def _uses(workflow):
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]


def test_the_scheduled_job_runs_on_a_schedule_and_on_demand_only():
    # PyYAML reads the key "on" as True. Not on push or pull requests: the
    # ordinary gate never records coverage.
    triggers = _workflow()[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert [len(entry["cron"].split()) for entry in triggers["schedule"]] == [5]


def test_the_scheduled_job_is_read_only_and_pins_every_action():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in workflow["jobs"].values())
    text = WORKFLOW.read_text()
    for forbidden in ("write", "git push", "gh pr", "secrets.", "pull_request"):
        assert forbidden not in text, forbidden
    for job in workflow["jobs"].values():
        checkout = job["steps"][0]
        assert checkout["uses"].startswith("actions/checkout@")
        assert checkout["with"] == {"persist-credentials": False}
    # The same pinned commits as CI's own steps, never a movable tag.
    assert all(PINNED.fullmatch(reference) for reference in _uses(workflow))
    assert set(_uses(workflow)) <= set(_uses(yaml.safe_load(CI.read_text())))


def test_the_scheduled_job_sets_up_exactly_as_ci_does():
    ci = yaml.safe_load(CI.read_text())["jobs"]
    record = _steps("record")
    # Checkout, Python, the venv, the hash-verified installer, the pinned
    # extras and their verification: the coverage shards' first six steps.
    assert record[:6] == ci["coverage"]["steps"][:6]
    assert record[5]["name"] == "Verify pinned dependency set"
    setup = [
        s
        for s in ci["test"]["steps"]
        if s.get("name") != "Require every shard to succeed"
    ]
    assert _steps("map")[:5] == setup[:5]


def test_the_scheduled_job_records_every_shard_and_runs_the_check():
    workflow = _workflow()
    shards = list(range(coverage_shards.SHARDS))
    assert workflow["jobs"]["record"]["strategy"]["matrix"]["shard"] == shards
    assert workflow["jobs"]["map"]["needs"] == ["record"]
    runs = [step.get("run", "") for step in _steps("record")]
    assert any("coverage_shards.py run --contexts" in run for run in runs)
    check = next(
        step["run"]
        for step in _steps("map")
        if "generate_test_map.py" in step.get("run", "")
    )
    assert "--check" in check.split()
    for shard in shards:
        assert f"test-map-shard-{shard}/.coverage" in check
    upload = _steps("map")[-1]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["if"] == "always()"
    assert upload["with"]["path"].endswith("/test-map/test_map.json")
    assert '--output "$RUNNER_TEMP/test-map/test_map.json"' in check
