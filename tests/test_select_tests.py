"""The changed profile runs only the tests a change can reach, and says why.

`scripts/select_tests.py` is the inner loop's judgment: a test it leaves out is
a test nobody runs until the product gate. These tests pin each rule on small
synthetic trees, pin the map against the real tree so it cannot rot silently,
and check that real one-file changes select the tests a person would expect.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "athena_select_tests_under_test", ROOT / "scripts" / "select_tests.py"
)
assert _SPEC is not None and _SPEC.loader is not None
select_tests = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = select_tests
_SPEC.loader.exec_module(select_tests)

# A miniature checkout. Every module body is an import statement or plain data:
# selection must never execute any of it.
TREE = {
    "src/athena/__init__.py": "",
    "src/athena/main.py": (
        "from athena.core import widgets_api\nfrom athena.web import router\n"
    ),
    "src/athena/core/__init__.py": "",
    "src/athena/core/db.py": "",
    "src/athena/core/base.py": "raise SystemExit('selection imported a module')\n",
    "src/athena/core/mid.py": "from athena.core.base import VALUE\n",
    "src/athena/core/rel.py": "from . import base\n",
    "src/athena/core/rel2.py": "from .mid import VALUE\n",
    "src/athena/core/lazy.py": "def load():\n    from athena.core import base\n",
    "src/athena/core/widgets_api.py": "import athena.core.mid\n",
    "src/athena/web/__init__.py": "from .router import router\n",
    "src/athena/web/router.py": "from athena.web import page\n",
    "src/athena/web/page.py": "",
    "src/athena/templates/card.html": "<p></p>\n",
    "tests/conftest.py": "",
    "tests/helper.py": "from athena.core import rel\n",
    "tests/test_base.py": "from athena.core import base\n",
    "tests/test_mid.py": "import athena.core.mid\n",
    "tests/test_rel2.py": "from athena.core.rel2 import VALUE\n",
    "tests/test_lazy.py": "from athena.core import lazy\n",
    "tests/test_uses_helper.py": "import helper\n",
    "tests/test_app.py": "from athena.main import create_app\n",
    "tests/test_widgets.py": "from athena.main import create_app\n",
    "tests/test_web.py": "from athena.main import create_app\n",
    "tests/test_patch.py": (
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr('athena.core.db.PATH', None)\n"
    ),
    "tests/test_card.py": "TEMPLATE = 'card.html'\n",
    "tests/test_guide.py": (
        "from pathlib import Path\n"
        "ROOT = Path(__file__).parents[1]\n"
        "GUIDE = ROOT / 'docs' / 'GUIDE.md'\n"
    ),
    "tests/test_fixtures.py": (
        "from pathlib import Path\n"
        "DATA = Path(__file__).parent / 'fixtures' / 'cases'\n"
    ),
    "tests/fixtures/cases/one.json": "{}\n",
    # The generated map: what recorded coverage saw run through the app. It
    # names a test that no longer exists, as a map recorded before a deletion
    # does.
    "scripts/test_map.json": json.dumps(
        {
            "about": "",
            "recorded_test_files": 4,
            "ubiquitous_share": 0.5,
            "ubiquitous": {},
            "map": {
                "src/athena/core/widgets_api.py": [
                    "tests/test_deleted.py",
                    "tests/test_widgets.py",
                ],
                "src/athena/web/page.py": ["tests/test_web.py"],
            },
        }
    ),
    "docs/GUIDE.md": "# Guide\n",
    "docs/NOTES.md": "# Notes\n",
    "data.txt": "x\n",
}


def build(tmp_path, overrides=None):
    for relative, text in {**TREE, **(overrides or {})}.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def selected(tmp_path, *changed, overrides=None):
    plan = select_tests.select(build(tmp_path, overrides), changed)
    assert not plan.full_suite, plan.full_suite
    return {Path(path).name for path in plan.test_files}


def test_direct_and_transitive_imports_select_their_tests(tmp_path):
    # base.py raises when imported: reaching these answers proves nothing ran.
    tests = selected(tmp_path, "src/athena/core/base.py")
    assert {
        "test_base.py",  # from athena.core import base
        "test_mid.py",  # mid: from athena.core.base import VALUE
        "test_rel2.py",  # rel2: from .mid import ..., two steps away
        "test_uses_helper.py",  # helper -> rel: from . import base
        "test_lazy.py",  # an import inside a function still counts
    } <= tests
    assert "test_app.py" not in tests


def test_relative_import_resolves_against_the_package(tmp_path):
    assert selected(tmp_path, "src/athena/core/mid.py") >= {"test_rel2.py"}
    assert "test_base.py" not in selected(tmp_path, "src/athena/core/mid.py")


def test_a_changed_test_selects_itself_only(tmp_path):
    assert selected(tmp_path, "tests/test_mid.py") == {"test_mid.py"}


def test_a_changed_helper_selects_the_tests_that_import_it(tmp_path):
    assert selected(tmp_path, "tests/helper.py") == {"test_uses_helper.py"}


def test_a_module_named_in_a_test_string_is_an_import(tmp_path):
    # monkeypatch.setattr("athena.core.db.PATH", ...) depends on athena.core.db.
    plan = select_tests.select(build(tmp_path), ["src/athena/core/mid.py"])
    index = select_tests.Index(tmp_path)
    assert "test_patch" in index.importers["athena.core.db"]
    assert "tests/test_patch.py" not in plan.test_files


def test_composition_roots_are_not_followed(tmp_path):
    # page.py reaches every test through athena.web.router and athena.main;
    # only the map's browser tests are selected, not every app test.
    tests = selected(tmp_path, "src/athena/web/page.py")
    assert "test_web.py" in tests
    assert "test_app.py" not in tests
    assert "test_widgets.py" not in tests


def test_the_http_map_selects_tests_the_graph_cannot_see(tmp_path):
    # From the generated map; the test it names that no longer exists is not run.
    plan = select_tests.select(build(tmp_path), ["src/athena/core/widgets_api.py"])
    assert plan.test_files == ("tests/test_widgets.py",)
    assert dict(plan.by_rule)["map"] == 1
    assert dict(plan.by_rule)["graph"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "constraints/ci-py312.txt",
        "tests/conftest.py",
        "src/athena/core/db.py",
        "src/athena/core/migrations/0001_init.sql",
        "src/athena/main.py",
        "src/athena/web/__init__.py",
        "src/athena/web/router.py",
        "scripts/gate.py",
        "scripts/gate.sh",
        "scripts/select_tests.py",
    ],
)
def test_safety_valves_select_the_full_suite_and_say_so(tmp_path, path):
    plan = select_tests.select(build(tmp_path), ["src/athena/core/mid.py", path])
    assert plan.test_files == tuple(sorted(f"tests/{t}" for t in _test_names()))
    assert len(plan.full_suite) == 1
    assert plan.full_suite[0].startswith(f"{path}: safety valve (")
    assert f"{path}: FULL SUITE, safety valve: " in "\n".join(plan.explanations)


def _test_names():
    return [Path(p).name for p in TREE if p.startswith("tests/test_")]


def test_a_module_only_the_app_imports_and_the_map_omits_runs_everything(tmp_path):
    overrides = {
        "src/athena/main.py": "from athena import middleware\n",
        "src/athena/middleware.py": "from athena.core import mid\n",
        "tests/test_middleware.py": "from athena import middleware\n",
    }
    plan = select_tests.select(build(tmp_path, overrides), ["src/athena/middleware.py"])
    assert plan.full_suite == (
        "src/athena/middleware.py: unmapped (only a composition root imports it "
        "and the map does not name its tests)",
    )
    # A change that reaches it is just as unbounded.
    plan = select_tests.select(tmp_path, ["src/athena/core/base.py"])
    assert plan.full_suite == (
        "src/athena/core/base.py: unmapped (it reaches athena.middleware, which "
        "only a composition root imports and the map does not name its tests)",
    )


def _map(ubiquitous=None, entries=None):
    return json.dumps(
        {
            "about": "",
            "recorded_test_files": 4,
            "ubiquitous_share": 0.5,
            "ubiquitous": ubiquitous or {},
            "map": entries or {},
        }
    )


def test_a_file_most_recorded_tests_run_runs_everything_when_it_changes(tmp_path):
    # Every recorded test ran the middleware, so the map leaves it out: naming
    # nearly the whole suite tells the selector nothing, and any test can be
    # affected by a change to it.
    overrides = {
        "src/athena/main.py": "from athena import middleware\n",
        "src/athena/middleware.py": "from athena.core import mid\n",
        "scripts/test_map.json": _map({"src/athena/middleware.py": 4}),
    }
    plan = select_tests.select(build(tmp_path, overrides), ["src/athena/middleware.py"])
    assert plan.full_suite == (
        "src/athena/middleware.py: unmapped (4 of the 4 recorded test files run "
        "it, so the map leaves it out)",
    )
    # A change the graph merely carries to it is bounded by its own tests.
    plan = select_tests.select(tmp_path, ["src/athena/core/base.py"])
    assert not plan.full_suite
    assert "tests/test_base.py" in plan.test_files
    assert "tests/test_app.py" not in plan.test_files


def test_a_module_reached_only_through_the_app_and_never_recorded_runs_everything(
    tmp_path,
):
    # A router added since the map was recorded: which tests reach it is
    # unknown until the map is regenerated, so the full suite runs, not none.
    build(tmp_path, {"scripts/test_map.json": _map()})
    plan = select_tests.select(tmp_path, ["src/athena/core/widgets_api.py"])
    assert plan.full_suite == (
        "src/athena/core/widgets_api.py: unmapped (only a composition root "
        "imports it and the map does not name its tests)",
    )
    # With no map file at all (a checkout that has none), the same holds.
    (tmp_path / "scripts/test_map.json").unlink()
    plan = select_tests.select(tmp_path, ["src/athena/core/widgets_api.py"])
    assert plan.full_suite == (
        "src/athena/core/widgets_api.py: unmapped (only a composition root "
        "imports it and the map does not name its tests)",
    )


def test_a_recorded_module_its_importing_tests_cover_is_bounded_by_the_graph(
    tmp_path,
):
    # An empty entry: recorded, and every test that ran it imports it.
    overrides = {
        "tests/test_widgets.py": "from athena.core import widgets_api\n",
        "scripts/test_map.json": _map(entries={"src/athena/core/widgets_api.py": []}),
    }
    plan = select_tests.select(
        build(tmp_path, overrides), ["src/athena/core/widgets_api.py"]
    )
    assert not plan.full_suite
    assert plan.test_files == ("tests/test_widgets.py",)
    assert dict(plan.by_rule) == {"graph": 1, "map": 0, "reference": 0}


def test_an_unreadable_map_stops_the_selection(tmp_path):
    broken = _map(entries={"src/athena/core/widgets_api.py": "tests/test_widgets.py"})
    with pytest.raises(
        select_tests.SelectionError, match="test_map.json is unreadable"
    ):
        select_tests.select(
            build(tmp_path, {"scripts/test_map.json": broken}),
            ["src/athena/core/mid.py"],
        )


def test_an_unmapped_file_runs_the_full_suite_and_names_it(tmp_path):
    plan = select_tests.select(build(tmp_path), ["data.txt"])
    assert len(plan.test_files) == plan.total_test_files
    assert plan.full_suite == (
        "data.txt: unmapped (no import, map entry or reference reaches it)",
    )


def test_a_document_selects_the_tests_that_read_it(tmp_path):
    assert selected(tmp_path, "docs/GUIDE.md") == {"test_guide.py"}


def test_a_document_no_test_names_selects_nothing_and_says_so(tmp_path):
    plan = select_tests.select(build(tmp_path), ["docs/NOTES.md"])
    assert plan.test_files == ()
    assert plan.full_suite == ()
    assert plan.explanations == (
        "docs/NOTES.md: no test selected (a document: tests that read one name it "
        "and are selected by reference)",
    )


def test_a_template_selects_browser_tests_and_tests_that_name_it(tmp_path):
    tests = selected(tmp_path, "src/athena/templates/card.html")
    assert {"test_web.py", "test_card.py"} <= tests
    assert "test_app.py" not in tests


def test_a_file_in_a_directory_a_test_reads_selects_that_test(tmp_path):
    # Path(__file__).parent / "fixtures" / "cases" reads every file in it.
    assert selected(tmp_path, "tests/fixtures/cases/two.json") == {"test_fixtures.py"}


def test_a_deleted_module_still_selects_the_tests_that_import_it(tmp_path):
    build(tmp_path)
    (tmp_path / "src/athena/core/mid.py").unlink()
    plan = select_tests.select(tmp_path, ["src/athena/core/mid.py"])
    assert not plan.full_suite
    assert "tests/test_mid.py" in plan.test_files


# --- the map against the real tree ------------------------------------------


def _real_files():
    return sorted(
        path.relative_to(ROOT).as_posix()
        for top in ("src", "tests", "scripts", "docs")
        for path in (ROOT / top).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ) + sorted(path.name for path in ROOT.glob("*.md"))


def _real_tests():
    return [path for path in _real_files() if select_tests._is_test_file(path)]


@pytest.mark.parametrize("entry", select_tests.TEST_MAP, ids=lambda entry: entry.source)
def test_every_map_entry_still_matches_real_files(entry):
    sources = [
        path for path in _real_files() if select_tests.matches(entry.source, path)
    ]
    assert sources, f"{entry.source} matches no file"
    tests = _real_tests()
    for glob in entry.tests:
        expanded = {
            glob.replace("{name}", match.group("name")) if "{name}" in glob else glob
            for match in (select_tests.matches(entry.source, p) for p in sources)
            if match is not None
        }
        assert any(
            select_tests.matches(pattern, test)
            for pattern in expanded
            for test in tests
        ), f"{entry.source}: {glob} matches no test file"


def test_every_module_only_the_app_reaches_is_mapped_or_runs_everything(real_index):
    # Routers, pages and the app's own helpers: the tests that reach them come
    # from the recorded map, or, for one it does not name (new since the map
    # was recorded, or run by most tests), the full suite runs. None may select
    # only what the graph or a reference happens to find.
    index = real_index
    app_only = [
        path
        for module, path in index.module_paths.items()
        if path.startswith("src/")
        and index.only_roots_import(module)
        and select_tests.valve(path) is None
    ]
    assert len(app_only) > 50
    for path in app_only:
        plan = select_tests.select(ROOT, [path], index)
        assert plan.full_suite or path in index.generated.tests, path
        if not plan.full_suite:
            assert set(index.generated.tests[path]) & set(index.test_files) <= set(
                plan.test_files
            ), path


@pytest.mark.parametrize(
    "path", ["src/athena/middleware.py", "src/athena/request_log.py"]
)
def test_the_request_wrappers_run_the_full_suite(real_index, path):
    # Every request passes through both (the request log since H15), so the
    # recorded coverage sees nearly every test run them and the map leaves them
    # out: a change to either knowingly runs the full suite. agent_openapi.py
    # stays out of the middleware's import graph for this reason.
    generated = real_index.generated
    assert path in generated.ubiquitous
    plan = select_tests.select(ROOT, [path], real_index)
    assert plan.full_suite == (
        f"{path}: unmapped ({generated.ubiquitous[path]} of the "
        f"{generated.recorded} recorded test files run it, so the map leaves it out)",
    )


# --- real one-file changes ---------------------------------------------------


@pytest.fixture(scope="module")
def real_index():
    return select_tests.Index(ROOT)


@pytest.fixture
def real(real_index):
    def plan_for(path):
        plan = select_tests.select(ROOT, [path], real_index)
        assert not plan.full_suite, plan.full_suite
        return plan, {Path(test).stem for test in plan.test_files}

    return plan_for


def test_a_router_change_selects_its_route_tests(real):
    plan, tests = real("src/athena/aegis/claims_api.py")
    assert {
        "test_claim_handoffs",
        "test_claim_loop_nine",
        "test_lease_expiry_boundary",
        "test_atomic_pickup",
        "test_delegation_lease",
        "test_issue_contributors",
    } <= tests
    # Bounded, not the full suite: the route tests come from the recorded map
    # (every test file that called a claim route), which is under the ubiquity
    # share by construction. How many that is, is the recording's answer.
    assert len(plan.test_files) < plan.total_test_files
    assert any(" map " in line for line in plan.explanations), plan.explanations


def test_a_shared_helper_change_selects_every_surface_that_uses_it(real_index):
    # By import, at any distance short of the app factory. (Whether the map
    # adds more, or most recorded tests run it and the full suite runs, is the
    # recorded coverage's answer.)
    reached = real_index.reached("athena.core.etag")
    tests = {Path(real_index.module_paths[module]).stem for module in reached}
    assert {"test_etag", "test_issue_etags", "test_page_etags"} <= tests


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "src/athena/aegis/dashboard.py",
            {"test_dashboard", "test_access_dashboard", "test_web"},
        ),
        (
            "src/athena/aegis/notification_state_api.py",
            {"test_notifications", "test_inbox_target_state"},
        ),
    ],
)
def test_code_reached_only_through_the_app_selects_the_tests_that_ran_it(
    real, path, expected
):
    # None of these tests imports the module: the app does. The recorded map is
    # how they are found (G7), not their file names.
    plan, tests = real(path)
    assert expected <= tests
    assert dict(plan.by_rule)["map"] >= len(expected)
    assert len(plan.test_files) < plan.total_test_files / 3


def test_a_document_change_selects_the_tests_that_read_it(real):
    plan, tests = real("docs/AGENT_GUIDE.md")
    assert {"test_agent_guide", "test_doc_examples"} <= tests
    assert len(plan.test_files) < 10


def test_a_real_test_change_selects_itself_and_its_readers_only(real):
    plan, tests = real("tests/test_etag.py")
    assert dict(plan.by_rule)["graph"] == 1
    assert dict(plan.by_rule)["map"] == 0
    # Anything else is a test that names the file (this module does).
    readers = {
        path.stem
        for path in (ROOT / "tests").glob("test_*.py")
        if "tests/test_etag.py" in path.read_text()
    }
    assert "test_select_tests" in readers
    assert tests == {"test_etag"} | readers


# --- the diff ----------------------------------------------------------------


def _git(repo, *arguments):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=selection test",
            "-c",
            "user.email=selection-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repository(tmp_path):
    for name in ("kept", "committed", "staged", "unstaged", "deleted", "ignored"):
        (tmp_path / f"{name}.txt").write_text("before\n")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "work")
    (tmp_path / "committed.txt").write_text("after\n")
    _git(tmp_path, "commit", "-q", "-am", "work")
    (tmp_path / "staged.txt").write_text("after\n")
    _git(tmp_path, "add", "staged.txt")
    (tmp_path / "unstaged.txt").write_text("after\n")
    (tmp_path / "deleted.txt").unlink()
    (tmp_path / "untracked.txt").write_text("new\n")
    (tmp_path / "ignored.txt").write_text("after\n")
    return tmp_path


def test_the_diff_is_every_change_since_the_merge_base(repository):
    diff = select_tests.diff(repository, "main", explicit=True)
    assert diff.base == "main"
    assert diff.note is None
    assert diff.paths == (
        "committed.txt",
        "deleted.txt",
        "staged.txt",
        "unstaged.txt",
        "untracked.txt",
    )


def test_a_missing_default_base_falls_back_to_main_and_says_so(repository):
    diff = select_tests.diff(repository, "origin/main", explicit=False)
    assert diff.base == "main"
    assert diff.note == "origin/main not found; using local main"
    assert "committed.txt" in diff.paths


def test_with_no_base_at_all_there_is_no_diff(repository):
    _git(repository, "branch", "-q", "-m", "main", "trunk")
    diff = select_tests.diff(repository, "origin/main", explicit=False)
    assert diff.merge_base is None
    assert diff.note == "origin/main not found"
    plan = select_tests.everything(repository, f"no diff to select from: {diff.note}")
    assert plan.full_suite == ("no diff to select from: origin/main not found",)


def test_a_named_base_that_is_not_a_commit_is_refused(repository):
    with pytest.raises(select_tests.SelectionError, match="'nope' is not a commit"):
        select_tests.diff(repository, "nope", explicit=True)
